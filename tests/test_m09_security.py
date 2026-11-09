"""M09: pruebas de seguridad NoSQL y auditoría (ADR-009) contra DynamoDB Local.

Cubre: caso normal, dos casos límite, fallo declarado y la regresión del rol
autodeclarado. Los eventos se leen directo de la tabla de auditoría (oráculo
de la prueba), no a través del almacén, para no generar eventos de lectura.
Si existe M09_CASOS_PATH, al terminar el módulo se vuelca un reporte para
scripts/build_m09_artifact.py.
"""

import inspect
import itertools
import json
import os
import re
import uuid
from pathlib import Path

import pytest

from src.events_store import EventsStore
from src.nosql_access import AccesoDenegado, AuditoriaInvalida, Principal, autorizar, sanear_evento

WRITER = ("svc-api", "cdrl_writer")
OPS = ("ops-01", "cdrl_ops")
AUDITOR = ("aud-01", "cdrl_auditor")

COLECCIONES = ("lecturas", "alertas", "auditoria", "respaldo_migracion")
OPERACIONES = ("get", "query", "put", "update", "delete", "scan")

# Oráculo independiente de access-policy.json (ADR-009): rol -> (actores, permisos).
ESPERADO = {
    "cdrl_writer": (("svc-api", "svc-carga"), {"lecturas": {"put"}, "alertas": {"put"}}),
    "cdrl_ops": (("ops-01", "ops-02"), {"lecturas": {"get", "query"},
                                         "alertas": {"get", "query", "update", "delete"}}),
    "cdrl_auditor": (("aud-01",), {"auditoria": {"query"}}),
    "migracion": (("job-migracion", "job-backfill"),
                  {"lecturas": {"get", "query", "scan", "put", "update"},
                   "respaldo_migracion": {"get", "query", "put", "delete"}}),
    "recuperacion": (("job-recuperacion",), {"lecturas": {"get", "query", "put"}}),
    "simulacro": (("job-simulacro",), {"lecturas": {"delete"}}),
}

_ids = itertools.count(9000)
REPORTE = {"matriz": []}
PATRON_SECRETOS = re.compile(
    r"AKIA[0-9A-Z]{16}|://[^\s/:]+:[^\s@]+@|password\s*=|bearer\s+|secret|token", re.I)


