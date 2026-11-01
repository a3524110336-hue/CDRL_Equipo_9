"""Benchmark M08: linea base (fan-out de 5 Query) contra GSI2 con cursor.

Mide cuatro variantes (ADR-008 s5) sobre una tabla aislada de DynamoDB Local
con lecturas sinteticas y semilla fija. Reporta p50/p95, tamano de muestra,
llamadas, items leidos y RCU por ejecucion, el costo de escritura del GSI2 y
el entorno. No fija de antemano que GSI2 gane: informa lo que sale.

Uso:
    python scripts/bench_m08.py            # carga completa (make bench)
    python scripts/bench_m08.py --corto    # carga reducida (la usa make verify)
Sin credenciales.
"""

import argparse
import json
import math
import os
import platform
import random
import re
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DYNAMODB_ENDPOINT", f"http://localhost:{os.environ.get('DYNAMODB_PORT', '8000')}")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("DYNAMODB_LOCAL", "true")

import boto3

from src.events_store import EventsStore, METRICAS

EQUIPOS = [f"fixture-m08-{n:02d}" for n in range(5)]
SEMILLA = 42
CALENTAMIENTO = 5
MEDIDAS = 50
INICIO = datetime(2026, 10, 1, tzinfo=timezone.utc)
UNIDADES = {"cpu": "porcentaje", "memoria": "porcentaje", "disco_libre": "bytes",
            "latencia": "milisegundos", "tasa_error": "proporcion"}
ESCALAS = {
    "completa": dict(por_serie=400, k=100),   # 5 equipos x 5 metricas x 400 = 10 000
    "corta": dict(por_serie=40, k=10),        # 5 x 5 x 40 = 1 000
}


class Medidor:
    """Envuelve table.query para contar llamadas, items y capacidad consumida."""

    def __init__(self, tabla):
        self.tabla = tabla
        self.original = tabla.query
        self.llamadas = 0
        self.items = 0
        self.rcu = 0.0
        self.rcu_visto = False

    def __enter__(self):
        def espia(**kwargs):
            kwargs.setdefault("ReturnConsumedCapacity", "TOTAL")
            respuesta = self.original(**kwargs)
            self.llamadas += 1
            self.items += len(respuesta["Items"])
            capacidad = respuesta.get("ConsumedCapacity")
            if capacidad:
                self.rcu += float(capacidad["CapacityUnits"])
                self.rcu_visto = True
            return respuesta

        self.tabla.query = espia
        return self

    def __exit__(self, *exc):
        del self.tabla.query


def cargar(store, por_serie):
    rng = random.Random(SEMILLA)
    lectura_id = 0
    for equipo in EQUIPOS:
        for metrica in METRICAS:
            for minuto in range(por_serie):
                lectura_id += 1
                medido_en = INICIO + timedelta(minutes=minuto, seconds=rng.randint(0, 59))
                store.put_lectura(lectura_id, dict(equipo_codigo=equipo, metrica=metrica,
                                                   unidad=UNIDADES[metrica], valor=0.5, medido_en=medido_en),
                                  fuente="fixture")
    return lectura_id


def _cursor_pagina_9(store, equipo, k):
    cursor = None
    for _ in range(9):
        cursor = store.paginar_lecturas_equipo(equipo, limite=k, cursor=cursor)["cursor"]
    return cursor


def _variantes(store, equipo, k):
    cursor9 = _cursor_pagina_9(store, equipo, k)
    return {
        "A1": lambda: store.ultimas_lecturas(equipo, limite=k),
        "B1": lambda: store.paginar_lecturas_equipo(equipo, limite=k)["items"],
        "A10": lambda: store.ultimas_lecturas(equipo, limite=10 * k)[9 * k:],
        "B10": lambda: store.paginar_lecturas_equipo(equipo, limite=k, cursor=cursor9)["items"],
    }


