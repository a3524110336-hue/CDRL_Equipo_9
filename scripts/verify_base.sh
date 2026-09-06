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
python3 - <<'PY'
import json
from pathlib import Path

Path("artifacts/base-verify.json").write_text(json.dumps({
    "status": "starter_base_valid",
    "scope": "structure_and_contract_only",
    "nextMilestone": "m01-data-contract"
}, indent=2) + "\n")
PY

echo "CDRL starter base verification passed"
