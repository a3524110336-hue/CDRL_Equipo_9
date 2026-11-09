"""Genera artifacts/m09-nosql-security.json.

Corre las pruebas de M09 contra DynamoDB Local y arma el reporte: un caso por
prueba, la matriz rol x coleccion x operacion con lo esperado contra lo
obtenido, el conteo de eventos de auditoria por resultado y el barrido de
secretos. Fixtures sinteticos; sin credenciales.
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

# id -> (prefijo de la prueba, criterio que demuestra)
CASOS = {
    "M09-N1": ("test_normal_", "normal: cdrl_ops reconoce una alerta y el evento trae actor, accion, recurso y resultado"),
    "M09-L1": ("test_limite_1_", "limite 1: permiso en la coleccion pero no en la operacion; tabla intacta y evento denegada"),
    "M09-L2": ("test_limite_2_", "limite 2: lectura cruzada, scan y principales sin rol o con rol inventado; todo negado"),
    "M09-F1": ("test_fallo_declarado_", "fallo declarado: un secreto no entra a la auditoria (AuditoriaInvalida) y el barrido no encuentra coincidencias"),
    "M09-R1": ("test_regresion_", "remediacion: el rol autodeclarado ya no borra ni queda auditado como ok"),
    "M09-M1": ("test_matriz_completa_", "matriz completa: cada celda rol x coleccion x operacion, esperado contra obtenido"),
}

with tempfile.TemporaryDirectory() as tmp:
    junit_path = Path(tmp) / "junit.xml"
    casos_path = Path(tmp) / "casos.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_m09_security.py", "-v", f"--junitxml={junit_path}"],
        cwd=ROOT, env=dict(os.environ, M09_CASOS_PATH=str(casos_path)),
    )
    root = ET.parse(junit_path).getroot()
    suite = root.find("testsuite") if root.tag != "testsuite" else root
    reporte = (json.loads(casos_path.read_text(encoding="utf-8")) if casos_path.exists()
               else {"matriz": [], "eventos_por_resultado": {}, "barrido_secretos": {}})
    ejecutadas = []
    for case in suite.iter("testcase"):
        ok = case.find("failure") is None and case.find("error") is None and case.find("skipped") is None
        ejecutadas.append((case.get("name").split("[")[0], ok))

casos = []
for id_, (prefijo, criterio) in CASOS.items():
    mias = [ok for nombre, ok in ejecutadas if nombre.startswith(prefijo)]
    casos.append({"id": id_, "prueba": prefijo.rstrip("_"), "criterio": criterio,
                  "pruebas_ejecutadas": len(mias),
                  "resultado": "ok" if mias and all(mias) else "fallo"})
coverage = {c["id"]: c["resultado"] == "ok" for c in casos}

matriz = reporte["matriz"]
discrepancias = [c for c in matriz if c["esperado"] != c["obtenido"]]
eventos = reporte["eventos_por_resultado"]
barrido = reporte["barrido_secretos"]
auditoria_ok = (eventos.get("ok", 0) > 0 and eventos.get("denegada", 0) > 0
                and barrido.get("eventos_revisados", 0) > 0 and barrido.get("coincidencias", 1) == 0)

out = {
    "schema": "m09-nosql-security/1",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "synthetic_fixtures": True,
    "source": "tests/test_m09_security.py contra DynamoDB Local (docker-compose.yml)",
    "adr": "docs/ADR-009-seguridad-nosql-auditoria.md",
    "pytest": {
        "total": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "exit_code": result.returncode,
    },
    "casos": casos,
    "matriz": matriz,
    "matriz_resumen": {"celdas": len(matriz), "discrepancias": len(discrepancias)},
    "eventos_por_resultado": eventos,
    "barrido_secretos": barrido,
    "coverage": coverage,
    "auditoria_ok": auditoria_ok,
    "all_required_cases_passed": (all(coverage.values()) and result.returncode == 0
                                  and len(matriz) > 0 and not discrepancias and auditoria_ok),
}

destino = ROOT / "artifacts" / "m09-nosql-security.json"
destino.parent.mkdir(exist_ok=True)
destino.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps({"all_required_cases_passed": out["all_required_cases_passed"],
                  "casos": len(casos), "celdas": len(matriz), "pytest": out["pytest"]["total"]}))

if not out["all_required_cases_passed"]:
    sys.exit(1)