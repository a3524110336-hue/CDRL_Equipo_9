"""Contrato de entrada: identidad estable, unidades y rangos por métrica."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


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


class ConsultaLecturas(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equipo: str = Field(
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$",
        description="Código estable de equipos.codigo, por ejemplo edge-01.",
    )
    metrica: Literal["cpu", "memoria", "disco_libre", "latencia", "tasa_error"] | None = None
    desde: AwareDatetime | None = Field(default=None, description="Inicio inclusivo, ISO 8601 con T y zona horaria.")
    hasta: AwareDatetime | None = Field(default=None, description="Fin inclusivo, ISO 8601 con T y zona horaria.")
    limite: int = Field(default=100, ge=1, le=1000, description="Máximo de filas devueltas; en el resumen limita grupos.")

    @field_validator("desde", "hasta", mode="before")
    @classmethod
    def fecha_iso(cls, value):
        if value is None:
            return value
        return LecturaBase.fecha_iso(value)

    @field_validator("limite", mode="before")
    @classmethod
    def limite_entero(cls, value):
        # HTTP envía texto: admitir dígitos, pero no 1.0, 1e2 ni booleanos.
        if type(value) is int or (isinstance(value, str) and value.isascii() and value.isdecimal()):
            return value
        raise ValueError("El límite debe ser un número entero entre 1 y 1000.")

    @model_validator(mode="after")
    def rango_ordenado(self):
        if self.desde is not None and self.hasta is not None and self.desde > self.hasta:
            raise ValueError("desde debe ser anterior o igual a hasta.")
        return self


class ConsultaResumen(ConsultaLecturas):
    desde: AwareDatetime = Field(description="Inicio inclusivo, ISO 8601 con T y zona horaria.")
    hasta: AwareDatetime = Field(description="Fin inclusivo, ISO 8601 con T y zona horaria.")


class ResumenMetricaSalida(BaseModel):
    metrica: str
    unidad: str
    cantidad: int
    promedio: float = Field(allow_inf_nan=False)
    minimo: float
    maximo: float


class LecturaFueraUmbralSalida(LecturaSalida):
    umbral_minimo: float
    umbral_maximo: float


class ErrorDetalle(BaseModel):
    codigo: str
    mensaje: str
    errores: list[dict] | None = None


class ErrorSalida(BaseModel):
    detail: ErrorDetalle
