"""Contrato del GSI2 de M08 (ADR-008 §2) contra DynamoDB Local real.

Solo prueba lo que declara db/nosql/indexes.json: que ensure_table() crea el
GSI2 con la forma declarada y que una Query a GSI2 devuelve las lecturas de un
equipo en el mismo orden que la tabla base. La paginación con cursor y el
benchmark son de tests/test_m08_pagination.py y scripts/bench_m08.py.
"""

import json
import os
import uuid
from pathlib import Path

import pytest
from boto3.dynamodb.conditions import Key

os.environ.setdefault("DYNAMODB_ENDPOINT", f"http://localhost:{os.environ.get('DYNAMODB_PORT', '8000')}")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("DYNAMODB_LOCAL", "true")

from src.events_store import EventsStore

ROOT = Path(__file__).resolve().parents[1]
INDEXES = json.loads((ROOT / "db" / "nosql" / "indexes.json").read_text(encoding="utf-8"))
EQUIPO = "fixture-m08-indices"


@pytest.fixture(scope="module")
def store():
    table_name = f"cdrl_eventos_test_m08_{uuid.uuid4().hex[:8]}"
    s = EventsStore(table_name=table_name)
    s.ensure_table()
    yield s
    s.resource.meta.client.delete_table(TableName=table_name)


def _con_gsi2(store, item):
    """Pone GSI2PK como lo hará put_lectura / backfill_m08 (ADR-008 §2)."""
    store.table.update_item(Key={"PK": item["PK"], "SK": item["SK"]},
                            UpdateExpression="SET GSI2PK = :pk",
                            ExpressionAttributeValues={":pk": f"EQ#{EQUIPO}"})


def test_m08_gsi2_declarado_y_activo(store):
    q5 = next(q for q in INDEXES["queries"] if q["id"] == "Q5")
    assert q5["index"] == "GSI2" and q5["operation"] == "Query"

    tabla = store.resource.meta.client.describe_table(TableName=store.table_name)["Table"]
    gsi2 = next(i for i in tabla["GlobalSecondaryIndexes"] if i["IndexName"] == "GSI2")
    assert gsi2["IndexStatus"] == "ACTIVE"
    assert {(k["AttributeName"], k["KeyType"]) for k in gsi2["KeySchema"]} == {("GSI2PK", "HASH"), ("SK", "RANGE")}
    assert gsi2["Projection"]["ProjectionType"] == "ALL"


def test_m08_gsi2_ordena_igual_que_la_tabla_y_deja_fuera_las_alertas(store):
    lecturas = [
        (801, "cpu", "2026-11-01T10:00:00Z"),
        (802, "memoria", "2026-11-01T10:00:00Z"),
        (803, "latencia", "2026-11-01T11:30:00Z"),
        (804, "disco_libre", "2026-11-01T09:15:00Z"),
        (805, "tasa_error", "2026-11-01T11:30:00Z"),
    ]
    unidades = {"cpu": "porcentaje", "memoria": "porcentaje", "disco_libre": "bytes",
                "latencia": "milisegundos", "tasa_error": "proporcion"}
    esperadas = []
    for lectura_id, metrica, medido_en in lecturas:
        store.put_lectura(lectura_id, {"equipo_codigo": EQUIPO, "metrica": metrica, "unidad": unidades[metrica],
                                       "valor": 0.5, "medido_en": medido_en})
        item = store.get_lectura(EQUIPO, metrica, medido_en, lectura_id)
        _con_gsi2(store, item)
        esperadas.append(item["SK"])
    store.put_alerta(lectura_id=803, umbral_id=1, estado="abierta", severidad="advertencia",
                     medido_en="2026-11-01T11:30:00Z")

    pagina = store.table.query(IndexName="GSI2", KeyConditionExpression=Key("GSI2PK").eq(f"EQ#{EQUIPO}"),
                               ScanIndexForward=False)
    obtenidas = [item["SK"] for item in pagina["Items"]]

    assert obtenidas == sorted(esperadas, reverse=True)
    assert all(item["entidad"] == "lectura" for item in pagina["Items"])
    # A igual instante desempata el id numérico, como en ultimas_lecturas (Q1).
    assert [item["lectura_id"] for item in pagina["Items"]][:2] == [805, 803]
