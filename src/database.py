"""Consultas parametrizadas sobre las migraciones de Persona 1."""

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from src.schemas import LecturaEntrada

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


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
def conexion():
    try:
        with psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            dbname=os.getenv("POSTGRES_DB", "cdrl"),
            user=os.getenv("POSTGRES_USER", "cdrl_dev"),
            password=os.getenv("POSTGRES_PASSWORD", "cdrl_dev_only"),
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
    with conexion() as conn:
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
    with conexion() as conn:
        equipo_id = buscar_equipo(conn, lectura.equipo_codigo)
        guardada = conn.execute(
            """INSERT INTO lecturas (equipo_id, metrica, unidad, valor, medido_en)
               VALUES (%s, %s, %s, %s, %s)
               RETURNING id, equipo_id, metrica, unidad, valor, medido_en, registrado_en""",
            (equipo_id, lectura.metrica, lectura.unidad, lectura.valor, lectura.medido_en),
        ).fetchone()
        # La restricción UNIQUE arbitra los reintentos incluso si son concurrentes.
    return guardada
