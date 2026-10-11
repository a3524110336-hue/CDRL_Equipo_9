"""Genera artifacts/m06-evolution-recovery.json corriendo las pruebas de M06
contra DynamoDB Local. Reporte por caso (lista `casos`) y mediciones de
recuperacion (`recuperacion`). Fixtures sinteticos; sin credenciales.
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
    "M06-N1": "normal_v1_y_v2_se_leen_igual",
    "M06-L1": "limite_1_v1_sin_campos_nuevos",
    "M06-L2": "limite_2_restaurar_existente_no_duplica",
    "M06-F1": "fallo_declarado_version_desconocida",
    "M06-F2": "fallo_declarado_auditoria_campo_prohibido",
    "M06-A1": "auditoria_una_operacion_sin_datos",
    "M06-A2": "auditoria_append_only",
    "M06-R1": "recuperacion_con_mediciones",
    "M06-S1": "auditoria_sin_secretos",
}

with tempfile.TemporaryDirectory() as tmp:
    casos_path = Path(tmp) / "casos.json"
    junit_path = Path(tmp) / "junit.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_m06_evolution_recovery.py", "-v",
         f"--junitxml={junit_path}"],
        cwd=ROOT, env=dict(os.environ, M06_CASOS_PATH=str(casos_path)),
    )
    root = ET.parse(junit_path).getroot()
    suite = root.find("testsuite") if root.tag != "testsuite" else root
    reporte = (json.loads(casos_path.read_text(encoding="utf-8"))
               if casos_path.exists() else {"casos": [], "recuperacion": {}})

casos = reporte["casos"]
por_id = {c["id"]: c["resultado"] == "ok" for c in casos}
coverage = {label: por_id.get(case_id, False) for case_id, label in REQUIRED_CASES.items()}

out = {
    "schema": "m06-evolution-recovery/1",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "synthetic_fixtures": True,
    "source": "tests/test_m06_evolution_recovery.py contra DynamoDB Local (docker-compose.yml)",
    "adr": "docs/ADR-006-evolucion-auditoria-recuperacion.md",
    "pytest": {
        "total": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "exit_code": result.returncode,
    },
    "casos": casos,
    "recuperacion": reporte["recuperacion"],
    "coverage": coverage,
    "all_required_cases_passed": all(coverage.values()) and result.returncode == 0,
}

destino = ROOT / "artifacts" / "m06-evolution-recovery.json"
destino.parent.mkdir(exist_ok=True)
destino.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps({"all_required_cases_passed": out["all_required_cases_passed"],
                  "casos": len(casos), "pytest": out["pytest"]["total"]}))

if not out["all_required_cases_passed"]:
    sys.exit(1)