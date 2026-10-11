"""Pruebas M03 pendientes: el migrador SI puede hacer DDL y el escritor NO
puede borrar lecturas ni leer umbrales.

Misma tecnica que tests/test_access_control.py: conexion directa a PostgreSQL
con las credenciales de cada rol (el limite lo impone la base, no la API).
Requiere scripts/rotate_db_passwords.sh con el rol migrator; scripts/verify_base.sh
ya lo hace y exporta CDRL_MIGRATOR_PASSWORD y CDRL_WRITER_PASSWORD.
"""

import os
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

HOST = os.getenv("POSTGRES_HOST", "localhost")
PORT = os.getenv("POSTGRES_PORT", "5432")
DBNAME = os.getenv("POSTGRES_DB", "cdrl")

_ROLES = {
    "migrator": "CDRL_MIGRATOR_PASSWORD",
    "writer": "CDRL_WRITER_PASSWORD",
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


# --- Migrador: lo que SI puede hacer -----------------------------------------

def test_migrador_puede_hacer_ddl():
    """cdrl_migrator crea, altera y borra una tabla: es el unico rol con DDL."""
    tabla = f"tabla_prueba_migrador_{uuid.uuid4().hex[:8]}"
    with _conectar("migrator") as conn:
        try:
            conn.execute(f"CREATE TABLE {tabla} (id int PRIMARY KEY)")
            conn.execute(f"ALTER TABLE {tabla} ADD COLUMN nota text")
            columnas = {
                fila["column_name"]
                for fila in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                    (tabla,),
                ).fetchall()
            }
            assert columnas == {"id", "nota"}
        finally:
            conn.execute(f"DROP TABLE IF EXISTS {tabla}")
        existe = conn.execute("SELECT to_regclass(%s) AS tabla", (tabla,)).fetchone()
        assert existe["tabla"] is None


def test_limite_migrador_es_dueno_de_las_tablas_de_negocio():
    """Borde: el DDL no se otorga en PostgreSQL, es del dueno. La migracion
    0005 traspasa las cinco tablas a cdrl_migrator; si no, no podria alterarlas."""
    esperadas = {"schema_migrations", "equipos", "lecturas", "umbrales", "alertas"}
    with _conectar("migrator") as conn:
        duenos = {
            fila["tablename"]: fila["tableowner"]
            for fila in conn.execute(
                "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
    for tabla in esperadas:
        assert duenos.get(tabla) == "cdrl_migrator", f"{tabla} no es de cdrl_migrator"


# --- Escritor: lo que NO puede hacer -----------------------------------------

def test_acceso_denegado_writer_no_puede_borrar_lecturas():
    """writer inserta, pero una lectura es un hecho medido: no se borra."""
    with _conectar("writer") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM lecturas WHERE false")


def test_acceso_denegado_writer_no_puede_leer_umbrales():
    """Los umbrales son configuracion de alertas, no parte de la ruta de escritura."""
    with _conectar("writer") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM umbrales LIMIT 1")