"""Control de acceso local de ADR-009; la matriz es la única fuente de permisos."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "db" / "nosql"


class AccesoDenegado(PermissionError):
    """El principal o la operación no están permitidos por la matriz."""


class AuditoriaInvalida(ValueError):
    """El evento no cumple el contrato seguro de auditoría."""


def _politica():
    return json.loads((_ROOT / "access-policy.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Principal:
    actor: str
    rol: str

    def __post_init__(self):
        _validar_principal(self)


def _validar_principal(principal):
    if not isinstance(principal, Principal):
        raise AccesoDenegado("Principal obligatorio.")
    if not isinstance(principal.rol, str) or not isinstance(principal.actor, str):
        raise AccesoDenegado("Actor y rol inválidos.")
    role = _politica()["roles"].get(principal.rol)
    if (role is None or principal.actor not in role["actors"]
            or ("environments" in role
                and os.getenv("CDRL_ENTORNO") not in role["environments"])):
        raise AccesoDenegado("Principal fuera de la matriz o del entorno permitido.")
    return role


def cargar_principal() -> Principal:
    return Principal(os.getenv("CDRL_ACTOR", ""), os.getenv("CDRL_ROL", ""))


def autorizar(principal: Principal, coleccion: str, operacion: str) -> None:
    role = _validar_principal(principal)
    if operacion not in role["permissions"].get(coleccion, []):
        raise AccesoDenegado("Operación no permitida por la matriz.")


def sanear_evento(evento: dict) -> dict:
    """Rechaza datos sensibles, incluso en estructuras anidadas; nunca enmascara."""
    if not isinstance(evento, dict):
        raise AuditoriaInvalida("La auditoría requiere un documento.")
    fields = json.loads((_ROOT / "audit-table.json").read_text(encoding="utf-8"))["fields"]
    forbidden = {name.lower() for name in fields["forbidden_examples"]}
    secret = re.compile(r"AKIA[0-9A-Z]*|://[^\s/:]+:[^\s@]+@|password\s*=|secret|token|bearer\s+", re.I)

    def revisar(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or key.lower() in forbidden or secret.search(key):
                    raise AuditoriaInvalida("Llave prohibida en auditoría.")
                revisar(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                revisar(child)
        elif isinstance(value, str) and secret.search(value):
            raise AuditoriaInvalida("Texto prohibido en auditoría.")

    revisar(evento)
    if set(evento) - set(fields["required"] + fields["optional"]):
        raise AuditoriaInvalida("Campo fuera de la lista blanca.")
    return dict(evento)
