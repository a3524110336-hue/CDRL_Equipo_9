"""Pruebas M08 (paginacion estable con cursor, GSI2 y relleno) contra DynamoDB Local.

Usa el events_store.py real. Fixture sintetico: 1 000 lecturas por equipo,
semilla 42, paginas de 100. Cada prueba deja un caso en CASOS; si existe
M08_CASOS_PATH, al terminar el modulo se vuelcan junto con las mediciones de
paginacion para que scripts/build_m08_artifact.py arme el artifact.
Sin credenciales.
"""

import base64
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from boto3.dynamodb.conditions import Attr

os.environ.setdefault("DYNAMODB_ENDPOINT", f"http://localhost:{os.environ.get('DYNAMODB_PORT', '8000')}")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("DYNAMODB_LOCAL", "true")

from src.events_store import CursorInvalido, EventsStore, METRICAS
from src.nosql_access import Principal

N = 1000
LIMITE = 100
NUEVAS = 50
INICIO = datetime(2026, 10, 1, tzinfo=timezone.utc)
HASTA = INICIO + timedelta(minutes=N - 1)
EQUIPO_PAG = "fixture-m08-pag"
EQUIPO_INS = "fixture-m08-ins"
EQUIPO_RELLENO = "fixture-m08-relleno"
EQUIPO_VACIO = "fixture-m08-vacio"
EQUIPO_OTRO = "fixture-m08-otro"
UNIDADES = {"cpu": "porcentaje", "memoria": "porcentaje", "disco_libre": "bytes",
            "latencia": "milisegundos", "tasa_error": "proporcion"}

CASOS: list[dict] = []
PAGINACION: dict = {}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _plano(valor):
    return json.loads(json.dumps(valor, default=str, ensure_ascii=False))


def _caso(id_, prueba, criterio, esperado, obtenido, codigo_error=None):
    """Registra el caso ANTES de afirmar, para que el reporte muestre el obtenido."""
    esperado, obtenido = _plano(esperado), _plano(obtenido)
    CASOS.append({
        "id": id_, "prueba": prueba, "criterio": criterio,
        "esperado": esperado, "obtenido": obtenido, "codigo_error": codigo_error,
        "resultado": "ok" if esperado == obtenido else "fallo",
    })
    assert obtenido == esperado


