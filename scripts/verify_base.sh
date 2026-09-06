#!/usr/bin/env bash
set -euo pipefail

required_files=(
  ".env.example"
  "Makefile"
  "docker-compose.yml"
  "docs/ADR-000-starter-base.md"
  "evidence/m01-data-contract.json"
  ".github/workflows/cdrl-feedback.yml"
)

for required in "${required_files[@]}"; do
  test -f "$required" || { echo "missing required file: $required" >&2; exit 1; }
done

if command -v docker >/dev/null 2>&1; then
  docker compose config --quiet
fi

python3 - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("evidence/m01-data-contract.json").read_text())
required = {"assignmentId", "commitSha", "commands", "results", "assumptions", "limitations"}
missing = sorted(required.difference(payload))
if missing:
    raise SystemExit(f"missing evidence fields: {', '.join(missing)}")
PY

mkdir -p artifacts

echo "Levantando Postgres y DynamoDB..."
docker compose up -d postgres dynamodb

echo "Esperando a que Postgres esté listo..."
: "${POSTGRES_USER:=cdrl_dev}"
: "${POSTGRES_DB:=cdrl}"
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

bash scripts/apply_migrations.sh

echo "Levantando la API..."
docker compose up -d --build app

echo "Esperando a que la API responda..."
for i in $(seq 1 30); do
  if curl -fs http://localhost:8001/openapi.json > /dev/null 2>&1; then
    break
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
    "status": "m01_data_contract_valid",
    "scope": "api_contract_and_tests",
    "tests": {
        "total": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
    },
}
Path("artifacts/base-verify.json").write_text(json.dumps(summary, indent=2) + "\n")
PY

docker compose down

echo "CDRL M01 verification passed"