"""Recuperación M06 sobre fixture aislado; respaldo JSONL fuera del repo."""

import argparse
import hashlib
import json
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from src.events_store import EventsStore, normalizar_lectura

ROOT = Path(__file__).resolve().parents[1]
METRICAS = ("cpu", "memoria")
EQUIPO = "fixture-m06"


def _key(item):
    return {name: item[name] for name in ("PK", "SK")}


def query_fixture(store):
    """Documentos originales paginados, sin upcast, Scan ni pérdida de precisión."""
    items = []
    for metric in METRICAS:
        arguments = dict(KeyConditionExpression=Key("PK").eq(f"EQ#{EQUIPO}#{metric}"),
                         ConsistentRead=True, Limit=25)
        while True:
            page = store.table.query(**arguments)
            items.extend(page["Items"])
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                break
            arguments["ExclusiveStartKey"] = cursor
    return sorted(items, key=lambda item: (item["PK"], item["SK"]))


def restaurar_faltantes(store, items):
    """Copia condicional de documentos originales; nunca actualiza existentes."""
    restored, existed = 0, 0
    for item in items:
        normalizar_lectura(item)  # No convertir v1 al persistir.
        if store.table.get_item(Key=_key(item), ConsistentRead=True).get("Item") is not None:
            existed += 1
            continue
        try:
            store.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
            restored += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            existed += 1
    return {"restaurados": restored, "ya_existian": existed}


def _compare(store, originals):
    missing, differences = 0, 0
    for expected in originals:
        actual = store.table.get_item(Key=_key(expected), ConsistentRead=True).get("Item")
        if actual is None:
            missing += 1
        else:
            differences += sum(actual.get(name) != expected.get(name) or
                               (name in actual) != (name in expected)
                               for name in set(actual) | set(expected))
    return missing, differences


