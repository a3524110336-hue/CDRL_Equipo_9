"""Genera artifacts/m05-document-store.json corriendo las pruebas de M05
contra DynamoDB Local y resumiendo su resultado junto a los indices
declarados en db/nosql/indexes.json. No guarda credenciales.
"""

import datetime
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DYNAMODB_ENDPOINT", f"http://localhost:{os.environ.get('DYNAMODB_PORT', '8000')}")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("DYNAMODB_LOCAL", "true")

JUNIT_PATH = ROOT / "artifacts" / "m05-pytest-report.xml"
JUNIT_PATH.parent.mkdir(exist_ok=True)

result = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/test_m05_document_store.py", "-v", f"--junitxml={JUNIT_PATH}"],
    cwd=ROOT,
)

tree = ET.parse(JUNIT_PATH)
root = tree.getroot()
suite = root.find("testsuite") if root.tag != "testsuite" else root
cases = suite.findall("testcase")

tests_summary = [
    {"name": c.get("name"), "passed": c.find("failure") is None and c.find("error") is None}
    for c in cases
]

indexes = json.loads((ROOT / "db" / "nosql" / "indexes.json").read_text(encoding="utf-8"))
queries_use_query_not_scan = all(
    q["operation"] == "Query" and q["index"] in {"TABLE", "GSI1"} for q in indexes["queries"]
)

REQUIRED_TESTS = {
    "test_caso_normal_alta_lectura_alerta_cambio_estado_borrado": "normal",
    "test_limite_item_al_filo_del_tamano_maximo": "limite_1_tamano_maximo",
    "test_limite_repetir_la_misma_operacion_es_idempotente": "limite_2_idempotencia",
    "test_duplicado_el_segundo_put_no_sobrescribe": "duplicado",
    "test_ausencia_get_update_delete_de_algo_inexistente": "ausencia",
    "test_fallo_declarado_documento_invalido_no_escribe_nada": "fallo_declarado",
    "test_q1_a_q4_usan_query_a_indice_declarado_nunca_scan": "q1_a_q4_query_sin_scan",
}
by_name = {t["name"]: t["passed"] for t in tests_summary}
coverage = {label: by_name.get(name, False) for name, label in REQUIRED_TESTS.items()}

out = {
    "schema": "m05-document-store/1",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "synthetic_fixtures": True,
    "source": "tests/test_m05_document_store.py contra DynamoDB Local (docker-compose.yml)",
    "indexes_declared_in": "db/nosql/indexes.json",
    "max_item_bytes": 409600,
    "queries_use_query_not_scan": queries_use_query_not_scan,
    "pytest": {
        "total": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "exit_code": result.returncode,
        "tests": tests_summary,
    },
    "coverage": coverage,
    "all_required_cases_passed": all(coverage.values()) and result.returncode == 0,
}

(ROOT / "artifacts" / "m05-document-store.json").write_text(
    json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)
print(json.dumps({"all_required_cases_passed": out["all_required_cases_passed"], "pytest": out["pytest"]["total"]}))

if not out["all_required_cases_passed"]:
    sys.exit(1)