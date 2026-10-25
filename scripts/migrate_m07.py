"""Migración M07 sobre las tablas configuradas (ADR-007); no genera fixtures."""

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botocore.exceptions import BotoCoreError, ClientError

from src.events_store import EventsStore, MIGRACION_ID


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Clasifica sin escribir, ni siquiera auditoría.")
    mode.add_argument("--aplicar", action="store_true", help="Respalda y migra las lecturas v1.")
    mode.add_argument("--rollback", action="store_true", help="Restaura los originales de m07-v1-a-v2.")
    parser.add_argument("--lote", type=int, default=25, help="Documentos evaluados por página (por defecto 25).")
    args = parser.parse_args(argv)
    if args.lote < 1:
        parser.error("--lote debe ser positivo")
    try:
        store = EventsStore()
        if not args.dry_run:
            store.ensure_table()
        if args.rollback:
            report = store.revertir_migracion(MIGRACION_ID, lote=args.lote)
        else:
            report = store.migrar_lecturas_v1(lote=args.lote, dry_run=args.dry_run)
    except (BotoCoreError, ClientError, ValueError) as exc:
        # Los mensajes del SDK pueden incluir endpoints o datos: no exportarlos.
        code = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__
        report = getattr(exc, "reporte", dict(migracion_id=MIGRACION_ID, migradas=0, omitidas=0,
                                              fallidas=1, revertidas=0))
        print(json.dumps(dict(report, resultado="fallo", codigo_error=code), ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(report["version_desconocida"] or report["no_revertible"] or report["fallidas"]))


if __name__ == "__main__":
    raise SystemExit(main())
