"""Endpoints de validación y persistencia de lecturas."""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src import database
from src.schemas import ErrorSalida, LecturaEntrada, LecturaSalida, ValidacionSalida

app = FastAPI(
    title="CDRL — API de lecturas",
    version="0.1.0",
    description="API de validación y persistencia de lecturas. Autor: Marco Antonio Osorio Hernandez.",
)


@app.exception_handler(database.ErrorLectura)
async def error_lectura(request: Request, exc: database.ErrorLectura):
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"codigo": exc.codigo, "mensaje": exc.mensaje}},
    )


@app.exception_handler(RequestValidationError)
async def error_validacion(request: Request, exc: RequestValidationError):
    # Los valores originales pueden contener NaN/Infinity, que no son JSON válido.
    errores = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "codigo": "lectura_invalida",
                "mensaje": "La lectura no cumple el contrato de datos.",
                "errores": errores,
            }
        },
    )


ERRORES = {
    409: {"model": ErrorSalida, "description": "Lectura duplicada."},
    422: {"model": ErrorSalida, "description": "Lectura inválida o equipo inexistente."},
    503: {"model": ErrorSalida, "description": "Conexión con PostgreSQL no disponible."},
}


@app.post("/lecturas/validar", response_model=ValidacionSalida, responses=ERRORES)
def validar(lectura: LecturaEntrada):
    """Valida el contrato, la existencia del equipo y los duplicados, sin guardar."""
    equipo_id = database.validar_lectura(lectura)
    return {"valida": True, "equipo_id": equipo_id, "lectura": lectura}


@app.post(
    "/lecturas",
    status_code=status.HTTP_201_CREATED,
    response_model=LecturaSalida,
    responses=ERRORES,
)
def guardar(lectura: LecturaEntrada):
    """Valida y guarda una lectura en una transacción; no requiere validar antes."""
    return database.guardar_lectura(lectura)
