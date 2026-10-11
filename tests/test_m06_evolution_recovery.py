"""Pruebas M06 (evolucion, auditoria y recuperacion) contra DynamoDB Local real.

Usa el events_store.py real, no un emulador. Cada prueba deja un caso en
CASOS (id, criterio, esperado, obtenido, codigo_error, resultado); si la
variable M06_CASOS_PATH existe, al terminar el modulo se vuelcan junto con las
mediciones de recuperacion para que scripts/build_m06_artifact.py arme el
artifact. Fixtures sinteticos; sin credenciales.
"""

import importlib.util
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("DYNAMODB_ENDPOINT", f"http://localhost:{os.environ.get('DYNAMODB_PORT', '8000')}")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("DYNAMODB_LOCAL", "true")

from src.events_store import (
    AuditoriaInvalida,
    DocumentoInvalido,
    EventsStore,
    VersionDesconocida,
    normalizar_lectura,
)

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "db" / "nosql" / "audit-table.json"
T0 = "2026-10-01T00:00:00Z"
T1 = "2026-10-01T00:00:01Z"
DIA_INICIO = datetime.now(timezone.utc).date().isoformat()
SECRETOS = re.compile(r"AKIA[0-9A-Z]{16}|://[^\s/:]+:[^\s@]+@|password|secret|token|bearer", re.I)

