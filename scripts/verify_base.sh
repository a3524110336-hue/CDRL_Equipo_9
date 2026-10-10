#!/usr/bin/env bash
set -euo pipefail

required_files=(
  ".env.example"
  "Makefile"
  "docker-compose.yml"
  "docs/ADR-000-starter-base.md"
  "evidence/m01-data-contract.json"
  "evidence/m02-relational-model.json"
  "evidence/m03-least-privilege.json"
  ".github/workflows/cdrl-feedback.yml"
  "evidence/m03-least-privilege.json"
  "evidence/m04-nosql-decision.json"
  "evidence/m05-document-store.json"
)

for required in "${required_files[@]}"; do
  test -f "$required" || { echo "missing required file: $required" >&2; exit 1; }
done

python3 - <<'PY'
import json
from pathlib import Path

required = {"assignmentId", "commitSha", "commands", "results", "assumptions", "limitations"}
for name in ("evidence/m03-least-privilege.json", "evidence/m04-nosql-decision.json", "evidence/m05-document-store.json"):
    payload = json.loads(Path(name).read_text(encoding="utf-8"))
    missing = sorted(required.difference(payload))
    if missing:
        raise SystemExit(f"missing evidence fields in {name}: {', '.join(missing)}")
PY

mkdir -p artifacts

# ADR-006: versión fija; todo el historial disponible, hallazgos redactados.
# Un hallazgo (o un fallo del escáner) corta verify por set -e.
echo "Escaneando secretos en el historial con gitleaks..."
docker run --rm -v "$PWD:/repo:ro" \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/repo \
  ghcr.io/gitleaks/gitleaks:v8.30.1 \
  git /repo --log-opts="--all" --redact --exit-code=1

# Cada verificación tiene credenciales y volúmenes propios. No rota las claves
# de desarrollo ni elimina sus datos, incluso si las pruebas fallan.
export COMPOSE_FILE="$PWD/docker-compose.yml"
COMPOSE_PROJECT_NAME="cdrl-verify-$("${PYTHON_BIN:-python3}" -c 'import secrets; print(secrets.token_hex(8))')"
POSTGRES_PASSWORD="$("${PYTHON_BIN:-python3}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
CDRL_READER_PASSWORD="$("${PYTHON_BIN:-python3}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
CDRL_WRITER_PASSWORD="$("${PYTHON_BIN:-python3}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
CDRL_OPS_PASSWORD="$("${PYTHON_BIN:-python3}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
export COMPOSE_PROJECT_NAME POSTGRES_PASSWORD CDRL_READER_PASSWORD CDRL_WRITER_PASSWORD CDRL_OPS_PASSWORD
export POSTGRES_USER=cdrl_dev POSTGRES_DB=cdrl
export POSTGRES_PORT="${POSTGRES_PORT:-5432}" DYNAMODB_PORT="${DYNAMODB_PORT:-8000}"
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  printf '::add-mask::%s\n' "$POSTGRES_PASSWORD" "$CDRL_READER_PASSWORD" "$CDRL_WRITER_PASSWORD" "$CDRL_OPS_PASSWORD"
fi
docker compose config --quiet

limpiar() {
  local resultado=$?
  trap - EXIT
  if ! docker compose down -v --remove-orphans; then
    if [ "$resultado" -eq 0 ]; then resultado=1; fi
  fi
  exit "$resultado"
}
trap limpiar EXIT

echo "Levantando Postgres y DynamoDB..."
docker compose up -d postgres dynamodb

echo "Esperando a que Postgres esté listo..."
: "${POSTGRES_USER:=cdrl_dev}"
: "${POSTGRES_DB:=cdrl}"
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "PostgreSQL no quedó disponible dentro del plazo." >&2
    exit 1
  fi
  sleep 1
done

bash scripts/apply_migrations.sh
bash scripts/rotate_db_passwords.sh writer reader ops

echo "Verificando que las restricciones rechacen datos invalidos..."
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f - < scripts/check_constraints.sql

echo "Levantando la API..."
docker compose up -d --build app

echo "Esperando a que la API consulte PostgreSQL con el rol reader..."
for i in $(seq 1 30); do
  if curl -fs --max-time 4 'http://localhost:8001/lecturas?equipo=edge-01&limite=1' > /dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "La API no pudo consultar PostgreSQL dentro del plazo; revisar credenciales y preparación de roles." >&2
    exit 1
  fi
  sleep 1
done

PYTEST_BIN="python3 -m pytest"
if [ -x ".venv/bin/pytest" ]; then
  PYTEST_BIN=".venv/bin/pytest"
fi

$PYTEST_BIN tests/ -v --junitxml=artifacts/pytest-report.xml

python3 - <<'PY'
import json
from pathlib import Path
import xml.etree.ElementTree as ET

tree = ET.parse("artifacts/pytest-report.xml")
root = tree.getroot()
suite = root.find("testsuite") if root.tag != "testsuite" else root

summary = {
    "status": "m03_least_privilege_valid",
    "scope": "role_separation_secrets_and_access_control",
    "tests": {
        "total": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
    },
}
Path("artifacts/base-verify.json").write_text(json.dumps(summary, indent=2) + "\n")
PY

docker compose down -v --remove-orphans
trap - EXIT

echo "CDRL M03 verification passed"
