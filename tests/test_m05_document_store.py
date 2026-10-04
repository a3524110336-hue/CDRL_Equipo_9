"""Pruebas del almacen documental M05 contra DynamoDB Local real.

Requiere DynamoDB Local arriba (docker compose up -d dynamodb) y usa una
tabla sintetica aislada por sesion de pytest, creada con ensure_table() y
borrada al terminar. No guarda credenciales: DYNAMODB_LOCAL genera firmas
efimeras en memoria (ver src/events_store.py).
"""

import json
import os
import uuid
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("DYNAMODB_ENDPOINT", f"http://localhost:{os.environ.get('DYNAMODB_PORT', '8000')}")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("DYNAMODB_LOCAL", "true")

from src.events_store import (
    AlertaNoExiste,
    DocumentoDemasiadoGrande,
    DocumentoInvalido,
    EventsStore,
    MAX_ITEM_BYTES,
    estimate_item_size,
)

ROOT = Path(__file__).resolve().parents[1]
INDEXES_PATH = ROOT / "db" / "nosql" / "indexes.json"


@pytest.fixture(scope="module")
def store():
    table_name = f"cdrl_eventos_test_m05_{uuid.uuid4().hex[:8]}"
    s = EventsStore(table_name=table_name)
    s.ensure_table()
    yield s
    s.resource.meta.client.delete_table(TableName=table_name)


def _lectura(equipo_codigo, lectura_id=1, metrica="cpu", unidad="porcentaje", valor=42.5,
             medido_en="2026-10-04T00:00:00Z"):
    return {
        "equipo_codigo": equipo_codigo, "metrica": metrica, "unidad": unidad,
        "valor": valor, "medido_en": medido_en,
    }


# ---------------------------------------------------------------------------
# Caso normal
# ---------------------------------------------------------------------------

def test_caso_normal_alta_lectura_alerta_cambio_estado_borrado(store):
    lectura_id, umbral_id = 101, 1
    lectura = _lectura("fixture-m05-normal", lectura_id)

    store.put_lectura(lectura_id, lectura)
    guardada = store.get_lectura("fixture-m05-normal", "cpu", lectura["medido_en"], lectura_id)
    assert guardada is not None
    assert guardada["lectura_id"] == lectura_id
    assert guardada["entidad"] == "lectura"

    store.put_alerta(lectura_id=lectura_id, umbral_id=umbral_id, estado="abierta",
                      severidad="advertencia", medido_en=lectura["medido_en"])
    alerta = store.get_alerta(lectura_id, umbral_id)
    assert alerta["estado"] == "abierta"
    assert "cerrada_en" not in alerta

    reconocida = store.actualizar_estado_alerta(lectura_id, umbral_id, "reconocida")
    assert reconocida["estado"] == "reconocida"
    assert "cerrada_en" not in reconocida

    cerrada = store.actualizar_estado_alerta(lectura_id, umbral_id, "cerrada")
    assert cerrada["estado"] == "cerrada"
    assert "cerrada_en" in cerrada

    assert store.delete_alerta(lectura_id, umbral_id) is True
    assert store.get_alerta(lectura_id, umbral_id) is None
    assert store.delete_alerta(lectura_id, umbral_id) is False


# ---------------------------------------------------------------------------
# Limite 1: documento justo en los 400 KB (409 600 bytes), medido por DynamoDB
# ---------------------------------------------------------------------------

def test_limite_item_al_filo_del_tamano_maximo(store):
    base = _lectura("fixture-m05-boundary", medido_en="2026-10-04T01:00:00Z")
    lectura_id_ok, lectura_id_over = 201, 202

    base_item = EventsStore.build_lectura(lectura_id_ok, base)
    base_size = estimate_item_size(base_item)
    payload_key_bytes = len("payload".encode("utf-8"))
    pad_len = MAX_ITEM_BYTES - base_size - payload_key_bytes
    assert pad_len > 0, "el documento base ya es demasiado grande para la prueba"

    item_en_limite = EventsStore.build_lectura(lectura_id_ok, base, payload="x" * pad_len)
    assert estimate_item_size(item_en_limite) == MAX_ITEM_BYTES

    store.put_lectura(lectura_id_ok, base, payload="x" * pad_len)
    guardada = store.get_lectura("fixture-m05-boundary", "cpu", base["medido_en"], lectura_id_ok)
    assert guardada is not None

    with pytest.raises(DocumentoDemasiadoGrande):
        store.put_lectura(lectura_id_over, base, payload="x" * (pad_len + 1))
    ausente = store.get_lectura("fixture-m05-boundary", "cpu", base["medido_en"], lectura_id_over)
    assert ausente is None


# ---------------------------------------------------------------------------
# Limite 2: repetir la misma operacion (idempotencia)
# ---------------------------------------------------------------------------

def test_limite_repetir_la_misma_operacion_es_idempotente(store):
    lectura_id, umbral_id = 301, 301
    lectura = _lectura("fixture-m05-idempotente", lectura_id, medido_en="2026-10-04T02:00:00Z")
    store.put_lectura(lectura_id, lectura)
    store.put_alerta(lectura_id=lectura_id, umbral_id=umbral_id, estado="abierta",
                      severidad="critica", medido_en=lectura["medido_en"])

    primero = store.actualizar_estado_alerta(lectura_id, umbral_id, "reconocida")
    segundo = store.actualizar_estado_alerta(lectura_id, umbral_id, "reconocida")
    assert primero == segundo

    cerrada_1 = store.actualizar_estado_alerta(lectura_id, umbral_id, "cerrada")
    cerrada_2 = store.actualizar_estado_alerta(lectura_id, umbral_id, "cerrada")
    assert cerrada_1 == cerrada_2
    assert cerrada_1["cerrada_en"] == cerrada_2["cerrada_en"]

    assert store.delete_alerta(lectura_id, umbral_id) is True
    assert store.delete_alerta(lectura_id, umbral_id) is False


