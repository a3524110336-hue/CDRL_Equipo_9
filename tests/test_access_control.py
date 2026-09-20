"""
Pruebas de separacion de roles y minimo privilegio (M03).

Se conecta directamente a PostgreSQL (no a traves de la API) con las
credenciales de cada rol, para demostrar que es la base de datos misma la
que impone los limites -- no solo el codigo de la API.

Requiere que scripts/rotate_db_passwords.sh ya haya puesto contrasena a los
roles writer, reader y ops, y que las variables CDRL_*_PASSWORD esten
exportadas en el entorno donde corre pytest (scripts/verify_base.sh ya lo
hace).

Incluye:
- 1 caso normal (reader puede leer lo que le corresponde)
- 2 casos limite (el borde exacto de lo que SI puede hacer cada rol:
  writer inserta y lee equipos; ops lee metadatos de migraciones)
- 1 fallo declarado (writer no puede actualizar una lectura)
- 3 pruebas de acceso denegado (reader intenta INSERT; writer intenta DDL;
  ops intenta leer datos de negocio), exactamente las que pide el criterio
  de M03
"""

import os
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

HOST = os.getenv("POSTGRES_HOST", "localhost")
PORT = os.getenv("POSTGRES_PORT", "5432")
DBNAME = os.getenv("POSTGRES_DB", "cdrl")

_ROLES = {
    "writer": "CDRL_WRITER_PASSWORD",
    "reader": "CDRL_READER_PASSWORD",
    "ops": "CDRL_OPS_PASSWORD",
}


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


# --- Caso normal ------------------------------------------------------------

def test_caso_normal_reader_lee_lecturas():
    with _conectar("reader") as conn:
        fila = conn.execute("SELECT id FROM lecturas LIMIT 1").fetchone()
        assert fila is not None


# --- Casos limite: el borde exacto de lo que cada rol SI puede hacer --------

def test_caso_limite_writer_inserta_y_lee_equipos():
    marca = datetime.now(timezone.utc).isoformat()
    with _conectar("writer") as conn:
        equipo = conn.execute("SELECT id FROM equipos LIMIT 1").fetchone()
        assert equipo is not None
        fila = conn.execute(
            """INSERT INTO lecturas (equipo_id, metrica, unidad, valor, medido_en)
               VALUES (%s, 'memoria', 'porcentaje', 55.5, %s)
               RETURNING id""",
            (equipo["id"], marca),
        ).fetchone()
        assert fila is not None


def test_caso_limite_ops_lee_metadatos_de_migraciones():
    with _conectar("ops") as conn:
        fila = conn.execute("SELECT version FROM schema_migrations LIMIT 1").fetchone()
        assert fila is not None


# --- Fallo declarado ---------------------------------------------------------

def test_fallo_declarado_writer_no_puede_actualizar_lecturas():
    """writer puede insertar (arriba) pero nunca editar: una lectura es un
    hecho medido, se corrige con otra lectura, no editandola."""
    with _conectar("writer") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE lecturas SET valor = 0 WHERE false")


# --- Las 3 pruebas negativas de acceso denegado que pide M03 -----------------

def test_acceso_denegado_reader_no_puede_insertar():
    with _conectar("reader") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO lecturas (equipo_id, metrica, unidad, valor, medido_en) "
                "VALUES (1, 'cpu', 'porcentaje', 1, now())"
            )


def test_acceso_denegado_writer_no_puede_hacer_ddl():
    """Crear tablas es competencia exclusiva de cdrl_migrator (migracion 0005)."""
    with _conectar("writer") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE tabla_prohibida_writer (id int)")


def test_acceso_denegado_ops_no_puede_leer_datos_de_negocio():
    with _conectar("ops") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM lecturas LIMIT 1")