def _medir(store, funciones):
    nombres = list(funciones)
    for _ in range(CALENTAMIENTO):
        for nombre in nombres:
            funciones[nombre]()
    muestras = {n: [] for n in nombres}
    acumulado = {n: dict(llamadas=0, items=0, rcu=0.0, visto=False) for n in nombres}
    ids = {}
    for _ in range(MEDIDAS):
        for nombre in nombres:
            with Medidor(store.table) as medidor:
                inicio = time.perf_counter_ns()
                resultado = funciones[nombre]()
                fin = time.perf_counter_ns()
            muestras[nombre].append((fin - inicio) / 1e6)
            a = acumulado[nombre]
            a["llamadas"] += medidor.llamadas
            a["items"] += medidor.items
            a["rcu"] += medidor.rcu
            a["visto"] = a["visto"] or medidor.rcu_visto
            ids[nombre] = [int(item["lectura_id"]) for item in resultado]
    return muestras, acumulado, ids


def _por_ejecucion(total, n):
    valor = total / n
    return int(valor) if float(valor).is_integer() else round(valor, 2)


def _resumen(muestras, acumulado):
    q = statistics.quantiles(muestras, n=100, method="inclusive")
    n = len(muestras)
    return dict(n=n, p50_ms=round(q[49], 3), p95_ms=round(q[94], 3), min_ms=round(min(muestras), 3),
                max_ms=round(max(muestras), 3), media_ms=round(statistics.fmean(muestras), 3),
                llamadas=_por_ejecucion(acumulado["llamadas"], n),
                items_leidos=_por_ejecucion(acumulado["items"], n),
                rcu=_por_ejecucion(acumulado["rcu"], n) if acumulado["visto"] else None)


def _costo_escritura(store):
    lectura = dict(equipo_codigo="fixture-m08-costo", metrica="cpu", unidad="porcentaje", valor=0.5, medido_en=INICIO)
    con = store.build_lectura(900001, lectura, fuente="fixture")
    sin = store.build_lectura(900002, dict(lectura, medido_en=INICIO + timedelta(minutes=1)), fuente="fixture")
    sin.pop("GSI2PK", None)
    cap = {}
    for nombre, item in (("con", con), ("sin", sin)):
        respuesta = store.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)",
                                         ReturnConsumedCapacity="INDEXES")
        cap[nombre] = float(respuesta["ConsumedCapacity"]["CapacityUnits"])
    tam_con = len(json.dumps(con, default=str).encode("utf-8"))
    tam_sin = len(json.dumps(sin, default=str).encode("utf-8"))
    if cap["con"] > cap["sin"]:
        return dict(fuente="medida", wcu_sin_gsi2=cap["sin"], wcu_con_gsi2=cap["con"],
                    bytes_item_con_gsi2=tam_con, bytes_item_sin_gsi2=tam_sin)
    # DynamoDB Local no informo la capacidad del indice: se reporta la calculada
    # (1 WCU por KB en la tabla y otra por KB en el indice que recibe el item).
    wcu_sin = math.ceil(tam_sin / 1024)
    return dict(fuente="calculada", wcu_sin_gsi2=wcu_sin, wcu_con_gsi2=wcu_sin + math.ceil(tam_con / 1024),
                bytes_item_con_gsi2=tam_con, bytes_item_sin_gsi2=tam_sin,
                capacidad_informada_por_dynamodb_local=dict(con=cap["con"], sin=cap["sin"]))


