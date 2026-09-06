"""Contrato de entrada: identidad estable, unidades y rangos por métrica."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class LecturaBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equipo_codigo: str = Field(
        strict=True,
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$",
        description="Código estable de equipos.codigo, por ejemplo edge-01.",
    )
    medido_en: AwareDatetime

    @field_validator("medido_en", mode="before")
    @classmethod
    def fecha_iso(cls, value):
        # No aceptar timestamps numéricos ni cadenas numéricas como fechas.
        if not isinstance(value, (str, datetime)) or (isinstance(value, str) and "T" not in value):
            raise ValueError("Usa una fecha ISO 8601 con T y zona horaria.")
        return value


class LecturaCPU(LecturaBase):
    metrica: Literal["cpu"]
    unidad: Literal["porcentaje"]
    valor: float = Field(strict=True, allow_inf_nan=False, ge=0, le=100)


class LecturaMemoria(LecturaBase):
    metrica: Literal["memoria"]
    unidad: Literal["porcentaje"]
    valor: float = Field(strict=True, allow_inf_nan=False, ge=0, le=100)


class LecturaDisco(LecturaBase):
    metrica: Literal["disco_libre"]
    unidad: Literal["bytes"]
    valor: float = Field(strict=True, allow_inf_nan=False, ge=0)


class LecturaLatencia(LecturaBase):
    metrica: Literal["latencia"]
    unidad: Literal["milisegundos"]
    valor: float = Field(strict=True, allow_inf_nan=False, ge=0)


class LecturaTasaError(LecturaBase):
    metrica: Literal["tasa_error"]
    unidad: Literal["proporcion"]
    valor: float = Field(strict=True, allow_inf_nan=False, ge=0, le=1)


LecturaEntrada = Annotated[
    LecturaCPU | LecturaMemoria | LecturaDisco | LecturaLatencia | LecturaTasaError,
    Field(discriminator="metrica"),
]


class ValidacionSalida(BaseModel):
    valida: Literal[True] = True
    equipo_id: int
    lectura: LecturaEntrada


class LecturaSalida(BaseModel):
    id: int
    equipo_id: int
    metrica: str
    unidad: str
    valor: float
    medido_en: AwareDatetime
    registrado_en: AwareDatetime


class ErrorDetalle(BaseModel):
    codigo: str
    mensaje: str
    errores: list[dict] | None = None


class ErrorSalida(BaseModel):
    detail: ErrorDetalle