# ---------------------------------------------------------------- utilidades
@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setenv("CDRL_ENTORNO", "local")
    monkeypatch.setenv("DYNAMODB_LOCAL", "true")
    monkeypatch.setenv("AWS_REGION", os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1")


@pytest.fixture(scope="module")
def tablas():
    """Tablas aisladas por módulo; devuelve una fábrica de almacenes por principal."""
    if not os.getenv("DYNAMODB_ENDPOINT"):
        pytest.fail("Define DYNAMODB_ENDPOINT (DynamoDB Local) para correr las pruebas M09.")
    sufijo = uuid.uuid4().hex[:8]
    nombres = dict(table_name=f"m09_eventos_{sufijo}", audit_table_name=f"m09_auditoria_{sufijo}",
                   migration_table_name=f"m09_respaldo_{sufijo}")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CDRL_ENTORNO", "local")
        mp.setenv("DYNAMODB_LOCAL", "true")
        mp.setenv("AWS_REGION", os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1")

        def crear(actor, rol):
            return EventsStore(principal=Principal(actor, rol), **nombres)

        crear(*OPS).ensure_table()
        yield crear

        destino = os.environ.get("M09_CASOS_PATH")
        if destino:
            audit = crear(*OPS).audit_table
            items = [i for i in _todos(audit) if str(i.get("PK", "")).startswith("AUD#")]
            conteo = {}
            for i in items:
                conteo[i["resultado"]] = conteo.get(i["resultado"], 0) + 1
            coincidencias = [i["SK"] for i in items if PATRON_SECRETOS.search(json.dumps(i, default=str))]
            REPORTE["eventos_por_resultado"] = conteo
            REPORTE["barrido_secretos"] = {"eventos_revisados": len(items),
                                           "coincidencias": len(coincidencias),
                                           "patron": PATRON_SECRETOS.pattern}
            Path(destino).write_text(json.dumps(REPORTE, indent=2, ensure_ascii=False), encoding="utf-8")

        client = crear(*OPS).resource.meta.client
        for nombre in nombres.values():
            client.delete_table(TableName=nombre)


def _todos(tabla):
    items, kwargs = [], {}
    while True:
        pagina = tabla.scan(**kwargs)
        items += pagina["Items"]
        if "LastEvaluatedKey" not in pagina:
            return items
        kwargs["ExclusiveStartKey"] = pagina["LastEvaluatedKey"]


def eventos(store, **campos):
    """Eventos de auditoría (PK AUD#) que cumplen todos los campos dados."""
    return [i for i in _todos(store.audit_table)
            if str(i.get("PK", "")).startswith("AUD#") and all(i.get(k) == v for k, v in campos.items())]


def _clave(n):
    return f"LECTURA#{n:019d}#UMBRAL#{n:019d}"


def _key(n):
    return {"PK": f"LECTURA#{n:019d}", "SK": f"UMBRAL#{n:019d}"}


def _alerta(tablas):
    """Crea una alerta abierta con el rol writer y devuelve su id (lectura = umbral)."""
    n = next(_ids)
    tablas(*WRITER).put_alerta(lectura_id=n, umbral_id=n, estado="abierta", severidad="critica",
                               medido_en="2026-11-01T12:00:00Z")
    return n


BASE = dict(operacion="delete_alerta", actor="ops-01", actor_rol="cdrl_ops", accion="alertas.delete",
            recurso=f"alertas/{_clave(1)}", entidad="alerta", clave_afectada=_clave(1), resultado="ok")


# ------------------------------------------------------------------- pruebas
def test_normal_ops_reconoce_alerta_y_el_evento_trae_los_cuatro_campos(tablas):
    n = _alerta(tablas)
    ops = tablas(*OPS)
    ops.actualizar_estado_alerta(n, n, "reconocida")
    assert ops.get_alerta(n, n)["estado"] == "reconocida"
    encontrados = eventos(ops, operacion="actualizar_estado_alerta", clave_afectada=_clave(n))
    assert len(encontrados) == 1
    ev = encontrados[0]
    assert (ev["actor"], ev["accion"], ev["recurso"], ev["resultado"]) == (
        "ops-01", "alertas.update", f"alertas/{_clave(n)}", "ok")


def test_limite_1_permiso_en_la_coleccion_pero_no_en_la_operacion(tablas):
    n = _alerta(tablas)
    writer, auditor = tablas(*WRITER), tablas(*AUDITOR)

    # cdrl_writer puede escribir alertas, pero no borrarlas.
    antes_item = writer.table.get_item(Key=_key(n))["Item"]
    antes_eventos = len(eventos(writer))
    with pytest.raises(AccesoDenegado):
        writer.delete_alerta(n, n)
    assert writer.table.get_item(Key=_key(n))["Item"] == antes_item
    assert len(eventos(writer)) == antes_eventos + 1
    assert len(eventos(writer, actor="svc-api", accion="alertas.delete", resultado="denegada")) >= 1
    assert eventos(writer, actor="svc-api", accion="alertas.delete", resultado="ok") == []

    # cdrl_auditor consulta auditoría, pero no escribe en ella.
    antes_eventos = len(eventos(auditor))
    with pytest.raises(AccesoDenegado):
        auditor.registrar_auditoria(dict(BASE))
    assert len(eventos(auditor)) == antes_eventos + 1
    assert len(eventos(auditor, actor="aud-01", accion="auditoria.put", resultado="denegada")) == 1


def test_limite_2_lectura_cruzada_scan_y_principales_invalidos(tablas, monkeypatch):
    writer, ops, auditor = tablas(*WRITER), tablas(*OPS), tablas(*AUDITOR)

    # Lecturas cruzadas y scan, a través del almacén: niegan y dejan evento.
    intentos = [
        (writer.query_lecturas, ("eq-01", "cpu")),
        (auditor.query_lecturas, ("eq-01", "cpu")),
        (ops.query_auditoria_dia, ("2026-11-01",)),
        (writer.query_auditoria_dia, ("2026-11-01",)),
        (ops.scan_lecturas, ()),
        (writer.scan_lecturas, ()),
    ]
    antes = len(eventos(ops, resultado="denegada"))
    for funcion, argumentos in intentos:
        with pytest.raises(AccesoDenegado):
            funcion(*argumentos)
    assert len(eventos(ops, resultado="denegada")) == antes + len(intentos)

    # Scan: prohibido para todos salvo migracion sobre lecturas (matriz completa).
    for rol, (actores, _) in ESPERADO.items():
        for actor in actores:
            for coleccion in COLECCIONES:
                if (rol, coleccion) == ("migracion", "lecturas"):
                    autorizar(Principal(actor, rol), coleccion, "scan")
                else:
                    with pytest.raises(AccesoDenegado):
                        autorizar(Principal(actor, rol), coleccion, "scan")

    # Principal sin rol, con rol inventado o con actor ajeno al rol.
    for actor, rol in [("", ""), ("ops-01", ""), ("ops-01", "cdrl_admin"),
                       ("svc-api", "cdrl_ops"), ("ops-99", "cdrl_ops")]:
        with pytest.raises(AccesoDenegado):
            Principal(actor, rol)
    with pytest.raises(AccesoDenegado):
        autorizar(None, "lecturas", "get")

    # Sin variables CDRL_ACTOR/CDRL_ROL el almacén ni siquiera se construye.
    monkeypatch.delenv("CDRL_ACTOR", raising=False)
    monkeypatch.delenv("CDRL_ROL", raising=False)
    with pytest.raises(AccesoDenegado):
        EventsStore(table_name="m09_sin_principal")

    # El rol simulacro no existe fuera de local/ci.
    monkeypatch.setenv("CDRL_ENTORNO", "produccion")
    with pytest.raises(AccesoDenegado):
        Principal("job-simulacro", "simulacro")


@pytest.mark.parametrize("secreto", [
    "AKIA" + "IOSFODNN7EXAMPLE",
    "postgres" + "://usuario:clave@host/db",
    "Bearer " + "abc123xyz",
    "password" + "=hunter2",
], ids=["akia", "cadena_conexion", "bearer", "password"])
def test_fallo_declarado_un_secreto_no_entra_a_la_auditoria(tablas, secreto):
    ops = tablas(*OPS)
    n = _alerta(tablas)

    # Control positivo: el mismo evento sin secreto sí es válido.
    assert EventsStore.build_auditoria(dict(BASE))["actor"] == "ops-01"

    with pytest.raises(AuditoriaInvalida):
        sanear_evento(dict(BASE, motivo=secreto))
    with pytest.raises(AuditoriaInvalida):
        sanear_evento(dict(BASE, password="x"))  # llave prohibida

    antes = len(_todos(ops.audit_table))
    with pytest.raises(AuditoriaInvalida):
        ops._registrar_auditoria(dict(operacion="delete_alerta", entidad="alerta",
                                      clave_afectada=_clave(n), resultado="ok", motivo=secreto))
    assert len(_todos(ops.audit_table)) == antes  # no se escribió nada

    # Barrido final: ninguna coincidencia en toda la tabla de auditoría.
    items = _todos(ops.audit_table)
    assert items, "el barrido no debe ser vacío"
    assert [i for i in items if PATRON_SECRETOS.search(json.dumps(i, default=str))] == []


def test_regresion_el_rol_autodeclarado_ya_no_borra(tablas):
    """Antes: delete_alerta(..., actor_rol='cdrl_writer') borraba y se auditaba como ok."""
    n = _alerta(tablas)
    writer, ops = tablas(*WRITER), tablas(*OPS)

    # El rol ya no es un argumento de la operación.
    assert "actor_rol" not in inspect.signature(EventsStore.delete_alerta).parameters
    with pytest.raises(TypeError):
        ops.delete_alerta(n, n, actor_rol="cdrl_ops")
    assert writer.table.get_item(Key=_key(n)).get("Item") is not None

    # El rol sale del principal autenticado: writer no borra, aunque lo intente.
    with pytest.raises(AccesoDenegado):
        writer.delete_alerta(n, n)
    assert writer.table.get_item(Key=_key(n)).get("Item") is not None
    assert eventos(writer, clave_afectada=_clave(n), resultado="ok", accion="alertas.delete") == []
    assert len(eventos(writer, actor="svc-api", accion="alertas.delete", resultado="denegada")) >= 1


def test_matriz_completa_esperado_contra_obtenido():
    fallos, celdas = [], 0
    for rol, (actores, permisos) in ESPERADO.items():
        for actor in actores:
            for coleccion in COLECCIONES:
                for operacion in OPERACIONES:
                    celdas += 1
                    esperado = operacion in permisos.get(coleccion, set())
                    try:
                        autorizar(Principal(actor, rol), coleccion, operacion)
                        obtenido = True
                    except AccesoDenegado:
                        obtenido = False
                    REPORTE["matriz"].append(dict(actor=actor, rol=rol, coleccion=coleccion,
                                                  operacion=operacion, esperado=esperado, obtenido=obtenido))
                    if obtenido != esperado:
                        fallos.append((actor, rol, coleccion, operacion, esperado, obtenido))
    assert celdas == sum(len(a) for a, _ in ESPERADO.values()) * len(COLECCIONES) * len(OPERACIONES)
    assert fallos == []
    # Nadie actualiza ni borra auditoría.
    for _, (_, permisos) in ESPERADO.items():
        assert not ({"update", "delete"} & permisos.get("auditoria", set()))