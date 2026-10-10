"""Almacén documental M05/M06; PostgreSQL conserva los endpoints existentes.

Las lecturas son append-only. La identidad de una alerta vive en la clave base
(lectura, umbral), porque una condición sobre un atributo secundario no impone
unicidad. GSI1 organiza esas alertas por estado, shard, severidad e instante;
solo la tabla base admite lectura fuerte. Se valida antes de escribir; los
duplicados y fallos de conexión conservan sus códigos de boto3.
"""

import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Iterator
from uuid import uuid4
from urllib.parse import urlsplit

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import DYNAMODB_CONTEXT
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from pydantic import TypeAdapter, ValidationError

from src.schemas import ConsultaLecturas, LecturaBase, LecturaEntrada

METRICAS = ("cpu", "memoria", "disco_libre", "latencia", "tasa_error")
ALERT_SHARDS = 4
GSI_NAME = "GSI1"
MAX_ITEM_BYTES = 409600
INDEXES_PATH = Path(__file__).resolve().parents[1] / "db" / "nosql" / "indexes.json"
AUDIT_PATH = INDEXES_PATH.with_name("audit-table.json")
_ESTADOS = ("abierta", "reconocida", "cerrada")
_SEVERIDAD = {"advertencia": 1, "critica": 2}
_LECTURA = TypeAdapter(LecturaEntrada)
_TRANSICIONES = {"abierta": {"reconocida", "cerrada"}, "reconocida": {"cerrada"}, "cerrada": set()}


class DocumentoInvalido(ValueError):
    """El documento rompe el contrato; no se efectuó la escritura."""


class DocumentoDemasiadoGrande(DocumentoInvalido):
    """Nombres y valores del item exceden los 409.600 bytes."""


class VersionDesconocida(DocumentoInvalido):
    """Versión de lectura que este cliente no sabe interpretar."""


class AuditoriaInvalida(DocumentoInvalido):
    """Registro fuera del contrato seguro de auditoría."""


def normalizar_lectura(item: dict) -> dict:
    """Upcast puro: nunca reescribe el documento persistido."""
    result = dict(item)
    if "schemaVersion" not in item:
        result.update(schemaVersion=1, fuente="desconocida", registrado_en=None)
    elif (isinstance(item["schemaVersion"], bool)
          or not isinstance(item["schemaVersion"], (int, Decimal))
          or item["schemaVersion"] != 2):
        raise VersionDesconocida("schemaVersion no soportada.")
    else:
        if item.get("fuente") not in ("api", "lote", "fixture"):
            raise DocumentoInvalido("fuente v2 inválida o ausente.")
        if not isinstance(item.get("registrado_en"), str):
            raise DocumentoInvalido("registrado_en v2 obligatorio.")
        _utc(item["registrado_en"])
        if datetime.fromisoformat(item["registrado_en"].replace("Z", "+00:00")).utcoffset().total_seconds() != 0:
            raise DocumentoInvalido("registrado_en requiere UTC.")
    return result


class AlertaNoExiste(LookupError):
    """No existe el par (lectura_id, umbral_id) solicitado."""


class TransicionAlertaInvalida(ValueError):
    """El estado solicitado retrocede en el ciclo de vida de la alerta."""


class ConflictoAlerta(RuntimeError):
    """Otros escritores impidieron completar el cambio condicional."""