CASOS: list[dict] = []
RECUPERACION: dict = {}


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
    destino = os.environ.get("M06_CASOS_PATH")
    if destino:
        Path(destino).write_text(
            json.dumps({"casos": CASOS, "recuperacion": RECUPERACION}, default=str, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _nuevo_store(prefijo):
    sufijo = uuid.uuid4().hex[:8]
    s = EventsStore(table_name=f"{prefijo}_{sufijo}", audit_table_name=f"{prefijo}_aud_{sufijo}")
    s.ensure_table()
    return s


def _borrar(s):
    for nombre in (s.table_name, s.audit_table_name):
        s.resource.meta.client.delete_table(TableName=nombre)


@pytest.fixture(scope="module")
def store():
    s = _nuevo_store("cdrl_eventos_test_m06")
    yield s
    _borrar(s)


@pytest.fixture(scope="module")
def store_recuperacion():
    s = _nuevo_store("cdrl_eventos_test_m06_rec")
    yield s
    _borrar(s)


def _lectura(equipo, medido_en=T0, valor=42.5):
    return {"equipo_codigo": equipo, "metrica": "cpu", "unidad": "porcentaje",
            "valor": valor, "medido_en": medido_en}


def _item_crudo(store, lectura_id, equipo, medido_en=T0, version=None):
    """Escribe en crudo. version=None -> v1 (sin schemaVersion/fuente/registrado_en)."""
    item = store.build_lectura(lectura_id, _lectura(equipo, medido_en), fuente="fixture")
    if version is None:
        for campo in ("schemaVersion", "fuente", "registrado_en"):
            del item[campo]
    else:
        item["schemaVersion"] = version
    store.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return item


def _crudo(store, item):
    return store.table.get_item(Key={"PK": item["PK"], "SK": item["SK"]}, ConsistentRead=True).get("Item")


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


def _cargar_recover():
    spec = importlib.util.spec_from_file_location("recover_m06", ROOT / "scripts" / "recover_m06.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


# ---------------------------------------------------------------------------
# Caso normal: v1 y v2 coexisten
# ---------------------------------------------------------------------------

def test_normal_v1_y_v2_se_leen_igual(store):
    equipo = "fixture-m06-normal"
    _item_crudo(store, 1001, equipo, T0)
    store.put_lectura(1002, _lectura(equipo, T1), fuente="fixture")

    v1 = store.get_lectura(equipo, "cpu", T0, 1001)
    v2 = store.get_lectura(equipo, "cpu", T1, 1002)
    q1 = store.query_lecturas(equipo, "cpu", consistent_read=True)["Items"]

    _caso("M06-N1", "test_normal_v1_y_v2_se_leen_igual",
          "versiones antigua y nueva coexisten y se leen en el mismo formato",
          {"mismas_claves": True, "version_v1": 1, "version_v2": 2, "q1_total": 2, "q1_versiones": [1, 2]},
          {"mismas_claves": set(v1) == set(v2), "version_v1": int(v1["schemaVersion"]),
           "version_v2": int(v2["schemaVersion"]), "q1_total": len(q1),
           "q1_versiones": sorted(int(i["schemaVersion"]) for i in q1)})


# ---------------------------------------------------------------------------
# Limite 1: v1 sin el campo nuevo
# ---------------------------------------------------------------------------

def test_limite_v1_sin_fuente_ni_registrado_en(store):
    equipo = "fixture-m06-limite1"
    crudo = _item_crudo(store, 1010, equipo, T0)
    leida = store.get_lectura(equipo, "cpu", T0, 1010)
    en_tabla = _crudo(store, crudo)

    _caso("M06-L1", "test_limite_v1_sin_fuente_ni_registrado_en",
          "una v1 sin fuente ni registrado_en se lee sin error y sin reescribirse",
          {"fuente": "desconocida", "registrado_en": None, "schemaVersion": 1, "tabla_sigue_sin_schemaVersion": True},
          {"fuente": leida["fuente"], "registrado_en": leida["registrado_en"],
           "schemaVersion": int(leida["schemaVersion"]),
           "tabla_sigue_sin_schemaVersion": "schemaVersion" not in en_tabla})


# ---------------------------------------------------------------------------
# Limite 2: restaurar algo que ya existe no lo duplica
# ---------------------------------------------------------------------------

def test_limite_restaurar_existente_no_duplica(store):
    equipo = "fixture-m06-limite2"
    modulo = _cargar_recover()
    store.put_lectura(1020, _lectura(equipo, T0), fuente="fixture")
    original = _crudo(store, store.build_lectura(1020, _lectura(equipo, T0)))

    sobre_existente = modulo.restaurar_faltantes(store, [original])
    total_1 = len(store.query_lecturas(equipo, "cpu", consistent_read=True)["Items"])
    intacta = _crudo(store, original) == original

    store.table.delete_item(Key={"PK": original["PK"], "SK": original["SK"]})
    sobre_faltante = modulo.restaurar_faltantes(store, [original])
    total_2 = len(store.query_lecturas(equipo, "cpu", consistent_read=True)["Items"])

    _caso("M06-L2", "test_limite_restaurar_existente_no_duplica",
          "restaurar un id que ya existe no lo duplica ni lo sobrescribe",
          {"sobre_existente": {"restaurados": 0, "ya_existian": 1}, "total_tras_existente": 1,
           "existente_intacta": True, "sobre_faltante": {"restaurados": 1, "ya_existian": 0}, "total_final": 1},
          {"sobre_existente": sobre_existente, "total_tras_existente": total_1,
           "existente_intacta": intacta, "sobre_faltante": sobre_faltante, "total_final": total_2})


# ---------------------------------------------------------------------------
# Fallos declarados
# ---------------------------------------------------------------------------

def test_fallo_declarado_schema_version_desconocida(store):
    equipo = "fixture-m06-version"
    base = _item_crudo(store, 1030, equipo, T0)
    assert issubclass(VersionDesconocida, DocumentoInvalido)

    codigos = []
    for version in (3, "2", 0, True):
        candidato = dict(base, schemaVersion=version, fuente="api", registrado_en="2026-10-01T00:00:00Z")
        with pytest.raises(VersionDesconocida) as exc:
            normalizar_lectura(candidato)
        codigos.append(type(exc.value).__name__)

    _item_crudo(store, 1031, equipo, T1, version=3)
    with pytest.raises(VersionDesconocida) as exc:
        store.get_lectura(equipo, "cpu", T1, 1031)
    codigos.append(type(exc.value).__name__)

    _caso("M06-F1", "test_fallo_declarado_schema_version_desconocida",
          "schemaVersion 3, '2', 0 o true se rechaza; no se adivina",
          ["VersionDesconocida"] * 5, codigos, codigo_error="VersionDesconocida")


def test_fallo_declarado_auditoria_con_campo_prohibido(store):
    base = dict(operacion="falla_controlada", actor_rol="recuperacion", entidad="lote_lecturas",
                resultado="ok", conteo=1)
    prohibidos = [
        {"valor": 42},
        {"payload": "dato crudo"},
        {"endpoint": os.environ["DYNAMODB_ENDPOINT"]},
        {"motivo": "password_expuesta"},
    ]
    antes = len(_auditorias(store))
    codigos = []
    for extra in prohibidos:
        registro = dict(base, clave_afectada=f"LOTE#{uuid.uuid4().hex}", **extra)
        with pytest.raises(AuditoriaInvalida) as exc:
            store.registrar_auditoria(registro)
        codigos.append(type(exc.value).__name__)
    despues = len(_auditorias(store))

    _caso("M06-F2", "test_fallo_declarado_auditoria_con_campo_prohibido",
          "una auditoria con campo prohibido o texto secreto se rechaza y la tabla no cambia",
          {"codigos": ["AuditoriaInvalida"] * 4, "registros_nuevos": 0},
          {"codigos": codigos, "registros_nuevos": despues - antes}, codigo_error="AuditoriaInvalida")


# ---------------------------------------------------------------------------
# Auditoria de una operacion y append-only
# ---------------------------------------------------------------------------

def test_auditoria_cambiar_alerta_deja_un_registro_sin_datos(store):
    equipo, lectura_id, umbral_id = "fixture-m06-auditoria", 1050, 7
    store.put_lectura(lectura_id, _lectura(equipo, T0), fuente="fixture")
    store.put_alerta(lectura_id=lectura_id, umbral_id=umbral_id, estado="abierta",
                     severidad="advertencia", medido_en=T0)
    store.actualizar_estado_alerta(lectura_id, umbral_id, "reconocida", operacion_id=uuid.uuid4().hex)

    clave = f"LECTURA#{lectura_id:019d}#UMBRAL#{umbral_id:019d}"
    registros = [r for r in _auditorias(store) if r.get("clave_afectada") == clave]
    registro = registros[0] if registros else {}
    texto = json.dumps(registro, default=str)

    _caso("M06-A1", "test_auditoria_cambiar_alerta_deja_un_registro_sin_datos",
          "cambiar una alerta deja exactamente un registro con estado anterior/nuevo y sin valor ni payload",
          {"registros": 1, "operacion": "actualizar_estado_alerta", "estado_anterior": "abierta",
           "estado_nuevo": "reconocida", "resultado": "ok", "sin_valor_ni_payload": True,
           "sin_endpoint": True, "estado_en_tabla": "reconocida"},
          {"registros": len(registros), "operacion": registro.get("operacion"),
           "estado_anterior": registro.get("estado_anterior"), "estado_nuevo": registro.get("estado_nuevo"),
           "resultado": registro.get("resultado"),
           "sin_valor_ni_payload": "valor" not in registro and "payload" not in registro,
           "sin_endpoint": os.environ["DYNAMODB_ENDPOINT"] not in texto,
           "estado_en_tabla": store.get_alerta(lectura_id, umbral_id)["estado"]})


def test_auditoria_append_only_repetir_operacion_id_falla(store):
    operacion_id, clave = uuid.uuid4().hex, f"LOTE#{uuid.uuid4().hex}"
    base = dict(operacion="falla_controlada", actor_rol="recuperacion", entidad="lote_lecturas",
                clave_afectada=clave, resultado="ok", conteo=1, motivo="prueba_append_only",
                operacion_id=operacion_id)
    store.registrar_auditoria(base)

    codigos = []
    for extra in ({}, {"ocurrido_en": "2030-01-01T00:00:00Z"}):
        with pytest.raises(ClientError) as exc:
            store.registrar_auditoria(dict(base, conteo=999, **extra))
        codigos.append(exc.value.response["Error"]["Code"])

    registros = [r for r in _auditorias(store) if r.get("clave_afectada") == clave]
    en_2030 = store.query_auditoria_dia("2030-01-01")["Items"]

    _caso("M06-A2", "test_auditoria_append_only_repetir_operacion_id_falla",
          "repetir un operacion_id (incluso con otra fecha) falla y no sobrescribe",
          {"codigos": ["ConditionalCheckFailedException"] * 2, "registros": 1, "conteo_original": 1, "en_otra_fecha": 0},
          {"codigos": codigos, "registros": len(registros),
           "conteo_original": int(registros[0]["conteo"]) if registros else None, "en_otra_fecha": len(en_2030)},
          codigo_error="ConditionalCheckFailedException")


# ---------------------------------------------------------------------------
# Recuperacion con mediciones
# ---------------------------------------------------------------------------

def test_recuperacion_recupera_fixture_y_mide(store_recuperacion, tmp_path):
    modulo = _cargar_recover()
    r = modulo.recover_m06(store_recuperacion, backup_dir=tmp_path)
    RECUPERACION.update({k: v for k, v in r.items() if k != "respaldo_archivo"})

    lote = [a for a in _auditorias(store_recuperacion) if a.get("clave_afectada") == f"LOTE#{r['lote_id']}"]

    _caso("M06-R1", "test_recuperacion_recupera_fixture_y_mide",
          "el procedimiento recupera lo borrado sin pisar lo posterior y mide el resultado",
          {"esperados": 30, "restaurados": 30, "ya_existian": 0, "faltantes_tras_restaurar": 0,
           "diferencias_campo": 0, "v1_restauradas_sin_schemaVersion": 10, "posteriores_intactas": 10,
           "total_final": 130, "segunda_corrida_restaurados": 0,
           "auditoria": ["falla_controlada", "restaurar_lecturas"]},
          {"esperados": r["esperados"], "restaurados": r["restaurados"], "ya_existian": r["ya_existian"],
           "faltantes_tras_restaurar": r["faltantes_tras_restaurar"], "diferencias_campo": r["diferencias_campo"],
           "v1_restauradas_sin_schemaVersion": r["v1_restauradas_sin_schemaVersion"],
           "posteriores_intactas": r["posteriores_intactas"], "total_final": r["total_final"],
           "segunda_corrida_restaurados": r["segunda_corrida"]["restaurados"],
           "auditoria": sorted(a["operacion"] for a in lote)})


# ---------------------------------------------------------------------------
# El log de auditoria no tiene secretos
# ---------------------------------------------------------------------------

def test_log_de_auditoria_sin_secretos(store, store_recuperacion):
    fields = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))["fields"]
    permitidos = set(fields["required"]) | set(fields["optional"]) | {"PK", "SK"}
    endpoint = os.environ["DYNAMODB_ENDPOINT"]

    registros = _auditorias(store) + _auditorias(store_recuperacion)
    campos_extra, con_secreto = [], []
    for r in registros:
        campos_extra += sorted(set(r) - permitidos)
        for valor in r.values():
            if isinstance(valor, str) and (SECRETOS.search(valor) or endpoint in valor):
                con_secreto.append(valor)

    _caso("M06-S1", "test_log_de_auditoria_sin_secretos",
          "ningun registro de auditoria trae campos fuera de la lista blanca ni patrones de secreto",
          {"hay_registros": True, "campos_extra": [], "valores_con_secreto": []},
          {"hay_registros": len(registros) > 0, "campos_extra": campos_extra, "valores_con_secreto": con_secreto})