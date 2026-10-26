"""Pruebas M07 (migracion v1 -> v2 repetible y reversible) contra DynamoDB Local.

Usa el events_store.py real, no un emulador. Cada prueba aisla las tres tablas
(eventos, auditoria y respaldo) con nombres propios y las borra al terminar.
Fixture sintetico: 120 lecturas (40 v1 y 80 v2), semilla 42. Cada prueba deja un
caso en CASOS; si existe M07_CASOS_PATH, al terminar el modulo se vuelcan junto
con las mediciones para que scripts/build_m07_artifact.py arme el artifact.
Sin credenciales.
"""

import json
import os
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from boto3.dynamodb.conditions import Key

os.environ.setdefault("DYNAMODB_ENDPOINT", f"http://localhost:{os.environ.get('DYNAMODB_PORT', '8000')}")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("DYNAMODB_LOCAL", "true")

from src.events_store import (
    EventsStore,
    MIGRACION_ID,
    MigracionInterrumpida,
    VersionDesconocida,
    normalizar_lectura,
)

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "db" / "nosql" / "audit-table.json"
EQUIPO = "fixture-m07"
EQUIPO_V3 = "fixture-m07-v3"
METRICAS = ("cpu", "memoria")
CAMPOS_NEGOCIO = ("PK", "SK", "entidad", "lectura_id", "equipo_codigo", "metrica", "unidad", "valor", "medido_en")
CAMPOS_NUEVOS = ("schemaVersion", "fuente", "registrado_en", "migracion_id")
DIA_INICIO = datetime.now(timezone.utc).date().isoformat()
SECRETOS = re.compile(r"AKIA[0-9A-Z]{16}|://[^\s/:]+:[^\s@]+@|password|secret|token|bearer", re.I)

