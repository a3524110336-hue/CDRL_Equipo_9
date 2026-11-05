"""Deriva las politicas IAM de db/nosql/iam/ desde db/nosql/access-policy.json.

La matriz es la unica fuente: este script no decide permisos, solo los traduce.
Con --check falla si algun archivo de db/nosql/iam/ no coincide con lo que se
generaria (asi la matriz y las politicas no pueden separarse sin que se note).

DynamoDB Local no evalua IAM y el Learner Lab solo deja usar LabRole, asi que
estas politicas se validan por estructura, no aplicandolas (ADR-009 seccion 4).
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "db" / "nosql" / "access-policy.json"
IAM_DIR = ROOT / "db" / "nosql" / "iam"
AUDIT_TABLE_ACTIONS = ["dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"]


def _arn(table: str, index: bool = False) -> str:
    # Region y cuenta van como comodin: ningun identificador real entra al repo.
    return f"arn:aws:dynamodb:*:*:table/{table}" + ("/index/*" if index else "")


def build(policy: dict) -> dict[str, dict]:
    collections = policy["collections"]
    audit_table = collections["auditoria"]["default_table"]
    result = {}
    for role, spec in policy["roles"].items():
        statements = []
        for collection, operations in spec["permissions"].items():
            meta = collections[collection]
            actions = sorted({a for op in operations for a in policy["dynamodb_actions"][op]})
            resources = [_arn(meta["default_table"])]
            if "query" in operations and meta["indexes"]:
                resources.append(_arn(meta["default_table"], index=True))
            statement = {
                "Sid": f"{collection.title().replace('_', '')}Permitido",
                "Effect": "Allow",
                "Action": actions,
                "Resource": resources,
            }
            # LeadingKeys no aplica a Scan: recorre la tabla sin clave de particion.
            if "scan" not in operations:
                statement["Condition"] = {
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": meta["leading_keys"]}}
            statements.append(statement)
        # Todos anexan eventos: sus escrituras, sus intentos negados y sus
        # consultas a la auditoria. IAM no distingue el anexo del almacen de un
        # put directo; esa diferencia la impone la matriz en el codigo.
        statements.append({
            "Sid": "AuditoriaSoloAnexar",
            "Effect": "Allow",
            "Action": ["dynamodb:PutItem"],
            "Resource": [_arn(audit_table)],
            "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["AUD#*", "OP#*"]}},
        })
        statements.append({
            "Sid": "AuditoriaInmutable",
            "Effect": "Deny",
            "Action": AUDIT_TABLE_ACTIONS,
            "Resource": [_arn(audit_table), _arn(audit_table, index=True)],
        })
        scan_tables = {collections[c]["default_table"] for c, ops in spec["permissions"].items() if "scan" in ops}
        denied_scan = sorted({m["default_table"] for m in collections.values()} - scan_tables)
        if denied_scan:
            statements.append({
                "Sid": "SinScan",
                "Effect": "Deny",
                "Action": ["dynamodb:Scan"],
                "Resource": [_arn(t) for t in denied_scan] + [_arn(t, index=True) for t in denied_scan],
            })
        result[role] = {"Version": "2012-10-17", "Statement": statements}
    return result


def _render(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="solo comparar, sin escribir")
    args = parser.parse_args()
    expected = {f"{role}.json": _render(doc)
                for role, doc in build(json.loads(POLICY_PATH.read_text(encoding="utf-8"))).items()}
    if args.check:
        current = {p.name: p.read_text(encoding="utf-8") for p in IAM_DIR.glob("*.json")} if IAM_DIR.exists() else {}
        diff = sorted(set(expected) ^ set(current) | {n for n in expected if current.get(n) != expected[n]})
        if diff:
            print("Politicas IAM desactualizadas: " + ", ".join(diff), file=sys.stderr)
            return 1
        print(f"Politicas IAM al dia ({len(expected)} roles).")
        return 0
    IAM_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in expected.items():
        (IAM_DIR / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"Escritas {len(expected)} politicas en {IAM_DIR.relative_to(ROOT).as_posix()}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
