"""Carga reproducible de eventos sintéticos M04, sin leer PostgreSQL."""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botocore.exceptions import ClientError

from src.events_store import EventsStore, METRICAS

UNIDADES = {
    "cpu": "porcentaje", "memoria": "porcentaje", "disco_libre": "bytes",
    "latencia": "milisegundos", "tasa_error": "proporcion",
}


def fixtures(equipos=3, por_metrica=100, prefijo="fixture-m04", inicio=None):
    """Identidades deterministas por equipo, métrica e instante; sin aleatoriedad."""
    inicio = inicio or datetime(2026, 9, 1, tzinfo=timezone.utc)
    for equipo in range(1, equipos + 1):
        codigo = f"{prefijo}-{equipo:03d}"
        for metrica in METRICAS:
            for numero in range(por_metrica):
                instante = (inicio + timedelta(seconds=numero * 60)).astimezone(timezone.utc).isoformat()
                identidad = f"{codigo}|{metrica}|{instante}"
                lectura_id = int.from_bytes(hashlib.sha256(identidad.encode()).digest()[:8], "big") & ((1 << 63) - 1)
                valor = (numero * 17 + equipo * 7) % 101
                if metrica == "disco_libre":
                    valor *= 1024 * 1024
                elif metrica == "latencia":
                    valor *= 2
                elif metrica == "tasa_error":
                    valor /= 100
                yield lectura_id or 1, {
                    "equipo_codigo": codigo, "metrica": metrica,
                    "unidad": UNIDADES[metrica], "valor": valor, "medido_en": instante,
                }


def _positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Debe ser mayor que cero.")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--equipos", type=_positive, default=3)
    parser.add_argument("--por-metrica", type=_positive, default=100)
    parser.add_argument("--prefijo", default="fixture-m04")
    parser.add_argument("--inicio", default="2026-09-01T00:00:00Z")
    parser.add_argument("--batch", action="store_true", help="Benchmark H3: lotes de 25 SOLO en una tabla vacía y aislada.")
    args = parser.parse_args()
    try:
        inicio = datetime.fromisoformat(args.inicio.replace("Z", "+00:00"))
        if inicio.tzinfo is None:
            raise ValueError("--inicio requiere zona horaria.")
        # Validar también el código antes de crear una tabla o escribir.
        sample_id, sample = next(fixtures(args.equipos, 1, args.prefijo, inicio))
        EventsStore.build_lectura(sample_id, sample)
        # El prefijo real se comprueba con el mayor sufijo, sin alterar el fixture.
        last = dict(sample)
        last["equipo_codigo"] = f"{args.prefijo}-{args.equipos:03d}"
        EventsStore.build_lectura(sample_id, last)
    except ValueError as exc:
        parser.error(str(exc))
    store = EventsStore()
    store.ensure_table()
    if args.batch and store.table.scan(Select="COUNT", Limit=1, ConsistentRead=True)["Count"]:
        parser.error("--batch requiere una tabla vacía; usa DYNAMODB_TABLE con un nombre nuevo.")

    result = {"table": store.table_name, "mode": "batch" if args.batch else "conditional",
              "synthetic": True, "lecturas_insertadas": 0, "lecturas_duplicadas": 0,
              "alertas_insertadas": 0, "alertas_duplicadas": 0}
    alerts = []
    started = perf_counter()

    def conditional(operation, kind, **kwargs):
        try:
            operation(**kwargs)
            result[f"{kind}_insertadas"] += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            result[f"{kind}_duplicadas"] += 1

    def records():
        for lectura_id, lectura in fixtures(args.equipos, args.por_metrica, args.prefijo, inicio):
            # Regla sintética explícita; no sustituye el catálogo de umbrales SQL.
            if lectura["metrica"] == "cpu" and lectura["valor"] > 60:
                alerts.append(dict(lectura_id=lectura_id, umbral_id=1, estado="abierta",
                                   severidad="critica" if lectura["valor"] > 85 else "advertencia",
                                   medido_en=lectura["medido_en"]))
            yield lectura_id, lectura

    if args.batch:
        # batch_writer agrupa 25 y reintenta UnprocessedItems; no admite condiciones.
        # Requiere una tabla dedicada sin escritores concurrentes.
        with store.table.batch_writer() as batch:
            for lectura_id, lectura in records():
                batch.put_item(Item=store.build_lectura(lectura_id, lectura))
                result["lecturas_insertadas"] += 1
    else:
        for lectura_id, lectura in records():
            conditional(store.put_lectura, "lecturas", lectura_id=lectura_id, lectura=lectura)
    result["lecturas_seconds"] = round(perf_counter() - started, 6)
    for alert in alerts:
        conditional(store.put_alerta, "alertas", **alert)
    result["elapsed_seconds"] = round(perf_counter() - started, 6)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