# ---------------------------------------------------------------------------
# Duplicado: el segundo put no sobrescribe
# ---------------------------------------------------------------------------

def test_duplicado_el_segundo_put_no_sobrescribe(store):
    lectura_id = 401
    lectura = _lectura("fixture-m05-duplicado", lectura_id, valor=10.0, medido_en="2026-10-04T03:00:00Z")
    store.put_lectura(lectura_id, lectura)

    with pytest.raises(ClientError) as exc:
        store.put_lectura(lectura_id, lectura)
    assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    guardada = store.get_lectura("fixture-m05-duplicado", "cpu", lectura["medido_en"], lectura_id)
    assert float(guardada["valor"]) == 10.0

    umbral_id = 41
    store.put_alerta(lectura_id=lectura_id, umbral_id=umbral_id, estado="abierta",
                      severidad="advertencia", medido_en=lectura["medido_en"])
    with pytest.raises(ClientError) as exc2:
        store.put_alerta(lectura_id=lectura_id, umbral_id=umbral_id, estado="reconocida",
                          severidad="critica", medido_en=lectura["medido_en"])
    assert exc2.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    original = store.get_alerta(lectura_id, umbral_id)
    assert original["estado"] == "abierta"
    assert original["severidad"] == "advertencia"


# ---------------------------------------------------------------------------
# Ausencia: get/update/delete de algo que no existe
# ---------------------------------------------------------------------------

def test_ausencia_get_update_delete_de_algo_inexistente(store):
    assert store.get_lectura("fixture-m05-ausente", "cpu", "2026-10-04T04:00:00Z", 999) is None
    assert store.get_alerta(999, 999) is None
    with pytest.raises(AlertaNoExiste):
        store.actualizar_estado_alerta(999, 999, "reconocida")
    assert store.delete_alerta(999, 999) is False


# ---------------------------------------------------------------------------
# Fallo declarado: documento invalido rechazado sin escribir nada
# ---------------------------------------------------------------------------

def test_fallo_declarado_documento_invalido_no_escribe_nada(store):
    medido_en = "2026-10-04T05:00:00Z"

    metrica_invalida = _lectura("fixture-m05-invalida", 501, metrica="temperatura", unidad="porcentaje",
                                medido_en=medido_en)
    with pytest.raises(DocumentoInvalido):
        store.put_lectura(501, metrica_invalida)

    sin_zona = _lectura("fixture-m05-invalida", 502, medido_en="2026-10-04T05:00:00")
    with pytest.raises(DocumentoInvalido):
        store.put_lectura(502, sin_zona)

    campo_extra = dict(_lectura("fixture-m05-invalida", 503, medido_en=medido_en), origen="x")
    with pytest.raises(DocumentoInvalido):
        store.put_lectura(503, campo_extra)

    valor_fuera_rango = _lectura("fixture-m05-invalida", 504, valor=140.0, medido_en=medido_en)
    with pytest.raises(DocumentoInvalido):
        store.put_lectura(504, valor_fuera_rango)

    for lectura_id in (501, 502, 503, 504):
        assert store.get_lectura("fixture-m05-invalida", "cpu", medido_en, lectura_id) is None

    alerta_estado_invalido = dict(lectura_id=601, umbral_id=1, estado="pendiente",
                                  severidad="advertencia", medido_en=medido_en)
    with pytest.raises(DocumentoInvalido):
        store.put_alerta(**alerta_estado_invalido)
    assert store.get_alerta(601, 1) is None

    cierre_incoherente = dict(lectura_id=602, umbral_id=1, estado="cerrada",
                              severidad="critica", medido_en=medido_en)
    with pytest.raises(DocumentoInvalido):
        store.put_alerta(**cierre_incoherente)
    assert store.get_alerta(602, 1) is None


# ---------------------------------------------------------------------------
# Q1-Q4 van por Query a un indice declarado, nunca por Scan
# ---------------------------------------------------------------------------

def test_q1_a_q4_usan_query_a_indice_declarado_nunca_scan(store):
    indexes = json.loads(INDEXES_PATH.read_text(encoding="utf-8"))
    queries = indexes["queries"]
    assert {q["id"] for q in queries} == {"Q1", "Q2", "Q3", "Q4"}
    for q in queries:
        assert q["operation"] == "Query", f"{q['id']} debe usar Query, no {q['operation']}"
        assert q["index"] in {"TABLE", "GSI1"}, f"{q['id']} declara un indice desconocido"

    def _scan_prohibido(*args, **kwargs):
        raise AssertionError("Q1-Q4 no deben llamar a Scan")

    original_scan = store.table.scan
    store.table.scan = _scan_prohibido
    try:
        lectura_id = 701
        lectura = _lectura("fixture-m05-query", lectura_id, medido_en="2026-10-04T06:00:00Z")
        store.put_lectura(lectura_id, lectura)
        store.put_alerta(lectura_id=lectura_id, umbral_id=701, estado="abierta",
                          severidad="advertencia", medido_en=lectura["medido_en"])

        pagina_lecturas = store.query_lecturas("fixture-m05-query", "cpu", limite=10)
        assert any(item["lectura_id"] == lectura_id for item in pagina_lecturas["Items"])

        encontrada = False
        for shard in range(4):
            pagina_alertas = store.query_alertas("abierta", shard)
            if any(item["lectura_id"] == lectura_id and item["umbral_id"] == 701
                   for item in pagina_alertas["Items"]):
                encontrada = True
        assert encontrada, "la alerta nueva debe aparecer en Query a GSI1 por alguno de los shards"
    finally:
        store.table.scan = original_scan