@pytest.fixture(scope="module", autouse=True)
def _volcar_reporte():
    yield
    destino = os.environ.get("M08_CASOS_PATH")
    if destino:
        Path(destino).write_text(
            json.dumps({"casos": CASOS, "paginacion": PAGINACION}, default=str, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


@pytest.fixture(scope="module")
def store():
    corrida = uuid.uuid4().hex[:12]
    s = EventsStore(
        table_name=f"m08_eventos_{corrida}",
        audit_table_name=f"m08_auditoria_{corrida}",
        migration_table_name=f"m08_respaldo_{corrida}",
        principal=Principal("job-backfill", "migracion"),
    )
    s.ensure_table()
    yield s
    for nombre in (s.table_name, s.audit_table_name, s.migration_table_name):
        s.resource.meta.client.delete_table(TableName=nombre)


def _lectura(equipo, lectura_id, medido_en):
    metrica = METRICAS[lectura_id % len(METRICAS)]
    return dict(equipo_codigo=equipo, metrica=metrica, unidad=UNIDADES[metrica],
                valor=0.5, medido_en=medido_en)


def _tiempos():
    """id -> medido_en. Los minutos se reparten al azar (semilla 42) entre los ids."""
    minutos = list(range(N))
    random.Random(42).shuffle(minutos)
    return {i + 1: INICIO + timedelta(minutes=m) for i, m in enumerate(minutos)}


def _cargar(s, equipo, tiempos):
    for lectura_id, medido_en in tiempos.items():
        s.put_lectura(lectura_id, _lectura(equipo, lectura_id, medido_en), fuente="fixture")


def _orden_esperado(tiempos):
    return sorted(tiempos, key=tiempos.get, reverse=True)


def _id(item):
    return int(item["lectura_id"])


def _ids(paginas):
    return [_id(item) for pagina in paginas for item in pagina["items"]]


def _conteo(ids, esperadas):
    return len(ids) - len(set(ids)), len(set(esperadas) - set(ids))


def _recorrer(s, equipo, *, limite=LIMITE, hasta=None):
    paginas, cursor = [], None
    while True:
        pagina = s.paginar_lecturas_equipo(equipo, hasta=hasta if cursor is None else None,
                                           limite=limite, cursor=cursor)
        paginas.append(pagina)
        cursor = pagina["cursor"]
        if cursor is None or len(paginas) > 30:
            return paginas


def _codificar(estado):
    return base64.urlsafe_b64encode(json.dumps(estado, separators=(",", ":")).encode()).decode().rstrip("=")


def _decodificar(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def _escanear(s, filtro):
    items, cursor = [], None
    while True:
        argumentos = dict(FilterExpression=filtro, ConsistentRead=True)
        if cursor:
            argumentos["ExclusiveStartKey"] = cursor
        pagina = s.table.scan(**argumentos)
        items += pagina["Items"]
        cursor = pagina.get("LastEvaluatedKey")
        if not cursor:
            return items


@pytest.fixture(scope="module")
def datos(store):
    tiempos = _tiempos()
    _cargar(store, EQUIPO_PAG, tiempos)
    return tiempos


@pytest.fixture(scope="module")
def insercion(store):
    """Escenario del limite 1 y del contraste: mismo equipo, mismas inserciones.

    Paginas 1-3 con cursor y por desplazamiento; entran 50 lecturas nuevas
    (medido_en > hasta); se terminan los dos recorridos.
    """
    tiempos = _tiempos()
    _cargar(store, EQUIPO_INS, tiempos)
    esperadas = _orden_esperado(tiempos)

    def offset(n):
        return [_id(i) for i in store.ultimas_lecturas(EQUIPO_INS, limite=n * LIMITE)[(n - 1) * LIMITE:n * LIMITE]]

    paginas = [store.paginar_lecturas_equipo(EQUIPO_INS, hasta=HASTA, limite=LIMITE)]
    for _ in range(2):
        paginas.append(store.paginar_lecturas_equipo(EQUIPO_INS, limite=LIMITE, cursor=paginas[-1]["cursor"]))
    base = [offset(n) for n in (1, 2, 3)]

    nuevos = []
    for k in range(NUEVAS):
        lectura_id = N + 1 + k
        store.put_lectura(lectura_id, _lectura(EQUIPO_INS, lectura_id, HASTA + timedelta(minutes=k + 1)),
                          fuente="fixture")
        nuevos.append(lectura_id)

    while paginas[-1]["cursor"] is not None and len(paginas) < 30:
        paginas.append(store.paginar_lecturas_equipo(EQUIPO_INS, limite=LIMITE, cursor=paginas[-1]["cursor"]))
    base += [offset(n) for n in range(4, 11)]
    return dict(esperadas=esperadas, nuevos=nuevos, paginas=paginas, base=base)


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------

def test_normal_diez_paginas_cada_lectura_una_vez_en_orden(store, datos):
    paginas = _recorrer(store, EQUIPO_PAG)
    ids = _ids(paginas)
    duplicadas, saltadas = _conteo(ids, _orden_esperado(datos))
    _caso("M08-N1", "test_normal_diez_paginas_cada_lectura_una_vez_en_orden",
          "10 paginas de 100: las 1000 lecturas, una vez cada una y en SK descendente; la ultima sin cursor",
          {"paginas": 10, "tamanos": [100] * 10, "lecturas_unicas": 1000, "duplicadas": 0, "saltadas": 0,
           "mismo_orden_descendente": True, "cursor_en_las_intermedias": True, "ultima_sin_cursor": True},
          {"paginas": len(paginas), "tamanos": [len(p["items"]) for p in paginas],
           "lecturas_unicas": len(set(ids)), "duplicadas": duplicadas, "saltadas": saltadas,
           "mismo_orden_descendente": ids == _orden_esperado(datos),
           "cursor_en_las_intermedias": all(p["cursor"] for p in paginas[:-1]),
           "ultima_sin_cursor": paginas[-1]["cursor"] is None})


# ---------------------------------------------------------------------------
# Limite 1: estable ante inserciones a media paginacion
# ---------------------------------------------------------------------------

def test_limite_estable_ante_inserciones_a_media_paginacion(store, insercion):
    ids = _ids(insercion["paginas"])
    duplicadas, saltadas = _conteo(ids, insercion["esperadas"])
    nuevas_en_recorrido = len(set(ids) & set(insercion["nuevos"]))
    PAGINACION.update(paginas=len(insercion["paginas"]), lecturas_unicas=len(set(ids)),
                      duplicadas=duplicadas, saltadas=saltadas, insertadas_durante=len(insercion["nuevos"]))
    _caso("M08-L1", "test_limite_estable_ante_inserciones_a_media_paginacion",
          "con 50 lecturas nuevas entre la pagina 3 y la 4, el recorrido devuelve las 1000 originales, sin duplicar, saltar ni incluir las nuevas",
          {"insertadas_durante": 50, "paginas": 10, "lecturas_unicas": 1000, "duplicadas": 0, "saltadas": 0,
           "nuevas_en_el_recorrido": 0, "mismo_orden_descendente": True},
          {"insertadas_durante": len(insercion["nuevos"]), "paginas": len(insercion["paginas"]),
           "lecturas_unicas": len(set(ids)), "duplicadas": duplicadas, "saltadas": saltadas,
           "nuevas_en_el_recorrido": nuevas_en_recorrido,
           "mismo_orden_descendente": ids == insercion["esperadas"]})


# ---------------------------------------------------------------------------
# Limite 2: bordes
# ---------------------------------------------------------------------------

def test_limite_ultima_pagina_exacta_equipo_vacio_y_limite_maximo(store, datos):
    ultima = _recorrer(store, EQUIPO_PAG)[-1]
    p999 = _recorrer(store, EQUIPO_PAG, limite=999)
    p1000 = _recorrer(store, EQUIPO_PAG, limite=1000)
    vacio = store.paginar_lecturas_equipo(EQUIPO_VACIO)
    _caso("M08-L2", "test_limite_ultima_pagina_exacta_equipo_vacio_y_limite_maximo",
          "la ultima pagina sale exacta y sin cursor; un equipo sin lecturas devuelve [] sin cursor; limite=1000 cabe en una pagina",
          {"ultima_pagina_exacta_sin_cursor": True, "limite_999_tamanos": [999, 1], "limite_999_cursor_final": None,
           "limite_1000_tamanos": [1000], "limite_1000_cursor_final": None,
           "equipo_vacio_items": [], "equipo_vacio_cursor": None},
          {"ultima_pagina_exacta_sin_cursor": len(ultima["items"]) == LIMITE and ultima["cursor"] is None,
           "limite_999_tamanos": [len(p["items"]) for p in p999], "limite_999_cursor_final": p999[-1]["cursor"],
           "limite_1000_tamanos": [len(p["items"]) for p in p1000], "limite_1000_cursor_final": p1000[-1]["cursor"],
           "equipo_vacio_items": vacio["items"], "equipo_vacio_cursor": vacio["cursor"]})


# ---------------------------------------------------------------------------
# Fallo declarado: cursor invalido, sin llegar a consultar
# ---------------------------------------------------------------------------

def test_fallo_declarado_cursor_invalido_no_consulta_dynamodb(store, datos, monkeypatch):
    valido = store.paginar_lecturas_equipo(EQUIPO_PAG, limite=LIMITE)["cursor"]
    estado = _decodificar(valido)
    sin_v = {k: v for k, v in estado.items() if k != "v"}
    intentos = {
        "texto_alterado": (EQUIPO_PAG, {}, valido + "$"),
        "json_sin_campo_v": (EQUIPO_PAG, {}, _codificar(sin_v)),
        "otra_version": (EQUIPO_PAG, {}, _codificar({**estado, "v": 2})),
        "clave_de_otro_equipo": (EQUIPO_PAG, {}, _codificar({**estado, "ultima": {**estado["ultima"], "PK": "EQ#otro#cpu"}})),
        "cursor_de_otro_equipo": (EQUIPO_OTRO, {}, valido),
        "hasta_distinto": (EQUIPO_PAG, {"hasta": HASTA - timedelta(hours=1)}, valido),
    }

    llamadas = []
    original = store.table.query

    def espia(**kwargs):
        llamadas.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(store.table, "query", espia)
    resultados = {}
    for nombre, (equipo, extra, cursor) in intentos.items():
        try:
            store.paginar_lecturas_equipo(equipo, cursor=cursor, limite=LIMITE, **extra)
            resultados[nombre] = "sin_error"
        except CursorInvalido:
            resultados[nombre] = "CursorInvalido"

    _caso("M08-F1", "test_fallo_declarado_cursor_invalido_no_consulta_dynamodb",
          "un cursor alterado, de otro equipo, de otra version o con hasta distinto da CursorInvalido y DynamoDB no recibe ninguna Query",
          {"resultados": {nombre: "CursorInvalido" for nombre in intentos}, "queries_a_dynamodb": 0},
          {"resultados": resultados, "queries_a_dynamodb": len(llamadas)},
          codigo_error="CursorInvalido")


# ---------------------------------------------------------------------------
# Contraste: la linea base por desplazamiento si duplica
# ---------------------------------------------------------------------------

def test_contraste_desplazamiento_duplica_y_el_cursor_no(store, insercion):
    ids_base = [i for pagina in insercion["base"] for i in pagina]
    duplicadas_base, saltadas_base = _conteo(ids_base, insercion["esperadas"])
    duplicadas_cursor, _ = _conteo(_ids(insercion["paginas"]), insercion["esperadas"])
    PAGINACION.update(duplicadas_linea_base=duplicadas_base, saltadas_linea_base=saltadas_base)
    _caso("M08-C1", "test_contraste_desplazamiento_duplica_y_el_cursor_no",
          "con las mismas 50 inserciones, paginar por desplazamiento sobre ultimas_lecturas repite lecturas y el cursor no",
          {"insertadas_durante": 50, "duplicadas_linea_base": 50, "duplicadas_cursor": 0},
          {"insertadas_durante": len(insercion["nuevos"]), "duplicadas_linea_base": duplicadas_base,
           "duplicadas_cursor": duplicadas_cursor})


# ---------------------------------------------------------------------------
# Relleno: backfill repetible y Q5 ve las lecturas rellenadas
# ---------------------------------------------------------------------------

def test_relleno_backfill_repetible_y_q5_ve_las_lecturas(store):
    for lectura_id in range(1, 31):
        item = store.build_lectura(lectura_id, _lectura(EQUIPO_RELLENO, lectura_id, INICIO + timedelta(minutes=lectura_id)),
                                   fuente="fixture")
        item.pop("GSI2PK", None)
        store.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")

    sin_gsi2 = Attr("equipo_codigo").eq(EQUIPO_RELLENO) & Attr("GSI2PK").not_exists()
    del_equipo = Attr("equipo_codigo").eq(EQUIPO_RELLENO)

    antes = len(_escanear(store, sin_gsi2))
    en_seco = store.backfill_gsi2(dry_run=True)
    tras_seco = len(_escanear(store, sin_gsi2))
    primera = store.backfill_gsi2(dry_run=False)
    tras_primera = len(_escanear(store, sin_gsi2))
    visibles = sorted(_ids(_recorrer(store, EQUIPO_RELLENO)))
    foto1 = sorted(_escanear(store, del_equipo), key=lambda i: (i["PK"], i["SK"]))
    segunda = store.backfill_gsi2(dry_run=False)
    foto2 = sorted(_escanear(store, del_equipo), key=lambda i: (i["PK"], i["SK"]))

    PAGINACION["relleno"] = _plano({"dry_run": en_seco, "primera": primera, "segunda": segunda})
    _caso("M08-R1", "test_relleno_backfill_repetible_y_q5_ve_las_lecturas",
          "30 lecturas sin GSI2PK: el dry-run no escribe, la primera corrida las rellena y Q5 las ve, la segunda no cambia nada",
          {"sin_gsi2_antes": 30, "sin_gsi2_tras_dry_run": 30, "sin_gsi2_tras_primera": 0,
           "q5_ve_las_30": True, "segunda_corrida_sin_cambios": True, "sin_gsi2_tras_segunda": 0},
          {"sin_gsi2_antes": antes, "sin_gsi2_tras_dry_run": tras_seco, "sin_gsi2_tras_primera": tras_primera,
           "q5_ve_las_30": visibles == list(range(1, 31)), "segunda_corrida_sin_cambios": foto1 == foto2,
           "sin_gsi2_tras_segunda": len(_escanear(store, sin_gsi2))})
