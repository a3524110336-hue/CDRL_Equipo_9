"""Rellena GSI2PK de lecturas antiguas según ADR-008, sin cambiar sus claves."""

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botocore.exceptions import BotoCoreError, ClientError

from src.events_store import EventsStore
from src.nosql_access import Principal


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Recorre sin escribir, incluida la auditoría.")
    mode.add_argument("--aplicar", action="store_true", help="Rellena GSI2PK y audita cada lote.")
    args = parser.parse_args(argv)
    try:
        store = EventsStore(principal=Principal(os.getenv("CDRL_ACTOR", "job-backfill"),
                                                os.getenv("CDRL_ROL", "migracion")))
        if args.aplicar:
            store.ensure_table()
        report = store.backfill_gsi2(dry_run=args.dry_run)
    except (BotoCoreError, ClientError, ValueError) as exc:
        code = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__
        report = getattr(exc, "reporte", dict(escaneadas=0, actualizadas=0, ya_indexadas=0, fallidas=1))
        print(json.dumps(dict(report, resultado="fallo", codigo_error=code), ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["fallidas"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