def _entorno():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    imagen = re.search(r"amazon/dynamodb-local:[^\s@\"']+@sha256:[0-9a-f]{64}", compose)
    docker = None
    try:
        docker = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                                capture_output=True, text=True, timeout=20).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return dict(dynamodb_local=imagen.group(0) if imagen else None, python=platform.python_version(),
                boto3=boto3.__version__, plataforma=platform.platform(), procesador=platform.processor() or None,
                nucleos=os.cpu_count(), docker_server=docker,
                ejecutado_en="ci" if (os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")) else "local")


def ejecutar(escala):
    cfg = ESCALAS[escala]
    k = cfg["k"]
    corrida = uuid.uuid4().hex[:12]
    store = EventsStore(table_name=f"m08_bench_{corrida}", audit_table_name=f"m08_bench_aud_{corrida}",
                        migration_table_name=f"m08_bench_resp_{corrida}")
    store.ensure_table()
    try:
        inicio = time.perf_counter()
        total = cargar(store, cfg["por_serie"])
        carga_s = round(time.perf_counter() - inicio, 1)

        muestras, acumulado, ids = _medir(store, _variantes(store, EQUIPOS[0], k))
        if acumulado["A1"]["llamadas"] == 0:
            raise SystemExit("el medidor no intercepto las consultas de la linea base")
        iguales = (ids["A1"] == ids["B1"] and ids["A10"] == ids["B10"]
                   and len(ids["A1"]) == k and len(ids["A10"]) == k)
        if not iguales:
            raise SystemExit("A y B devuelven resultados distintos: no se compara la velocidad")

        variantes = {n: _resumen(muestras[n], acumulado[n]) for n in muestras}
        comparacion = []
        for base, gsi2 in (("A1", "B1"), ("A10", "B10")):
            a, b = variantes[base], variantes[gsi2]
            comparacion.append(dict(
                base=base, gsi2=gsi2,
                razon_p50_base_sobre_gsi2=round(a["p50_ms"] / b["p50_ms"], 2),
                razon_p95_base_sobre_gsi2=round(a["p95_ms"] / b["p95_ms"], 2),
                gsi2_mas_rapido_p50=b["p50_ms"] < a["p50_ms"], gsi2_mas_rapido_p95=b["p95_ms"] < a["p95_ms"],
                llamadas_base=a["llamadas"], llamadas_gsi2=b["llamadas"],
                items_leidos_base=a["items_leidos"], items_leidos_gsi2=b["items_leidos"]))
        benchmark = dict(
            escala=escala, lecturas=total, equipos=len(EQUIPOS), metricas=len(METRICAS), por_serie=cfg["por_serie"],
            k=k, equipo_medido=EQUIPOS[0], semilla=SEMILLA, calentamiento=CALENTAMIENTO, medidas_por_variante=MEDIDAS,
            carga_s=carga_s, variantes=variantes, resultados_iguales=True, comparacion=comparacion,
            costo_escritura=_costo_escritura(store),
            muestras_ms={n: [round(x, 3) for x in v] for n, v in muestras.items()})
        return dict(benchmark=benchmark, entorno=_entorno())
    finally:
        for nombre in (store.table_name, store.audit_table_name, store.migration_table_name):
            store.resource.meta.client.delete_table(TableName=nombre)


def imprimir(benchmark):
    print(f"escala={benchmark['escala']} lecturas={benchmark['lecturas']} k={benchmark['k']} "
          f"medidas={benchmark['medidas_por_variante']} carga={benchmark['carga_s']}s")
    for nombre, v in benchmark["variantes"].items():
        print(f"{nombre:>4}  p50={v['p50_ms']:>9.3f} ms  p95={v['p95_ms']:>9.3f} ms  "
              f"llamadas={v['llamadas']}  items={v['items_leidos']}  rcu={v['rcu']}")
    costo = benchmark["costo_escritura"]
    print(f"escritura: {costo['wcu_sin_gsi2']} -> {costo['wcu_con_gsi2']} WCU ({costo['fuente']})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corto", action="store_true", help="carga reducida (la usa make verify)")
    parser.add_argument("--salida", help="ruta del JSON de salida")
    args = parser.parse_args()
    escala = "corta" if args.corto else "completa"
    resultado = ejecutar(escala)
    destino = Path(args.salida) if args.salida else ROOT / "artifacts" / f"m08-benchmark-{escala}.json"
    destino.parent.mkdir(exist_ok=True)
    destino.write_text(json.dumps(resultado, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    imprimir(resultado["benchmark"])


if __name__ == "__main__":
    main()