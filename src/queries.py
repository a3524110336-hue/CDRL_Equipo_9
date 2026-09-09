"""Consultas de M02: SQL constante y valores enlazados mediante psycopg.

La consulta de umbrales depende del contrato provisional documentado en
docs/API-lecturas.md; debe contrastarse con las migraciones de Jonathan.
"""

import psycopg

from src import database
from src.schemas import ConsultaLecturas, ConsultaResumen


ULTIMAS_LECTURAS = """
    SELECT l.id, l.equipo_id, l.metrica, l.unidad, l.valor,
           l.medido_en, l.registrado_en
    FROM lecturas AS l
    WHERE l.equipo_id = %s
      AND (%s::text IS NULL OR l.metrica = %s)
      AND (%s::timestamptz IS NULL OR l.medido_en >= %s)
      AND (%s::timestamptz IS NULL OR l.medido_en <= %s)
    ORDER BY l.medido_en DESC, l.id DESC
    LIMIT %s
"""

RESUMEN_POR_METRICA = """
    SELECT l.metrica, l.unidad, COUNT(*) AS cantidad,
           -- text conserva los dígitos de float8 antes de acumular en numeric;
           -- evita desbordar la suma o redondear el máximo finito a infinito.
           AVG(l.valor::text::numeric) AS promedio,
           MIN(l.valor) AS minimo, MAX(l.valor) AS maximo
    FROM lecturas AS l
    WHERE l.equipo_id = %s
      AND (%s::text IS NULL OR l.metrica = %s)
      AND l.medido_en >= %s AND l.medido_en <= %s
    GROUP BY l.metrica, l.unidad
    ORDER BY l.metrica ASC
    LIMIT %s
"""

LECTURAS_FUERA_DE_UMBRAL = """
    SELECT l.id, l.equipo_id, l.metrica, l.unidad, l.valor,
           l.medido_en, l.registrado_en,
           u.minimo AS umbral_minimo, u.maximo AS umbral_maximo
    FROM lecturas AS l
    JOIN umbrales AS u ON u.metrica = l.metrica
    WHERE l.equipo_id = %s
      AND (%s::text IS NULL OR l.metrica = %s)
      AND (%s::timestamptz IS NULL OR l.medido_en >= %s)
      AND (%s::timestamptz IS NULL OR l.medido_en <= %s)
      AND (l.valor < u.minimo OR l.valor > u.maximo)
    ORDER BY l.medido_en DESC, l.id DESC
    LIMIT %s
"""


def _parametros_lecturas(equipo_id: int, filtros: ConsultaLecturas) -> tuple:
    return (
        equipo_id,
        filtros.metrica, filtros.metrica,
        filtros.desde, filtros.desde,
        filtros.hasta, filtros.hasta,
        filtros.limite,
    )


def ultimas_lecturas(filtros: ConsultaLecturas) -> list[dict]:
    with database.conexion() as conn:
        equipo_id = database.buscar_equipo(conn, filtros.equipo)
        return conn.execute(ULTIMAS_LECTURAS, _parametros_lecturas(equipo_id, filtros)).fetchall()


def resumen_por_metrica(filtros: ConsultaResumen) -> list[dict]:
    with database.conexion() as conn:
        equipo_id = database.buscar_equipo(conn, filtros.equipo)
        return conn.execute(
            RESUMEN_POR_METRICA,
            (equipo_id, filtros.metrica, filtros.metrica, filtros.desde, filtros.hasta, filtros.limite),
        ).fetchall()


def lecturas_fuera_de_umbral(filtros: ConsultaLecturas) -> list[dict]:
    try:
        with database.conexion() as conn:
            equipo_id = database.buscar_equipo(conn, filtros.equipo)
            return conn.execute(
                LECTURAS_FUERA_DE_UMBRAL, _parametros_lecturas(equipo_id, filtros)
            ).fetchall()
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
        raise database.ErrorLectura(
            503, "modelo_no_disponible", "El esquema de umbrales M02 no está disponible."
        ) from exc
