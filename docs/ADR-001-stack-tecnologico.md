# ADR-001 — Stack tecnológico: Python + FastAPI + PostgreSQL

## Estado
Aceptado.

## Contexto
El equipo necesita un lenguaje y framework para exponer los endpoints que
reciben y validan lecturas de telemetría, compatible con el esquema
relacional definido por Persona 1 (tablas `equipos` y `lecturas`).

## Decisión
Se usa **Python 3.12** con **FastAPI**, **Pydantic** para el contrato de
entrada (`src/schemas.py`) y **psycopg** para conectar con PostgreSQL
(`src/database.py`). FastAPI valida automáticamente tipos y rangos con
Pydantic, y expone documentación interactiva en `/docs`.

## Alternativas consideradas
- **Flask**: descartado por no traer validación de esquemas integrada.
- **Node.js/Express**: descartado por menor familiaridad del equipo con
  validación de esquemas equivalente a Pydantic.

## Consecuencias
- La API corre en el puerto 8001 (8000 ya lo usa DynamoDB local).
- Las pruebas automáticas (`tests/`) llaman a la API por HTTP.
- La API no aplica migraciones ni seed al arrancar — eso lo hace
  `scripts/verify_base.sh` antes de levantar el contenedor `app`.