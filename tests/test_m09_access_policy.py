"""Contrato de la matriz de acceso de M09 (ADR-009 §2-§4), sin DynamoDB.

Solo revisa lo declarado en db/nosql/access-policy.json, db/nosql/audit-table.json
y db/nosql/iam/: que la matriz sea negada por defecto, que respete las reglas
del ADR y que las politicas IAM sigan derivadas de ella. Que el codigo aplique
la matriz contra DynamoDB Local lo prueba tests/test_m09_security.py.
"""

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "db" / "nosql" / "access-policy.json").read_text(encoding="utf-8"))
AUDIT = json.loads((ROOT / "db" / "nosql" / "audit-table.json").read_text(encoding="utf-8"))
IAM_DIR = ROOT / "db" / "nosql" / "iam"

_spec = importlib.util.spec_from_file_location("build_iam_policies", ROOT / "scripts" / "build_iam_policies.py")
build_iam = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_iam)


def _permisos():
    for rol, spec in POLICY["roles"].items():
        for coleccion, operaciones in spec["permissions"].items():
            for operacion in operaciones:
                yield rol, coleccion, operacion


def test_matriz_negada_por_defecto_y_sin_nombres_sueltos():
    assert POLICY["default"] == "deny"
    assert set(POLICY["dynamodb_actions"]) == set(POLICY["operations"])
    for rol, coleccion, operacion in _permisos():
        assert coleccion in POLICY["collections"], (rol, coleccion)
        assert operacion in POLICY["operations"], (rol, operacion)
    for spec in POLICY["roles"].values():
        for operaciones in spec["permissions"].values():
            assert len(operaciones) == len(set(operaciones))


def test_scan_solo_para_migracion_sobre_lecturas():
    assert {(r, c) for r, c, o in _permisos() if o == "scan"} == {("migracion", "lecturas")}


def test_nadie_escribe_directo_en_auditoria():
    escrituras = {(r, o) for r, c, o in _permisos() if c == "auditoria" and o != "query"}
    assert escrituras == set()
    assert {r for r, c, o in _permisos() if c == "auditoria"} == {"cdrl_auditor"}


def test_writer_no_lee_ni_borra():
    ops = {o for r, c, o in _permisos() if r == "cdrl_writer"}
    assert ops == {"put"}


def test_actores_unicos_sinteticos_y_roles_en_la_auditoria():
    patron = AUDIT["fields"]["patterns"]["actor"]
    actores = [a for spec in POLICY["roles"].values() for a in spec["actors"]]
    assert len(actores) == len(set(actores)), "un actor no puede tener dos roles"
    assert all(re.fullmatch(patron, a) for a in actores)
    assert set(POLICY["roles"]) <= set(AUDIT["fields"]["enums"]["actor_rol"])
    assert "denegada" in AUDIT["fields"]["enums"]["resultado"]
    assert {"actor", "accion", "recurso"} <= set(AUDIT["fields"]["optional"]) | set(AUDIT["fields"]["required"])


def test_accion_y_recurso_cubren_toda_la_matriz():
    accion = AUDIT["fields"]["patterns"]["accion"]
    recurso = AUDIT["fields"]["patterns"]["recurso"]
    for coleccion in POLICY["collections"]:
        assert re.fullmatch(recurso, f"{coleccion}/COLECCION#{coleccion}")
        for operacion in POLICY["operations"]:
            assert re.fullmatch(accion, f"{coleccion}.{operacion}")
    assert not re.fullmatch(accion, "auditoria.truncate")
    assert not re.fullmatch(recurso, "lecturas/EQ#eq-1:clave@host")


def test_politicas_iam_derivadas_de_la_matriz():
    esperado = build_iam.build(POLICY)
    archivos = {p.stem for p in IAM_DIR.glob("*.json")}
    assert archivos == set(esperado)
    for rol, documento in esperado.items():
        assert json.loads((IAM_DIR / f"{rol}.json").read_text(encoding="utf-8")) == documento, rol


def test_politicas_iam_sin_comodines_peligrosos_ni_cuentas_reales():
    for archivo in IAM_DIR.glob("*.json"):
        texto = archivo.read_text(encoding="utf-8")
        documento = json.loads(texto)
        assert not re.search(r"\b\d{12}\b", texto), f"{archivo.name}: id de cuenta"
        for st in documento["Statement"]:
            assert "*" not in st["Action"] and "dynamodb:*" not in st["Action"], archivo.name
        negados = {a for st in documento["Statement"] if st["Effect"] == "Deny" for a in st["Action"]}
        assert {"dynamodb:UpdateItem", "dynamodb:DeleteItem"} <= negados, archivo.name
        if archivo.stem != "migracion":
            assert "dynamodb:Scan" in negados, archivo.name
