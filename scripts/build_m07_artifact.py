"""Genera artifacts/m07-document-evolution.json corriendo las pruebas de M07
contra DynamoDB Local. Reporte por caso (lista `casos`) y mediciones de la
migracion (`migracion`). Fixtures sinteticos; sin credenciales.
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DYNAMODB_ENDPOINT", f"http://localhost:{os.environ.get('DYNAMODB_PORT', '8000')}")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("DYNAMODB_LOCAL", "true")

REQUIRED_CASES = {
    "M07-N1": "normal_migra_v1_deja_v2_y_consultas_iguales",
    "M07-L1": "limite_1_segunda_corrida_no_cambia_nada",
    "M07-L2": "limite_2_interrupcion_y_reanudacion",
    "M07-F1": "fallo_declarado_version_desconocida",
    "M07-T1": "transicion_version_anterior_legible",
    "M07-R1": "rollback_identico_al_respaldo",
    "M07-A1": "auditoria_por_lote_sin_secretos",
}

with tempfile.TemporaryDirectory() as tmp:
    casos_path = Path(tmp) / "casos.json"
    junit_path = Path(tmp) / "junit.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_m07_document_migration.py", "-v",
         f"--junitxml={junit_path}"],
        cwd=ROOT, env=dict(os.environ, M07_CASOS_PATH=str(casos_path)),
    )
    root = ET.parse(junit_path).getroot()
    suite = root.find("testsuite") if root.tag != "testsuite" else root
    reporte = (json.loads(casos_path.read_text(encoding="utf-8"))
               if casos_path.exists() else {"casos": [], "migracion": {}})

casos = reporte["casos"]
por_id = {c["id"]: c["resultado"] == "ok" for c in casos}
coverage = {label: por_id.get(case_id, False) for case_id, label in REQUIRED_CASES.items()}

out = {
    "schema": "m07-document-evolution/1",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "synthetic_fixtures": True,
    "source": "tests/test_m07_document_migration.py contra DynamoDB Local (docker-compose.yml)",
    "adr": "docs/ADR-007-evolucion-compatible.md",
    "migracion_id": "m07-v1-a-v2",
    "pytest": {
        "total": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "exit_code": result.returncode,
    },
    "casos": casos,
    "migracion": reporte["migracion"],
    "coverage": coverage,
    "all_required_cases_passed": all(coverage.values()) and result.returncode == 0,
}

destino = ROOT / "artifacts" / "m07-document-evolution.json"
destino.parent.mkdir(exist_ok=True)
destino.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps({"all_required_cases_passed": out["all_required_cases_passed"],
                  "casos": len(casos), "pytest": out["pytest"]["total"]}))

if not out["all_required_cases_passed"]:
    sys.exit(1)