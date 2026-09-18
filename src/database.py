"""Conexiones de mínimo privilegio y consultas parametrizadas de lecturas."""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from src.schemas import LecturaEntrada

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

_ROLES_APLICACION = {
    "reader": ("cdrl_reader", "CDRL_READER_PASSWORD"),
    "writer": ("cdrl_writer", "CDRL_WRITER_PASSWORD"),
}


class ErrorLectura(Exception):
    def __init__(self, status: int, codigo: str, mensaje: str):
        super().__init__(mensaje)
        self.status = status
        self.codigo = codigo
        self.mensaje = mensaje


def equipo_inexistente():
    return ErrorLectura(422, "equipo_inexistente", "El equipo indicado no existe.")


def lectura_duplicada():
    return ErrorLectura(
        409, "lectura_duplicada", "Ya existe una lectura del equipo, métrica e instante."
    )


@contextmanager
def conexion(rol: Literal["reader", "writer"]):
    # La ruta elige un rol fijo; ni el cliente ni POSTGRES_USER lo pueden elevar.
    if rol not in _ROLES_APLICACION:
        raise ValueError("Rol de aplicación no permitido.")
    usuario, variable_password = _ROLES_APLICACION[rol]
    password = os.getenv(variable_password)
    if not password:
        # Evita recurrir al administrador, a .pgpass o a autenticación sin clave.
        raise ErrorLectura(
            503,
            "configuracion_no_disponible",
            "La configuración de acceso a la base de datos no está disponible.",
        )

    try:
        with psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            dbname=os.getenv("POSTGRES_DB", "cdrl"),
            user=usuario,
            password=password,
            connect_timeout=3,
            row_factory=dict_row,
        ) as conn:
            yield conn
        # Salir del contexto confirma la transacción antes de responder 201.
    except psycopg.errors.UniqueViolation as exc:
        raise lectura_duplicada() from exc
    except psycopg.errors.ForeignKeyViolation as exc:
        # También cubre una eliminación del equipo durante una escritura.
        raise equipo_inexistente() from exc
    except (psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation) as exc:
        raise ErrorLectura(
            422, "lectura_invalida", "La lectura no cumple el contrato de datos."
        ) from exc
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        raise ErrorLectura(
            503, "base_no_disponible", "No se pudo conectar con la base de datos."
        ) from exc


def buscar_equipo(conn, codigo: str) -> int:
    equipo = conn.execute("SELECT id FROM equipos WHERE codigo = %s", (codigo,)).fetchone()
    if equipo is None:
        raise equipo_inexistente()
    return equipo["id"]


def validar_lectura(lectura: LecturaEntrada) -> int:
    with conexion("reader") as conn:
        equipo_id = buscar_equipo(conn, lectura.equipo_codigo)
        duplicado = conn.execute(
            """SELECT id FROM lecturas
               WHERE equipo_id = %s AND metrica = %s AND medido_en = %s""",
            (equipo_id, lectura.metrica, lectura.medido_en),
        ).fetchone()
        if duplicado is not None:
            raise lectura_duplicada()
    return equipo_id


def guardar_lectura(lectura: LecturaEntrada) -> dict:
    with conexion("writer") as conn:
        equipo_id = buscar_equipo(conn, lectura.equipo_codigo)
        guardada = conn.execute(
            """INSERT INTO lecturas (equipo_id, metrica, unidad, valor, medido_en)
               VALUES (%s, %s, %s, %s, %s)
               RETURNING id, equipo_id, metrica, unidad, valor, medido_en, registrado_en""",
            (equipo_id, lectura.metrica, lectura.unidad, lectura.valor, lectura.medido_en),
        ).fetchone()
        # La restricción UNIQUE arbitra los reintentos incluso si son concurrentes.
    return guardada
