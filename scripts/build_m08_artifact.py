"""Genera artifacts/m08-pagination-performance.json.

Corre las pruebas de M08 contra DynamoDB Local (reporte por caso) y el
benchmark en escala corta (p50/p95, muestra y entorno contra la linea base).
Si existe artifacts/m08-benchmark-completa.json (make bench), lo referencia
como medicion aparte; verify no la regenera. Fixtures sinteticos; sin
credenciales.
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
    "M08-N1": "normal_diez_paginas_una_vez_y_en_orden",
    "M08-L1": "limite_1_estable_ante_inserciones",
    "M08-L2": "limite_2_ultima_pagina_vacio_y_maximo",
    "M08-F1": "fallo_declarado_cursor_invalido",
    "M08-C1": "contraste_desplazamiento_duplica",
    "M08-R1": "relleno_backfill_repetible",
}

with tempfile.TemporaryDirectory() as tmp:
    casos_path = Path(tmp) / "casos.json"
    junit_path = Path(tmp) / "junit.xml"
    bench_path = Path(tmp) / "bench.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_m08_pagination.py", "-v", f"--junitxml={junit_path}"],
        cwd=ROOT, env=dict(os.environ, M08_CASOS_PATH=str(casos_path)),
    )
    root = ET.parse(junit_path).getroot()
    suite = root.find("testsuite") if root.tag != "testsuite" else root
    reporte = (json.loads(casos_path.read_text(encoding="utf-8"))
               if casos_path.exists() else {"casos": [], "paginacion": {}})
    bench_run = subprocess.run(
        [sys.executable, "scripts/bench_m08.py", "--corto", "--salida", str(bench_path)], cwd=ROOT,
    )
    medicion = (json.loads(bench_path.read_text(encoding="utf-8"))
                if bench_path.exists() else {"benchmark": {}, "entorno": {}})

casos = reporte["casos"]
por_id = {c["id"]: c["resultado"] == "ok" for c in casos}
coverage = {label: por_id.get(case_id, False) for case_id, label in REQUIRED_CASES.items()}

benchmark = medicion["benchmark"]
benchmark_ok = (bench_run.returncode == 0 and benchmark.get("resultados_iguales") is True
                and len(benchmark.get("variantes", {})) == 4)

completa = None
completa_path = ROOT / "artifacts" / "m08-benchmark-completa.json"
if completa_path.exists():
    datos = json.loads(completa_path.read_text(encoding="utf-8"))
    completa = {
        "fuente": "make bench (corrida aparte; verify no la regenera)",
        "benchmark": {k: v for k, v in datos["benchmark"].items() if k != "muestras_ms"},
        "entorno": datos["entorno"],
    }

out = {
    "schema": "m08-pagination-performance/1",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "synthetic_fixtures": True,
    "source": "tests/test_m08_pagination.py y scripts/bench_m08.py contra DynamoDB Local (docker-compose.yml)",
    "adr": "docs/ADR-008-paginacion-indices-rendimiento.md",
    "pytest": {
        "total": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "exit_code": result.returncode,
    },
    "casos": casos,
    "paginacion": reporte["paginacion"],
    "benchmark": benchmark,
    "entorno": medicion["entorno"],
    "benchmark_completo": completa,
    "coverage": coverage,
    "benchmark_ok": benchmark_ok,
    "all_required_cases_passed": all(coverage.values()) and result.returncode == 0 and benchmark_ok,
}

destino = ROOT / "artifacts" / "m08-pagination-performance.json"
destino.parent.mkdir(exist_ok=True)
destino.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps({"all_required_cases_passed": out["all_required_cases_passed"],
                  "casos": len(casos), "pytest": out["pytest"]["total"], "benchmark_ok": benchmark_ok}))

if not out["all_required_cases_passed"]:
    sys.exit(1)