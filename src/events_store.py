"""Almacén experimental M04; PostgreSQL conserva los endpoints existentes.

Las lecturas son append-only. La identidad de una alerta vive en la clave base
(lectura, umbral), porque una condición sobre un atributo secundario no impone
unicidad. GSI1 organiza esas alertas por estado, shard, severidad e instante;
solo la tabla base admite lectura fuerte. Los errores de boto3 se propagan para
que el consumidor distinga duplicados, tamaño excedido y fallos de conexión.
"""

import hashlib
import os
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator
from urllib.parse import urlsplit

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from pydantic import TypeAdapter

from src.schemas import ConsultaLecturas, LecturaEntrada

METRICAS = ("cpu", "memoria", "disco_libre", "latencia", "tasa_error")
ALERT_SHARDS = 4
GSI_NAME = "GSI1"
_ESTADOS = ("abierta", "reconocida", "cerrada")
_SEVERIDAD = {"advertencia": 1, "critica": 2}
_LECTURA = TypeAdapter(LecturaEntrada)
_BASE_KEYS = [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]
_INDEX_KEYS = [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]


def _id(value: int) -> str:
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ValueError("El identificador debe ser un entero positivo de 64 bits.")
    return f"{value:019d}"


def _utc(value: str | datetime) -> str:
    if isinstance(value, str):
        if "T" not in value:
            raise ValueError("Usa una fecha ISO 8601 con T y zona horaria.")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("La fecha debe incluir zona horaria.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class EventsStore:
    """Cliente sin conexión al importar; configuración por entorno al instanciar.

    DYNAMODB_ENDPOINT y AWS_REGION (o AWS_DEFAULT_REGION) son obligatorios.
    DYNAMODB_TABLE permite cambiar cdrl_eventos. Con DYNAMODB_LOCAL=true se
    generan credenciales efímeras de firma exclusivamente para un host local;
    sin esa opción boto3 utiliza su cadena normal de proveedores de AWS.
    ``resource`` permite inyectar un recurso boto3 para pruebas aisladas.
    """

    def __init__(self, table_name: str | None = None, *, resource=None):
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

    def ensure_table(self) -> dict:
        """Crea la tabla si falta, espera ACTIVE y rechaza esquemas incompatibles."""
        client = self.resource.meta.client
        try:
            client.describe_table(TableName=self.table_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            try:
                client.create_table(
                    TableName=self.table_name,
                    KeySchema=_BASE_KEYS,
                    AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name in ("PK", "SK", "GSI1PK", "GSI1SK")],
                    BillingMode="PAY_PER_REQUEST",
                    GlobalSecondaryIndexes=[{"IndexName": GSI_NAME, "KeySchema": _INDEX_KEYS, "Projection": {"ProjectionType": "ALL"}}],
                )
            except ClientError as concurrent:
                if concurrent.response["Error"]["Code"] != "ResourceInUseException":
                    raise
        client.get_waiter("table_exists").wait(TableName=self.table_name, WaiterConfig={"Delay": 1, "MaxAttempts": 30})
        description = client.describe_table(TableName=self.table_name)["Table"]
        keys = lambda schema: {(entry["AttributeName"], entry["KeyType"]) for entry in schema}
        index = next((index for index in description.get("GlobalSecondaryIndexes", []) if index["IndexName"] == GSI_NAME), None)
        attributes = {entry["AttributeName"]: entry["AttributeType"] for entry in description["AttributeDefinitions"]}
        if (keys(description["KeySchema"]) != keys(_BASE_KEYS)
                or index is None or keys(index["KeySchema"]) != keys(_INDEX_KEYS)
                or index["Projection"]["ProjectionType"] != "ALL"
                or any(attributes.get(name) != "S" for name in ("PK", "SK", "GSI1PK", "GSI1SK"))):
            raise ValueError("La tabla existente no tiene PK/SK y GSI1 compatibles con M04.")
        if index.get("IndexStatus") != "ACTIVE":
            raise ValueError("GSI1 todavía no está ACTIVE; reintenta cuando termine su creación.")
        return description

    @staticmethod
    def build_lectura(lectura_id: int, lectura: LecturaEntrada | dict, *, payload: str | None = None) -> dict:
        """Valida el contrato relacional; payload sintético permite medir 400 KiB.

        El límite de tamaño lo valida DynamoDB, incluyendo nombres y claves;
        no se aproxima mediante el tamaño del JSON. Los números usan Decimal.
        """
        identity = _id(lectura_id)
        validated = _LECTURA.validate_python(lectura)
        instant = _utc(validated.medido_en)
        item = {
            "PK": f"EQ#{validated.equipo_codigo}#{validated.metrica}",
            "SK": f"TS#{instant}#{identity}",
            "entidad": "lectura", "lectura_id": lectura_id,
            "equipo_codigo": validated.equipo_codigo, "metrica": validated.metrica,
            "unidad": validated.unidad, "valor": Decimal(str(validated.valor)),
            "medido_en": instant,
        }
        if payload is not None:
            if not isinstance(payload, str):
                raise ValueError("payload debe ser una cadena sintética.")
            item["payload"] = payload
        return item

    def put_lectura(self, lectura_id: int, lectura: LecturaEntrada | dict, *, payload: str | None = None) -> dict:
        return self.table.put_item(
            Item=self.build_lectura(lectura_id, lectura, payload=payload),
            ConditionExpression="attribute_not_exists(PK)", ReturnConsumedCapacity="TOTAL",
        )

    @staticmethod
    def build_alerta(*, lectura_id: int, umbral_id: int, estado: str, severidad: str, medido_en: str | datetime) -> dict:
        """Identidad inmutable en la tabla; estado mutable y prioridad en GSI1.

        La clave del ADR basada en estado no impediría duplicar (lectura, umbral).
        Se materializa como GSI y se añade identidad al final para evitar empates.
        El rango numérico hace que critica preceda advertencia al leer descendente.
        """
        lectura, umbral = _id(lectura_id), _id(umbral_id)
        if estado not in _ESTADOS or severidad not in _SEVERIDAD:
            raise ValueError("Estado o severidad de alerta inválidos.")
        instant = _utc(medido_en)
        identity = f"LECTURA#{lectura}#UMBRAL#{umbral}"
        shard = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big") % ALERT_SHARDS
        return {
            "PK": f"LECTURA#{lectura}", "SK": f"UMBRAL#{umbral}",
            "GSI1PK": f"ALERTA#{estado}#{shard}",
            "GSI1SK": f"SEV#{_SEVERIDAD[severidad]}#TS#{instant}#{identity}",
            "entidad": "alerta", "lectura_id": lectura_id, "umbral_id": umbral_id,
            "estado": estado, "severidad": severidad, "medido_en": instant, "shard": shard,
        }

    def put_alerta(self, *, lectura_id: int, umbral_id: int, estado: str, severidad: str, medido_en: str | datetime) -> dict:
        return self.table.put_item(
            Item=self.build_alerta(lectura_id=lectura_id, umbral_id=umbral_id, estado=estado, severidad=severidad, medido_en=medido_en),
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
        return self.table.query(**arguments)

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

    def get_alerta(self, lectura_id: int, umbral_id: int, *, consistent_read: bool = True) -> dict:
        """Lectura fuerte por identidad, disponible para reevaluar alertas Q4."""
        return self.table.get_item(Key={"PK": f"LECTURA#{_id(lectura_id)}", "SK": f"UMBRAL#{_id(umbral_id)}"},
                                   ConsistentRead=consistent_read, ReturnConsumedCapacity="TOTAL")


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