def _utf8(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise DocumentoInvalido("La cadena debe ser UTF-8 válido.") from exc


def _number(value) -> Decimal:
    try:
        number = DYNAMODB_CONTEXT.create_decimal(value)
    except (DecimalException, TypeError, ValueError) as exc:
        raise DocumentoInvalido("valor fuera de la precisión o rango numérico de DynamoDB.") from exc
    if not number.is_finite() or (number and not Decimal("1E-130") <= abs(number) <= Decimal("9." + "9" * 37 + "E+125")):
        raise DocumentoInvalido("valor debe ser finito y representable como número de DynamoDB.")
    return number


def estimate_item_size(item: dict) -> int:
    """Estima items planos S/N; los bytes numéricos son aproximados según AWS.

    No incluye el overhead de facturación de 100 bytes ni el texto del JSON.
    DynamoDB sigue arbitrando la frontera exacta.
    """
    size = 0
    for name, value in item.items():
        size += _utf8(name)
        if isinstance(value, str):
            size += _utf8(value)
        elif type(value) is int or isinstance(value, Decimal):
            number = _number(value)
            digits = list(number.as_tuple().digits)
            while len(digits) > 1 and digits[-1] == 0:
                digits.pop()
            size += (len(digits) + 1) // 2 + 1 + int(number < 0)
        else:
            raise DocumentoInvalido(f"{name}: se requiere una cadena o un número; sin conversión implícita.")
    return size


def _validate_item_size(item: dict) -> None:
    estimate = estimate_item_size(item)
    # AWS documenta el tamaño numérico como aproximado. Rechazar sólo cuando
    # incluso la cota mínima (un byte por número) supera el límite; el motor
    # resuelve los pocos bytes de incertidumbre sin escrituras parciales.
    minimum = sum(_utf8(name) + (_utf8(value) if isinstance(value, str) else 1)
                  for name, value in item.items())
    if minimum > MAX_ITEM_BYTES:
        raise DocumentoDemasiadoGrande(f"documento sobre el límite de {MAX_ITEM_BYTES} bytes (estimado: {estimate}).")


def _write(operation, **arguments) -> dict:
    try:
        return operation(**arguments)
    except ClientError as exc:
        error = exc.response["Error"]
        message = error.get("Message", "").lower()
        if error["Code"] == "ValidationException" and ("item size" in message or "item has exceeded" in message):
            raise DocumentoDemasiadoGrande(f"documento sobre el límite de {MAX_ITEM_BYTES} bytes: {error.get('Message', '')}") from exc
        raise


def _id(value: int) -> str:
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise DocumentoInvalido("El identificador debe ser un entero positivo de 64 bits.")
    return f"{value:019d}"


def _utc(value: str | datetime) -> str:
    if isinstance(value, str):
        if "T" not in value:
            raise DocumentoInvalido("Usa una fecha ISO 8601 con T y zona horaria.")
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DocumentoInvalido("La fecha debe ser ISO 8601 válida con zona horaria.") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DocumentoInvalido("La fecha debe incluir zona horaria.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class EventsStore:
    """Cliente sin conexión al importar; configuración por entorno al instanciar.

    DYNAMODB_ENDPOINT y AWS_REGION (o AWS_DEFAULT_REGION) son obligatorios.
    DYNAMODB_TABLE permite cambiar cdrl_eventos. Con DYNAMODB_LOCAL=true se
    generan credenciales efímeras de firma exclusivamente para un host local;
    sin esa opción boto3 utiliza su cadena normal de proveedores de AWS.
    ``resource`` permite inyectar un recurso boto3 para pruebas aisladas.
    """

    def __init__(self, table_name: str | None = None, *, resource=None, audit_table_name: str | None = None):
        self.table_name = table_name or os.getenv("DYNAMODB_TABLE", "cdrl_eventos")
        if resource is None:
            endpoint = os.getenv("DYNAMODB_ENDPOINT", "").strip()
            region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
            if not endpoint or not region:
                raise ValueError("Configura DYNAMODB_ENDPOINT y AWS_REGION o AWS_DEFAULT_REGION.")
            url = urlsplit(endpoint)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
                raise ValueError("DYNAMODB_ENDPOINT debe ser una URL HTTP(S) sin credenciales.")
            local = os.getenv("DYNAMODB_LOCAL", "false").lower()
            if local not in ("true", "false", "1", "0"):
                raise ValueError("DYNAMODB_LOCAL debe ser true, false, 1 o 0.")
            credentials = {}
            if local in ("true", "1"):
                if url.hostname not in ("localhost", "127.0.0.1", "::1", "dynamodb"):
                    raise ValueError("DYNAMODB_LOCAL requiere un endpoint local permitido.")
                # No enviar las credenciales reales del entorno al emulador.
                credentials = {"aws_access_key_id": secrets.token_hex(16), "aws_secret_access_key": secrets.token_hex(32)}
            resource = boto3.resource(
                "dynamodb",
                endpoint_url=endpoint,
                region_name=region,
                config=Config(connect_timeout=2, read_timeout=3, retries={"mode": "standard", "total_max_attempts": 2}),
                **credentials,
            )
        self.resource = resource
        self.table = resource.Table(self.table_name)
        self.audit_table_name = audit_table_name or os.getenv("DYNAMODB_AUDIT_TABLE", "cdrl_auditoria")
        self.audit_table = resource.Table(self.audit_table_name)

    def ensure_table(self) -> dict:
        """Crea/compara eventos y auditoría contra sus declaraciones."""
        description = self._ensure_declared_table(self.table_name, INDEXES_PATH)
        self._ensure_declared_table(self.audit_table_name, AUDIT_PATH)
        return description

    def _ensure_declared_table(self, table_name: str, path: Path) -> dict:
        declared = json.loads(path.read_text(encoding="utf-8"))
        spec = declared["table"]
        indexes = [{key: index[key] for key in ("IndexName", "KeySchema", "Projection")}
                   for index in spec["GlobalSecondaryIndexes"]]
        client = self.resource.meta.client
        try:
            client.describe_table(TableName=table_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            try:
                client.create_table(
                    TableName=table_name,
                    KeySchema=spec["KeySchema"],
                    AttributeDefinitions=spec["AttributeDefinitions"],
                    BillingMode=spec["BillingMode"],
                    GlobalSecondaryIndexes=indexes,
                )
            except ClientError as concurrent:
                if concurrent.response["Error"]["Code"] != "ResourceInUseException":
                    raise
        client.get_waiter("table_exists").wait(TableName=table_name, WaiterConfig={"Delay": 1, "MaxAttempts": 30})
        description = client.describe_table(TableName=table_name)["Table"]

        def pairs(entries, value):
            return sorted((entry["AttributeName"], entry[value]) for entry in entries)

        def index_shape(entries):
            return sorted((index["IndexName"], pairs(index["KeySchema"], "KeyType"),
                           index["Projection"]["ProjectionType"],
                           sorted(index["Projection"].get("NonKeyAttributes", []))) for index in entries)

        actual_indexes = description.get("GlobalSecondaryIndexes", [])
        if (pairs(description["KeySchema"], "KeyType") != pairs(spec["KeySchema"], "KeyType")
                or pairs(description["AttributeDefinitions"], "AttributeType") != pairs(spec["AttributeDefinitions"], "AttributeType")
                or index_shape(actual_indexes) != index_shape(indexes)
                or description.get("LocalSecondaryIndexes")):
            raise ValueError(f"La tabla existente no coincide con los índices declarados en {path.name}.")
        if description.get("TableStatus") != "ACTIVE" or any(index.get("IndexStatus") != "ACTIVE" for index in actual_indexes):
            raise ValueError("La tabla o sus índices todavía no están ACTIVE; reintenta cuando termine su creación.")
        return description

    @staticmethod
    def build_lectura(lectura_id: int, lectura: LecturaEntrada | dict, *, payload: str | None = None, fuente: str = "api") -> dict:
        """Valida el contrato sin coerción y estima tamaño antes de escribir."""
        identity = _id(lectura_id)
        if fuente not in ("api", "lote", "fixture"):
            raise DocumentoInvalido("fuente debe ser api, lote o fixture.")
        if isinstance(lectura, LecturaBase):
            # También revalidar modelos mutados o creados con model_construct.
            lectura = lectura.model_dump()
        if not isinstance(lectura, dict):
            raise DocumentoInvalido("lectura debe ser un documento o un modelo LecturaEntrada.")
        try:
            validated = _LECTURA.validate_python(lectura)
        except ValidationError as exc:
            raise DocumentoInvalido(str(exc)) from exc
        instant = _utc(validated.medido_en)
        item = {
            "PK": f"EQ#{validated.equipo_codigo}#{validated.metrica}",
            "SK": f"TS#{instant}#{identity}",
            "entidad": "lectura", "lectura_id": lectura_id,
            "schemaVersion": 2, "fuente": fuente, "registrado_en": _utc(datetime.now(timezone.utc)),
            "equipo_codigo": validated.equipo_codigo, "metrica": validated.metrica,
            "unidad": validated.unidad, "valor": _number(str(validated.valor)),
            "medido_en": instant,
        }
        if payload is not None:
            if not isinstance(payload, str):
                raise DocumentoInvalido("payload debe ser una cadena sintética.")
            item["payload"] = payload
        _validate_item_size(item)
        return item

    def put_lectura(self, lectura_id: int, lectura: LecturaEntrada | dict, *, payload: str | None = None, fuente: str = "api") -> dict:
        return _write(self.table.put_item,
            Item=self.build_lectura(lectura_id, lectura, payload=payload, fuente=fuente),
            ConditionExpression="attribute_not_exists(PK)", ReturnConsumedCapacity="TOTAL",
        )

    @staticmethod
    def build_alerta(*, lectura_id: int, umbral_id: int, estado: str, severidad: str,
                     medido_en: str | datetime, cerrada_en: str | datetime | None = None) -> dict:
        """Identidad inmutable en la tabla; estado mutable y prioridad en GSI1.

        La clave del ADR basada en estado no impediría duplicar (lectura, umbral).
        Se materializa como GSI y se añade identidad al final para evitar empates.
        El rango numérico hace que critica preceda advertencia al leer descendente.
        """
        lectura, umbral = _id(lectura_id), _id(umbral_id)
        if not isinstance(estado, str) or estado not in _ESTADOS:
            raise DocumentoInvalido("estado debe ser abierta, reconocida o cerrada.")
        if not isinstance(severidad, str) or severidad not in _SEVERIDAD:
            raise DocumentoInvalido("severidad debe ser advertencia o critica.")
        if (estado == "cerrada") != (cerrada_en is not None):
            raise DocumentoInvalido("cerrada_en debe estar presente si y solo si estado es cerrada.")
        instant = _utc(medido_en)
        identity = f"LECTURA#{lectura}#UMBRAL#{umbral}"
        shard = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big") % ALERT_SHARDS
        item = {
            "PK": f"LECTURA#{lectura}", "SK": f"UMBRAL#{umbral}",
            "GSI1PK": f"ALERTA#{estado}#{shard}",
            "GSI1SK": f"SEV#{_SEVERIDAD[severidad]}#TS#{instant}#{identity}",
            "entidad": "alerta", "lectura_id": lectura_id, "umbral_id": umbral_id,
            "estado": estado, "severidad": severidad, "medido_en": instant, "shard": shard,
        }
        if cerrada_en is not None:
            item["cerrada_en"] = _utc(cerrada_en)
        _validate_item_size(item)
        return item

    def put_alerta(self, *, lectura_id: int, umbral_id: int, estado: str, severidad: str,
                   medido_en: str | datetime, cerrada_en: str | datetime | None = None) -> dict:
        return _write(self.table.put_item,
            Item=self.build_alerta(lectura_id=lectura_id, umbral_id=umbral_id, estado=estado, severidad=severidad,
                                   medido_en=medido_en, cerrada_en=cerrada_en),
            ConditionExpression="attribute_not_exists(PK)", ReturnConsumedCapacity="TOTAL",
        )

    def query_lecturas(self, equipo_codigo: str, metrica: str, *, desde=None, hasta=None, limite: int = 100,
                       exclusive_start_key: dict | None = None, consistent_read: bool = False) -> dict:
        """Página Q1/Q3 sin Scan ni FilterExpression; conserva capacidad y cursor."""
        query = ConsultaLecturas(equipo=equipo_codigo, metrica=metrica, desde=desde, hasta=hasta, limite=limite)
        if query.metrica is None:
            raise ValueError("query_lecturas requiere una métrica; usa ultimas_lecturas para las cinco.")
        condition = Key("PK").eq(f"EQ#{query.equipo}#{query.metrica}")
        if query.desde and query.hasta:
            condition &= Key("SK").between(f"TS#{_utc(query.desde)}#", f"TS#{_utc(query.hasta)}#~")
        elif query.desde:
            condition &= Key("SK").gte(f"TS#{_utc(query.desde)}#")
        elif query.hasta:
            condition &= Key("SK").lte(f"TS#{_utc(query.hasta)}#~")
        arguments = dict(KeyConditionExpression=condition, ScanIndexForward=False, Limit=query.limite,
                         ConsistentRead=consistent_read, ReturnConsumedCapacity="TOTAL")
        if exclusive_start_key:
            arguments["ExclusiveStartKey"] = exclusive_start_key
        page = self.table.query(**arguments)
        page["Items"] = [normalizar_lectura(item) for item in page["Items"]]
        return page

    def iter_lecturas(self, equipo_codigo: str, metrica: str, *, desde=None, hasta=None,
                      limite: int = 100, consistent_read: bool = False) -> Iterator[dict]:
        """Recorre todas las páginas de una ventana para agregar Q2 en el cliente."""
        cursor = None
        while True:
            page = self.query_lecturas(equipo_codigo, metrica, desde=desde, hasta=hasta, limite=limite,
                                      exclusive_start_key=cursor, consistent_read=consistent_read)
            yield from page["Items"]
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                return

    def ultimas_lecturas(self, equipo_codigo: str, *, metrica: str | None = None, desde=None,
                         hasta=None, limite: int = 100, consistent_read: bool = False) -> list[dict]:
        """Últimas N globales; fan-out fijo de cinco métricas si se omite el filtro.

        Continúa si DynamoDB corta una página por su límite de 1 MiB, aun cuando
        haya devuelto menos de Limit. Ordena UTC y desempata por id numérico.
        """
        query = ConsultaLecturas(equipo=equipo_codigo, metrica=metrica, desde=desde, hasta=hasta, limite=limite)

        def latest(metric):
            items, cursor = [], None
            while len(items) < query.limite:
                page = self.query_lecturas(query.equipo, metric, desde=query.desde, hasta=query.hasta,
                                          limite=query.limite - len(items), exclusive_start_key=cursor,
                                          consistent_read=consistent_read)
                items.extend(page["Items"])
                cursor = page.get("LastEvaluatedKey")
                if not cursor:
                    break
            return items

        if query.metrica:
            return latest(query.metrica)
        # Los objetos Resource de boto3 no se comparten entre hilos. El fan-out
        # es fijo; un consumidor paralelo debe crear su propio cliente por hilo.
        items = [item for metric in METRICAS for item in latest(metric)]
        return sorted(items, key=lambda item: item["SK"], reverse=True)[:query.limite]

    def query_alertas(self, estado: str, shard: int, *, limite: int = 100,
                      exclusive_start_key: dict | None = None) -> dict:
        """Página de un shard del GSI; siempre eventualmente consistente."""
        if estado not in _ESTADOS or type(shard) is not int or not 0 <= shard < ALERT_SHARDS:
            raise ValueError("Estado o shard de alerta inválidos.")
        if type(limite) is not int or not 1 <= limite <= 1000:
            raise ValueError("El límite debe ser un entero entre 1 y 1000.")
        arguments = dict(IndexName=GSI_NAME, KeyConditionExpression=Key("GSI1PK").eq(f"ALERTA#{estado}#{shard}"),
                         ScanIndexForward=False, Limit=limite, ReturnConsumedCapacity="INDEXES")
        if exclusive_start_key:
            arguments["ExclusiveStartKey"] = exclusive_start_key
        return self.table.query(**arguments)

    def get_lectura(self, equipo_codigo: str, metrica: str, medido_en: str | datetime,
                    lectura_id: int, *, consistent_read: bool = True) -> dict | None:
        """Lectura por clave completa; no existe una búsqueda por id solo."""
        query = ConsultaLecturas(equipo=equipo_codigo, metrica=metrica)
        if query.metrica is None:
            raise DocumentoInvalido("get_lectura requiere una métrica.")
        key = {"PK": f"EQ#{query.equipo}#{query.metrica}", "SK": f"TS#{_utc(medido_en)}#{_id(lectura_id)}"}
        item = self.table.get_item(Key=key, ConsistentRead=consistent_read,
                                   ReturnConsumedCapacity="TOTAL").get("Item")
        return normalizar_lectura(item) if item is not None else None

    @staticmethod
    def _alerta_key(lectura_id: int, umbral_id: int) -> dict:
        return {"PK": f"LECTURA#{_id(lectura_id)}", "SK": f"UMBRAL#{_id(umbral_id)}"}

    @staticmethod
    def build_auditoria(registro: dict) -> dict:
        """Valida la lista blanca antes de derivar las claves internas."""
        fields = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))["fields"]
        if not isinstance(registro, dict):
            raise AuditoriaInvalida("La auditoría requiere un documento.")
        item = dict(registro)
        allowed = set(fields["required"] + fields["optional"])
        if set(item) - allowed:
            raise AuditoriaInvalida("Campo fuera de la lista blanca.")
        item.setdefault("schemaVersion", 1)
        item.setdefault("operacion_id", uuid4().hex)
        item.setdefault("ocurrido_en", _utc(datetime.now(timezone.utc)))
        if (isinstance(item["schemaVersion"], bool)
                or not isinstance(item["schemaVersion"], (int, Decimal)) or item["schemaVersion"] != 1):
            raise AuditoriaInvalida("schemaVersion de auditoría debe ser 1.")
        for name in fields["required"]:
            if name not in ("PK", "SK") and name not in item:
                raise AuditoriaInvalida(f"Falta {name}.")
        for name, choices in fields["enums"].items():
            if name in item and (not isinstance(item[name], str) or item[name] not in choices):
                raise AuditoriaInvalida(f"{name} inválido.")
        for name, pattern in fields["patterns"].items():
            if name in item and (not isinstance(item[name], str) or re.fullmatch(pattern, item[name]) is None):
                raise AuditoriaInvalida(f"{name} inválido.")
        if "conteo" in item and (type(item["conteo"]) is not int or item["conteo"] < 0):
            raise AuditoriaInvalida("conteo debe ser un entero no negativo.")
        secret = re.compile(r"AKIA[0-9A-Z]{16}|://[^\s/:]+:[^\s@]+@|password|secret|token|bearer", re.I)
        if any(secret.search(value) for value in item.values() if isinstance(value, str)):
            raise AuditoriaInvalida("Texto prohibido en auditoría.")
        try:
            occurred = _utc(item["ocurrido_en"])
        except DocumentoInvalido as exc:
            raise AuditoriaInvalida("ocurrido_en inválido.") from exc
        item["ocurrido_en"] = occurred
        keys = {"PK": f"AUD#{occurred[:10]}", "SK": f"TS#{occurred}#{item['operacion_id']}"}
        if any(name in item and item[name] != value for name, value in keys.items()):
            raise AuditoriaInvalida("Claves de auditoría inconsistentes.")
        item.update(keys)
        is_alert = item["operacion"] in ("actualizar_estado_alerta", "delete_alerta")
        if (item["entidad"] != ("alerta" if is_alert else "lote_lecturas")
                or not item["clave_afectada"].startswith("LECTURA#" if is_alert else "LOTE#")):
            raise AuditoriaInvalida("Operación, entidad y clave incompatibles.")
        return item

    def _audit_writes(self, item: dict) -> list[dict]:
        identity = f"OP#{item['operacion_id']}"
        return [{"Put": {"TableName": self.audit_table_name, "Item": record,
                          "ConditionExpression": "attribute_not_exists(PK)"}}
                for record in ({"PK": identity, "SK": identity, "operacion_id": item["operacion_id"]}, item)]

    def _transact(self, changes: list[dict], audit: dict) -> dict:
        try:
            return self.resource.meta.client.transact_write_items(
                TransactItems=self._audit_writes(audit) + changes, ReturnConsumedCapacity="TOTAL")
        except ClientError as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if (exc.response["Error"]["Code"] == "TransactionCanceledException"
                    and any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons[:2])):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                             "Message": "operacion_id ya registrado."}}, "TransactWriteItems") from exc
            raise

    def registrar_auditoria(self, registro: dict | None = None, **fields) -> dict:
        if registro is not None and fields:
            raise AuditoriaInvalida("Usa un documento o campos, sin combinarlos.")
        return self._transact([], self.build_auditoria(fields if registro is None else registro))

    def query_auditoria_dia(self, dia: str, *, limite: int = 100, exclusive_start_key=None) -> dict:
        if not isinstance(dia, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", dia) is None:
            raise AuditoriaInvalida("día inválido.")
        datetime.strptime(dia, "%Y-%m-%d")
        return self._query_auditoria(Key("PK").eq(f"AUD#{dia}"), limite, exclusive_start_key)

    def query_auditoria_clave(self, clave_afectada: str, *, limite: int = 100, exclusive_start_key=None) -> dict:
        pattern = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))["fields"]["patterns"]["clave_afectada"]
        if not isinstance(clave_afectada, str) or re.fullmatch(pattern, clave_afectada) is None:
            raise AuditoriaInvalida("clave_afectada inválida.")
        return self._query_auditoria(Key("clave_afectada").eq(clave_afectada), limite,
                                     exclusive_start_key, index="AUD_CLAVE")

    def _query_auditoria(self, condition, limite, cursor, *, index=None):
        if type(limite) is not int or not 1 <= limite <= 1000:
            raise AuditoriaInvalida("limite fuera de rango.")
        arguments = dict(KeyConditionExpression=condition, Limit=limite,
                         ConsistentRead=index is None, ReturnConsumedCapacity="TOTAL")
        if index:
            arguments["IndexName"] = index
        if cursor:
            arguments["ExclusiveStartKey"] = cursor
        return self.audit_table.query(**arguments)

    def get_alerta(self, lectura_id: int, umbral_id: int, *, consistent_read: bool = True) -> dict | None:
        """Item o None; lectura fuerte por identidad para reevaluar alertas Q4."""
        return self.table.get_item(Key=self._alerta_key(lectura_id, umbral_id),
                                   ConsistentRead=consistent_read, ReturnConsumedCapacity="TOTAL").get("Item")

    @staticmethod
    def _validate_alerta(item: dict, lectura_id: int, umbral_id: int) -> None:
        """Revalida el documento completo, incluidos campos/claves generados."""
        try:
            expected = EventsStore.build_alerta(
                lectura_id=lectura_id, umbral_id=umbral_id, estado=item["estado"],
                severidad=item["severidad"], medido_en=item["medido_en"], cerrada_en=item.get("cerrada_en"),
            )
        except KeyError as exc:
            raise DocumentoInvalido(f"Falta el campo obligatorio {exc.args[0]} en la alerta.") from exc
        for field in set(item) | set(expected):
            if field not in expected:
                raise DocumentoInvalido(f"Campo extra {field} en la alerta.")
            if field not in item:
                raise DocumentoInvalido(f"Falta el campo obligatorio {field} en la alerta.")
            if item[field] != expected[field] or (type(item[field]) is bool and not isinstance(expected[field], bool)):
                raise DocumentoInvalido(f"{field} no coincide con el contrato/identidad de la alerta.")

    def actualizar_estado_alerta(self, lectura_id: int, umbral_id: int, estado: str,
                                 *, cerrada_en: str | datetime | None = None,
                                 actor_rol: str = "cdrl_ops", operacion_id: str | None = None) -> dict:
        """CAS atómico; estado repetido conserva el documento sin escribir."""
        key = self._alerta_key(lectura_id, umbral_id)
        if not isinstance(estado, str) or estado not in _ESTADOS:
            raise DocumentoInvalido("estado debe ser abierta, reconocida o cerrada.")
        if cerrada_en is not None and estado != "cerrada":
            raise DocumentoInvalido("cerrada_en solo es válida al solicitar estado cerrada.")
        audit_fields = dict(operacion="actualizar_estado_alerta", actor_rol=actor_rol,
                            entidad="alerta", clave_afectada=f"{key['PK']}#{key['SK']}", resultado="ok")
        if operacion_id is not None:
            audit_fields["operacion_id"] = operacion_id
        audit_base = self.build_auditoria(audit_fields)
        closure = _utc(cerrada_en) if cerrada_en is not None else None
        current = self.get_alerta(lectura_id, umbral_id, consistent_read=True)
        for attempt in range(3):
            if current is None:
                raise AlertaNoExiste(f"No existe la alerta de lectura_id={lectura_id}, umbral_id={umbral_id}.")
            self._validate_alerta(current, lectura_id, umbral_id)
            previous = current["estado"]
            if previous == estado:
                return current
            if estado not in _TRANSICIONES[previous]:
                self.registrar_auditoria(dict(audit_base, resultado="rechazada",
                    codigo_error="TransicionAlertaInvalida", estado_anterior=previous, estado_nuevo=estado))
                raise TransicionAlertaInvalida(f"Transición de alerta inválida: {previous} → {estado}.")
            if estado == "cerrada" and closure is None:
                closure = _utc(datetime.now(timezone.utc))
            candidate = self.build_alerta(lectura_id=lectura_id, umbral_id=umbral_id, estado=estado,
                                          severidad=current["severidad"], medido_en=current["medido_en"],
                                          cerrada_en=closure)
            expression = "SET estado = :estado, GSI1PK = :gsi"
            values = {":estado": estado, ":estado_anterior": previous, ":gsi": candidate["GSI1PK"]}
            if estado == "cerrada":
                expression += ", cerrada_en = :cierre"
                values[":cierre"] = candidate["cerrada_en"]
            try:
                audit = self.build_auditoria(dict(audit_base, estado_anterior=previous, estado_nuevo=estado))
                self._transact([{"Update": dict(TableName=self.table_name, Key=key,
                    UpdateExpression=expression,
                    ConditionExpression="attribute_exists(PK) AND estado = :estado_anterior",
                    ExpressionAttributeValues=values)}], audit)
                return candidate
            except ClientError as exc:
                reasons = exc.response.get("CancellationReasons", [])
                if (exc.response["Error"]["Code"] != "TransactionCanceledException"
                        or len(reasons) != 3 or reasons[2].get("Code") != "ConditionalCheckFailed"):
                    raise
                # El GSI es eventual: decidir siempre con la tabla base fuerte.
                current = self.get_alerta(lectura_id, umbral_id, consistent_read=True)
        # Clasificar también la última carrera antes de informar contención.
        if current is None:
            raise AlertaNoExiste(f"La alerta de lectura_id={lectura_id}, umbral_id={umbral_id} fue eliminada.")
        self._validate_alerta(current, lectura_id, umbral_id)
        if current["estado"] == estado:
            return current
        if estado not in _TRANSICIONES[current["estado"]]:
            self.registrar_auditoria(dict(audit_base, resultado="rechazada",
                codigo_error="TransicionAlertaInvalida", estado_anterior=current["estado"], estado_nuevo=estado))
            raise TransicionAlertaInvalida(f"Transición de alerta inválida: {current['estado']} → {estado}.")
        raise ConflictoAlerta("La alerta cambió durante tres intentos condicionales; reintenta la operación.")

    def delete_alerta(self, lectura_id: int, umbral_id: int, *, actor_rol: str = "cdrl_ops",
                      operacion_id: str | None = None) -> bool:
        """Baja y auditoría atómicas; ausencia no genera registro."""
        key = self._alerta_key(lectura_id, umbral_id)
        fields = dict(operacion="delete_alerta", actor_rol=actor_rol, entidad="alerta",
                      clave_afectada=f"{key['PK']}#{key['SK']}", resultado="ok")
        if operacion_id is not None:
            fields["operacion_id"] = operacion_id
        audit = self.build_auditoria(fields)
        try:
            self._transact([{"Delete": dict(TableName=self.table_name, Key=key,
                ConditionExpression="attribute_exists(PK)")}], audit)
            return True
        except ClientError as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if (exc.response["Error"]["Code"] == "TransactionCanceledException"
                    and len(reasons) == 3 and reasons[2].get("Code") == "ConditionalCheckFailed"):
                return False
            raise


def main() -> None:
    """Inicialización de Compose: reintenta la conexión durante hasta 30 segundos."""
    store = EventsStore()
    deadline = time.monotonic() + 30
    while True:
        try:
            description = store.ensure_table()
            print(f"DynamoDB: {store.table_name} {description['TableStatus']} (GSI1)")
            return
        except (EndpointConnectionError, ConnectTimeoutError, ConnectionClosedError, ReadTimeoutError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


if __name__ == "__main__":
    main()
