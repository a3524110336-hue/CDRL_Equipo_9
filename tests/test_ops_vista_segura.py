"""
Pruebas de la vista segura del operador (migracion 0006, pedida en la
revision de M03).

cdrl_ops opera sin ver datos: lee dos vistas agregadas y nada mas. Igual que
tests/test_access_control.py, se conecta directo a PostgreSQL con la
credencial de cada rol, para que sea la base la que impone el limite.

Incluye:
- 1 caso normal (ops lee el estado de la ingesta y cuadra con lo que ve reader)
- 2 casos limite (las columnas son exactamente las declaradas, sin valores;
  el resumen de alertas responde aunque no haya alertas)
- 2 fallos declarados (ops no puede redefinir la vista para ver valores ni
  escribir a traves de ella)
- 2 pruebas de acceso denegado (ops sigue sin leer `alertas`; reader no lee
  las vistas del operador)
"""

import os

import psycopg
import pytest
from psycopg.rows import dict_row

HOST = os.getenv("POSTGRES_HOST", "localhost")
PORT = os.getenv("POSTGRES_PORT", "5432")
DBNAME = os.getenv("POSTGRES_DB", "cdrl")

_ROLES = {
    "reader": "CDRL_READER_PASSWORD",
    "ops": "CDRL_OPS_PASSWORD",
}

# Lo unico que el operador puede ver. Si alguien agrega `valor`, `nombre` o
# `ubicacion` a una vista, estas pruebas fallan.
COLUMNAS_INGESTA = ["equipo", "metrica", "lecturas", "ultima_medicion", "ultimo_registro"]
COLUMNAS_ALERTAS = ["estado", "severidad", "alertas", "mas_antigua", "mas_reciente"]


def _conectar(rol: str):
    variable = _ROLES[rol]
    password = os.getenv(variable)
    if not password:
        pytest.skip(f"{variable} no esta exportada; corre scripts/rotate_db_passwords.sh primero.")
    return psycopg.connect(
        host=HOST,
        port=PORT,
        dbname=DBNAME,
        user=f"cdrl_{rol}",
        password=password,
        connect_timeout=3,
        autocommit=True,
        row_factory=dict_row,
    )


def _columnas(cursor) -> list[str]:
    return [columna.name for columna in cursor.description]


# --- Caso normal ------------------------------------------------------------

def test_caso_normal_ops_ve_la_ingesta_y_cuadra_con_reader():
    with _conectar("ops") as conn:
        filas = conn.execute("SELECT * FROM ops_estado_ingesta").fetchall()
    assert filas, "el seed deja lecturas; la vista no deberia venir vacia"
    total_vista = sum(fila["lecturas"] for fila in filas)

    with _conectar("reader") as conn:
        total_tabla = conn.execute("SELECT count(*) AS n FROM lecturas").fetchone()["n"]
    assert total_vista == total_tabla


# --- Casos limite -----------------------------------------------------------

def test_caso_limite_la_ingesta_solo_expone_columnas_operativas():
    with _conectar("ops") as conn:
        cursor = conn.execute("SELECT * FROM ops_estado_ingesta LIMIT 0")
        assert _columnas(cursor) == COLUMNAS_INGESTA


def test_caso_limite_resumen_de_alertas_responde_sin_datos_de_negocio():
    with _conectar("ops") as conn:
        cursor = conn.execute("SELECT * FROM ops_resumen_alertas")
        assert _columnas(cursor) == COLUMNAS_ALERTAS
        for fila in cursor.fetchall():
            assert fila["estado"] in ("abierta", "reconocida", "cerrada")
            assert fila["alertas"] > 0


# --- Fallo declarado --------------------------------------------------------

def test_fallo_declarado_ops_no_puede_redefinir_la_vista_para_ver_valores():
    """El ataque obvio: reescribir la vista para que exponga `valor`. Solo el
    dueno (cdrl_migrator) puede redefinirla."""
    with _conectar("ops") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "CREATE OR REPLACE VIEW ops_estado_ingesta AS "
                "SELECT e.codigo AS equipo, l.metrica, count(*) AS lecturas, "
                "max(l.medido_en) AS ultima_medicion, max(l.registrado_en) AS ultimo_registro, "
                "max(l.valor) AS valor "
                "FROM lecturas l JOIN equipos e ON e.id = l.equipo_id GROUP BY e.codigo, l.metrica"
            )


def test_fallo_declarado_ops_no_escribe_a_traves_de_la_vista():
    """Una vista agregada no es actualizable: PostgreSQL rechaza el INSERT
    (55000) antes incluso de mirar privilegios. Ops tampoco tiene INSERT."""
    with _conectar("ops") as conn:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            conn.execute(
                "INSERT INTO ops_estado_ingesta (equipo, metrica, lecturas) "
                "VALUES ('edge-01', 'cpu', 1)"
            )


# --- Acceso denegado --------------------------------------------------------

def test_acceso_denegado_ops_sigue_sin_leer_alertas():
    with _conectar("ops") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT valor_observado FROM alertas LIMIT 1")


def test_acceso_denegado_reader_no_lee_las_vistas_del_operador():
    with _conectar("reader") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM ops_estado_ingesta LIMIT 1")