CASOS: list[dict] = []
MIGRACION: dict = {}


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
    destino = os.environ.get("M07_CASOS_PATH")
    if destino:
        Path(destino).write_text(
            json.dumps({"casos": CASOS, "migracion": MIGRACION}, default=str, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


@pytest.fixture
def store():
    corrida = uuid.uuid4().hex[:12]
    s = EventsStore(
        table_name=f"m07_eventos_{corrida}",
        audit_table_name=f"m07_auditoria_{corrida}",
        migration_table_name=f"m07_respaldo_{corrida}",
    )
    s.ensure_table()
    yield s
    for nombre in (s.table_name, s.audit_table_name, s.migration_table_name):
        s.resource.meta.client.delete_table(TableName=nombre)


def _poblar(s, con_v3=False):
    """120 lecturas: ids 1-40 v1 (crudas) y 41-120 v2. Opcional: 1 documento v3 aparte."""
    rng = random.Random(42)
    inicio = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for n in range(120):
        lectura = dict(equipo_codigo=EQUIPO, metrica=METRICAS[n % 2], unidad="porcentaje",
                       valor=rng.randint(0, 100), medido_en=inicio + timedelta(seconds=n))
        if n < 40:
            item = s.build_lectura(n + 1, lectura, fuente="fixture")
            for campo in ("schemaVersion", "fuente", "registrado_en"):
                del item[campo]
            s.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        else:
            s.put_lectura(n + 1, lectura, fuente="fixture")
    if not con_v3:
        return None
    medido_en = inicio + timedelta(seconds=500)
    lectura = dict(equipo_codigo=EQUIPO_V3, metrica="cpu", unidad="porcentaje", valor=50, medido_en=medido_en)
    item = s.build_lectura(121, lectura, fuente="fixture")
    item["schemaVersion"] = 3
    s.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return {"item": item, "medido_en": medido_en}


def _crudos(s):
    """Items tal cual estan en la tabla de eventos (sin upcast)."""
    items, cursor = {}, None
    while True:
        argumentos = dict(ConsistentRead=True)
        if cursor:
            argumentos["ExclusiveStartKey"] = cursor
        pagina = s.table.scan(**argumentos)
        for item in pagina["Items"]:
            items[(item["PK"], item["SK"])] = item
        cursor = pagina.get("LastEvaluatedKey")
        if not cursor:
            return items


def _respaldos(s):
    items, cursor = [], None
    while True:
        argumentos = dict(KeyConditionExpression=Key("PK").eq(f"MIG#{MIGRACION_ID}"), ConsistentRead=True)
        if cursor:
            argumentos["ExclusiveStartKey"] = cursor
        pagina = s.migration_table.query(**argumentos)
        items += pagina["Items"]
        cursor = pagina.get("LastEvaluatedKey")
        if not cursor:
            return items


def _negocio(item):
    return {campo: item[campo] for campo in CAMPOS_NEGOCIO}


def _q1(s, equipo=EQUIPO):
    """Q1 sobre las dos metricas del fixture, reducido a campos de negocio."""
    filas = []
    for metrica in METRICAS:
        filas += [_negocio(i) for i in s.iter_lecturas(equipo, metrica, consistent_read=True)]
    return sorted(filas, key=lambda fila: (fila["PK"], fila["SK"]))


def _es_migrada(item):
    return (item.get("fuente") == "migrada" and item.get("migracion_id") == MIGRACION_ID
            and item.get("schemaVersion") == 2 and "registrado_en" in item)


def _difs(actual, esperado):
    return sum(actual.get(c) != esperado.get(c) or (c in actual) != (c in esperado)
               for c in set(actual) | set(esperado))


def _auditorias(s):
    items = []
    for dia in sorted({DIA_INICIO, datetime.now(timezone.utc).date().isoformat()}):
        cursor = None
        while True:
            pagina = s.query_auditoria_dia(dia, limite=1000, exclusive_start_key=cursor)
            items += pagina["Items"]
            cursor = pagina.get("LastEvaluatedKey")
            if not cursor:
                break
    return items


# ---------------------------------------------------------------------------
# Caso normal: migra las 40 v1, deja intactas las 80 v2, la lectura no cambia
# ---------------------------------------------------------------------------

def test_normal_migra_las_v1_y_deja_intactas_las_v2(store):
    _poblar(store)
    q1_antes = _q1(store)
    crudos_antes = _crudos(store)
    v2_antes = {k: v for k, v in crudos_antes.items() if "schemaVersion" in v}

    r = store.migrar_lecturas_v1(lote=25)

    crudos_despues = _crudos(store)
    q1_despues = _q1(store)

    _caso("M07-N1", "test_normal_migra_las_v1_y_deja_intactas_las_v2",
          "migra las 40 v1, las 80 v2 quedan intactas y Q1 devuelve lo mismo antes y despues",
          {"escaneadas": 120, "migradas": 40, "ya_v2": 80, "version_desconocida": 0, "fallidas": 0,
           "migradas_en_tabla": 40, "v2_intactas": 80, "respaldos": 40, "q1_total": 120, "q1_igual": True},
          {"escaneadas": int(r["escaneadas"]), "migradas": int(r["migradas"]), "ya_v2": int(r["ya_v2"]),
           "version_desconocida": int(r["version_desconocida"]), "fallidas": int(r["fallidas"]),
           "migradas_en_tabla": sum(_es_migrada(v) for v in crudos_despues.values()),
           "v2_intactas": sum(crudos_despues.get(k) == v for k, v in v2_antes.items()),
           "respaldos": len(_respaldos(store)), "q1_total": len(q1_despues), "q1_igual": q1_antes == q1_despues})


# ---------------------------------------------------------------------------
# Limite 1: repetir la migracion no cambia nada
# ---------------------------------------------------------------------------

def test_limite_segunda_corrida_no_cambia_nada(store):
    _poblar(store)
    store.migrar_lecturas_v1(lote=25)
    crudos_1, respaldos_1 = _crudos(store), _respaldos(store)

    r2 = store.migrar_lecturas_v1(lote=25)

    _caso("M07-L1", "test_limite_segunda_corrida_no_cambia_nada",
          "la segunda corrida migra 0 y no modifica ni la tabla ni los respaldos",
          {"migradas": 0, "migrables": 0, "ya_v2": 120, "fallidas": 0,
           "tabla_igual": True, "respaldos_iguales": True, "respaldos": 40},
          {"migradas": int(r2["migradas"]), "migrables": int(r2["migrables"]), "ya_v2": int(r2["ya_v2"]),
           "fallidas": int(r2["fallidas"]), "tabla_igual": _crudos(store) == crudos_1,
           "respaldos_iguales": _respaldos(store) == respaldos_1, "respaldos": len(respaldos_1)})


# ---------------------------------------------------------------------------
# Limite 2: interrupcion a la mitad de un lote y reanudacion
# ---------------------------------------------------------------------------

def test_limite_interrupcion_y_reanudacion_sin_duplicar(store):
    _poblar(store)

    with pytest.raises(MigracionInterrumpida) as exc:
        store.migrar_lecturas_v1(lote=25, detener_tras=30)
    parcial = exc.value.reporte
    crudos_parcial = _crudos(store)
    respaldos_parcial = len(_respaldos(store))

    r2 = store.migrar_lecturas_v1(lote=25)
    crudos_final = _crudos(store)
    respaldos_final = _respaldos(store)
    MIGRACION["reanudacion_migradas_total"] = int(parcial["migradas"]) + int(r2["migradas"])

    _caso("M07-L2", "test_limite_interrupcion_y_reanudacion_sin_duplicar",
          "si la migracion se interrumpe a la mitad, reanudar termina las 40 sin duplicar nada",
          {"parcial_migradas": 30, "v1_restantes_tras_interrupcion": 10, "respaldos_tras_interrupcion": 30,
           "reanudacion_migradas": 10, "reanudacion_ya_v2": 110, "migradas_total": 40,
           "respaldos_finales": 40, "respaldos_sk_unicos": 40, "items_en_tabla": 120, "v1_restantes_final": 0},
          {"parcial_migradas": int(parcial["migradas"]),
           "v1_restantes_tras_interrupcion": sum("schemaVersion" not in v for v in crudos_parcial.values()),
           "respaldos_tras_interrupcion": respaldos_parcial,
           "reanudacion_migradas": int(r2["migradas"]), "reanudacion_ya_v2": int(r2["ya_v2"]),
           "migradas_total": int(parcial["migradas"]) + int(r2["migradas"]),
           "respaldos_finales": len(respaldos_final), "respaldos_sk_unicos": len({b["SK"] for b in respaldos_final}),
           "items_en_tabla": len(crudos_final),
           "v1_restantes_final": sum("schemaVersion" not in v for v in crudos_final.values())})


# ---------------------------------------------------------------------------
# Fallo declarado: schemaVersion 3 se omite, se reporta y no se toca
# ---------------------------------------------------------------------------

def test_fallo_declarado_schema_version_3_se_reporta_y_no_se_modifica(store):
    v3 = _poblar(store, con_v3=True)
    clave = (v3["item"]["PK"], v3["item"]["SK"])

    r = store.migrar_lecturas_v1(lote=25)

    with pytest.raises(VersionDesconocida) as exc:
        store.get_lectura(EQUIPO_V3, "cpu", v3["medido_en"], 121)

    _caso("M07-F1", "test_fallo_declarado_schema_version_3_se_reporta_y_no_se_modifica",
          "un documento con schemaVersion 3 se omite, se reporta como VersionDesconocida y queda identico",
          {"escaneadas": 121, "migradas": 40, "ya_v2": 80, "version_desconocida": 1,
           "errores": [{"codigo_error": "VersionDesconocida"}], "v3_intacto": True,
           "respaldos": 40, "lectura_directa": "VersionDesconocida"},
          {"escaneadas": int(r["escaneadas"]), "migradas": int(r["migradas"]), "ya_v2": int(r["ya_v2"]),
           "version_desconocida": int(r["version_desconocida"]), "errores": r["errores"],
           "v3_intacto": _crudos(store)[clave] == v3["item"], "respaldos": len(_respaldos(store)),
           "lectura_directa": type(exc.value).__name__},
          codigo_error="VersionDesconocida")


# ---------------------------------------------------------------------------
# Transicion: con la migracion a medias, la version anterior sigue legible
# ---------------------------------------------------------------------------

def test_transicion_con_migracion_a_medias_todo_sigue_legible(store):
    _poblar(store)
    q1_antes = _q1(store)

    with pytest.raises(MigracionInterrumpida):
        store.migrar_lecturas_v1(lote=25, detener_tras=30)

    crudos = list(_crudos(store).values())
    v1 = next(v for v in crudos if "schemaVersion" not in v)
    migrada = next(v for v in crudos if v.get("fuente") == "migrada")
    nativa = next(v for v in crudos if v.get("fuente") == "fixture")
    n1, n2, n3 = (normalizar_lectura(x) for x in (v1, migrada, nativa))
    q1_durante = _q1(store)

    sin_id = lambda n: set(n) - {"migracion_id"}  # noqa: E731
    _caso("M07-T1", "test_transicion_con_migracion_a_medias_todo_sigue_legible",
          "con la migracion a medias, v1, v2 nativa y v2 migrada se leen al mismo formato",
          {"claves_comunes_iguales": True, "solo_la_migrada_trae_migracion_id": True,
           "versiones": [1, 2, 2], "fuentes": ["desconocida", "fixture", "migrada"],
           "q1_total": 120, "q1_igual_a_antes": True},
          {"claves_comunes_iguales": sin_id(n1) == sin_id(n2) == sin_id(n3),
           "solo_la_migrada_trae_migracion_id": "migracion_id" in n2 and "migracion_id" not in n1
                                                and "migracion_id" not in n3,
           "versiones": [int(n["schemaVersion"]) for n in (n1, n2, n3)],
           "fuentes": sorted(n["fuente"] for n in (n1, n2, n3)),
           "q1_total": len(q1_durante), "q1_igual_a_antes": q1_durante == q1_antes})


# ---------------------------------------------------------------------------
# Rollback: las 40 v1 vuelven identicas al respaldo y las 80 v2 no se tocan
# ---------------------------------------------------------------------------

def test_rollback_deja_las_v1_identicas_y_las_v2_intactas(store):
    _poblar(store, con_v3=True)
    q1_antes = _q1(store)
    crudos_antes = _crudos(store)
    v1_originales = {k: v for k, v in crudos_antes.items() if "schemaVersion" not in v}
    v2_originales = {k: v for k, v in crudos_antes.items() if v.get("schemaVersion") == 2}

    r1 = store.migrar_lecturas_v1(lote=25)
    q1_migrada = _q1(store)
    r2 = store.migrar_lecturas_v1(lote=25)
    respaldos = len(_respaldos(store))
    rb1 = store.revertir_migracion(MIGRACION_ID)
    rb2 = store.revertir_migracion(MIGRACION_ID)

    crudos_final = _crudos(store)
    q1_final = _q1(store)
    obtenido = {
        "escaneadas": int(r1["escaneadas"]), "migradas": int(r1["migradas"]), "ya_v2": int(r1["ya_v2"]),
        "version_desconocida": int(r1["version_desconocida"]),
        "segunda_corrida_migradas": int(r2["migradas"]), "respaldos_creados": respaldos,
        "revertidas": int(rb1["revertidas"]), "no_revertible": int(rb1["no_revertible"]),
        "segunda_corrida_revertidas": int(rb2["revertidas"]), "segunda_corrida_escaneadas": int(rb2["escaneadas"]),
        "diferencias_campo_tras_rollback": sum(_difs(crudos_final.get(k, {}), v) for k, v in v1_originales.items()),
        "v1_restauradas_sin_campos_nuevos": sum(
            not any(c in crudos_final.get(k, {}) for c in CAMPOS_NUEVOS) for k in v1_originales),
        "v2_originales_intactas": sum(crudos_final.get(k) == v for k, v in v2_originales.items()),
        "tabla_identica_a_la_inicial": crudos_final == crudos_antes,
        "respaldos_tras_rollback": len(_respaldos(store)),
        "consultas_iguales_antes_despues": q1_antes == q1_migrada == q1_final,
    }
    MIGRACION.update({k: obtenido[k] for k in (
        "escaneadas", "migradas", "ya_v2", "version_desconocida", "segunda_corrida_migradas", "respaldos_creados",
        "revertidas", "segunda_corrida_revertidas", "diferencias_campo_tras_rollback",
        "v2_originales_intactas", "consultas_iguales_antes_despues")})
    MIGRACION["duracion_ms"] = r1["duracion_ms"]
    MIGRACION["duracion_rollback_ms"] = rb1["duracion_ms"]

    _caso("M07-R1", "test_rollback_deja_las_v1_identicas_y_las_v2_intactas",
          "revertir deja las 40 v1 iguales campo por campo al respaldo, las 80 v2 intactas y la segunda reversa no hace nada",
          {"escaneadas": 121, "migradas": 40, "ya_v2": 80, "version_desconocida": 1,
           "segunda_corrida_migradas": 0, "respaldos_creados": 40, "revertidas": 40, "no_revertible": 0,
           "segunda_corrida_revertidas": 0, "segunda_corrida_escaneadas": 0,
           "diferencias_campo_tras_rollback": 0, "v1_restauradas_sin_campos_nuevos": 40,
           "v2_originales_intactas": 80, "tabla_identica_a_la_inicial": True,
           "respaldos_tras_rollback": 0, "consultas_iguales_antes_despues": True},
          obtenido)


# ---------------------------------------------------------------------------
# Auditoria: un registro por lote, sin secretos
# ---------------------------------------------------------------------------

def test_auditoria_un_registro_por_lote_sin_secretos(store):
    _poblar(store)
    r = store.migrar_lecturas_v1(lote=25)
    rb = store.revertir_migracion(MIGRACION_ID)

    registros = _auditorias(store)
    lotes_migrar = [a for a in registros if a.get("operacion") == "migrar_lecturas"
                    and a.get("clave_afectada") == f"LOTE#{r['corrida_id']}"]
    lotes_revertir = [a for a in registros if a.get("operacion") == "revertir_migracion"
                      and a.get("clave_afectada") == f"LOTE#{rb['corrida_id']}"]
    lotes = lotes_migrar + lotes_revertir

    campos = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))["fields"]
    permitidos = set(campos["required"]) | set(campos["optional"]) | {"PK", "SK"}
    endpoint = os.environ["DYNAMODB_ENDPOINT"]
    campos_extra = sorted({c for a in lotes for c in set(a) - permitidos})
    con_secreto = [v for a in lotes for v in a.values()
                   if isinstance(v, str) and (SECRETOS.search(v) or endpoint in v)]

    _caso("M07-A1", "test_auditoria_un_registro_por_lote_sin_secretos",
          "cada lote migrado o revertido deja su registro con conteo y sin valor, payload, endpoint ni campos fuera de la lista blanca",
          {"hay_lotes": True, "lotes_migrar_suman": 40, "lotes_revertir_suman": 40,
           "actor_roles": ["migracion"], "motivos": ["m07_v1_a_v2"], "resultados": ["ok"],
           "sin_valor_ni_payload": True, "campos_extra": [], "valores_con_secreto": []},
          {"hay_lotes": len(lotes_migrar) > 0 and len(lotes_revertir) > 0,
           "lotes_migrar_suman": int(sum(a["conteo"] for a in lotes_migrar)),
           "lotes_revertir_suman": int(sum(a["conteo"] for a in lotes_revertir)),
           "actor_roles": sorted({a["actor_rol"] for a in lotes}),
           "motivos": sorted({a["motivo"] for a in lotes}),
           "resultados": sorted({a["resultado"] for a in lotes}),
           "sin_valor_ni_payload": all(not ({"valor", "payload", "documento"} & set(a)) for a in lotes),
           "campos_extra": campos_extra, "valores_con_secreto": con_secreto})