def recover_m06(store=None, *, backup_dir=None):
    """Los nueve pasos del ADR-006. Devuelve mediciones para las pruebas de Ale.

    Sin store crea una tabla de fixture por corrida. Con store exige que las
    dos particiones fixture estén vacías; no borra datos ajenos para preparar.
    Conserva el respaldo externo y elimina la tabla restore incluso si falla.
    """
    run_id = uuid4().hex
    store = store or EventsStore(table_name=f"cdrl_eventos_fixture_{run_id}")
    store.ensure_table()
    if query_fixture(store):
        raise ValueError("El fixture M06 requiere particiones vacías; usa una tabla aislada.")
    directory = Path(backup_dir).resolve() if backup_dir else Path(tempfile.mkdtemp(prefix="cdrl-m06-"))
    if directory == ROOT or ROOT in directory.parents:
        raise ValueError("El respaldo debe quedar fuera del repositorio.")
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / f"{run_id}.jsonl"
    rng = random.Random(42)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)

    # 1. 40 v1 y 80 v2; claves y valores reproducibles.
    for number in range(120):
        reading = dict(equipo_codigo=EQUIPO, metrica=METRICAS[number % 2],
                       unidad="porcentaje", valor=rng.randint(0, 100),
                       medido_en=start + timedelta(seconds=number))
        if number < 40:
            item = store.build_lectura(number + 1, reading, fuente="fixture")
            for name in ("schemaVersion", "fuente", "registrado_en"):
                del item[name]
            store.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        else:
            store.put_lectura(number + 1, reading, fuente="fixture")

    # 2. Query cruda a JSONL con tipos DynamoDB, no float ni upcast.
    originals = query_fixture(store)
    serializer, deserializer = TypeSerializer(), TypeDeserializer()
    with backup.open("x", encoding="utf-8", newline="\n") as stream:
        for item in originals:
            stream.write(json.dumps({name: serializer.serialize(value) for name, value in item.items()},
                                    sort_keys=True, separators=(",", ":")) + "\n")
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()

    # 3. Diez posteriores; no forman parte del respaldo.
    for number in range(120, 130):
        store.put_lectura(number + 1, dict(equipo_codigo=EQUIPO, metrica=METRICAS[number % 2],
            unidad="porcentaje", valor=rng.randint(0, 100),
            medido_en=start + timedelta(seconds=number)), fuente="fixture")
    later = [item for item in query_fixture(store) if item["lectura_id"] > 120]

    # 4. Exactamente diez v1 y veinte v2, sin tocar otras particiones.
    deleted = [item for item in originals if int(item["lectura_id"]) in set(range(1, 11)) | set(range(41, 61))]
    for item in deleted:
        store.table.delete_item(Key=_key(item))
    audit_base = dict(actor_rol="recuperacion", entidad="lote_lecturas",
                      clave_afectada=f"LOTE#{run_id}", resultado="ok")
    store.registrar_auditoria(**audit_base, operacion="falla_controlada", conteo=len(deleted),
                             motivo="borrado_accidental")

    # 5. Tabla temporal con la misma declaración e índices que eventos.
    restore = EventsStore(table_name=f"cdrl_eventos_restore_{run_id}", resource=store.resource,
                          audit_table_name=store.audit_table_name)
    started = perf_counter()
    created = False
    try:
        restore.ensure_table()
        created = True
        for line in backup.read_text(encoding="utf-8").splitlines():
            item = {name: deserializer.deserialize(value) for name, value in json.loads(line).items()}
            restore.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")

        # 6-7. Comparar claves, luego copiar únicamente las faltantes.
        restored_items = query_fixture(restore)
        missing_items = [item for item in restored_items
                         if store.table.get_item(Key=_key(item), ConsistentRead=True).get("Item") is None]
        first = restaurar_faltantes(store, missing_items)

        # 8. Comparación campo por campo; segunda pasada para medir idempotencia.
        missing, differences = _compare(store, originals)
        later_missing, later_differences = _compare(store, later)
        v1 = sum("schemaVersion" not in store.table.get_item(Key=_key(item), ConsistentRead=True).get("Item", {})
                 for item in deleted if "schemaVersion" not in item
                 and store.table.get_item(Key=_key(item), ConsistentRead=True).get("Item") is not None)
        elapsed = round((perf_counter() - started) * 1000, 3)
        second = restaurar_faltantes(store, restored_items)
        second["faltantes_tras_restaurar"], second["diferencias_campo"] = _compare(store, originals)
        intact = sum(store.table.get_item(Key=_key(item), ConsistentRead=True).get("Item") == item for item in later)
        result = dict(esperados=30, **first, faltantes_tras_restaurar=missing, diferencias_campo=differences,
            v1_restauradas_sin_schemaVersion=v1, posteriores_intactas=intact,
            total_final=len(query_fixture(store)), duracion_restauracion_ms=elapsed,
            escrituras_posteriores_perdidas=later_missing, segunda_corrida=second,
            respaldo_conteo=len(originals), respaldo_sha256=digest, respaldo_archivo=str(backup),
            tabla_eventos=store.table_name, tabla_temporal=restore.table_name, lote_id=run_id)
        if (first != {"restaurados": 30, "ya_existian": 0} or missing or differences
                or later_missing or later_differences or intact != 10 or v1 != 10
                or result["total_final"] != 130 or second["restaurados"] != 0
                or second["faltantes_tras_restaurar"] or second["diferencias_campo"]):
            raise RuntimeError("La recuperación no cumple las mediciones del ADR-006.")

        # 9. Evidencia segura del lote y limpieza garantizada.
        store.registrar_auditoria(**audit_base, operacion="restaurar_lecturas", conteo=first["restaurados"])
        return result
    finally:
        if created:
            restore.resource.meta.client.delete_table(TableName=restore.table_name)
            restore.resource.meta.client.get_waiter("table_not_exists").wait(
                TableName=restore.table_name, WaiterConfig={"Delay": 1, "MaxAttempts": 30})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", help="Directorio fuera del repositorio; por defecto, temporal del sistema.")
    args = parser.parse_args()
    print(json.dumps(recover_m06(backup_dir=args.backup_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
