"""
Pruebas de las consultas parametrizadas de M02 (GET /lecturas,
/lecturas/resumen, /lecturas/fuera-de-umbral).

Usa el equipo 'edge-01' y el rango de fechas del seed determinista
(2026-09-01, 12 lecturas por métrica cada 5 minutos) para tener resultados
predecibles. No usa fechas de 2026-09-02 para no chocar con lo que insertan
las pruebas de tests/test_api.py.

Incluye:
- 1 caso normal (consulta de umbral con violaciones reales)
- 1 caso vacío (rango de fechas sin datos)
- 2 casos límite (limite=1, y desde==hasta en un instante exacto)
- 1 fallo declarado (equipo inexistente)
"""

import requests

BASE_URL = "http://localhost:8001"
EQUIPO = "edge-01"


def test_caso_normal_lecturas_fuera_de_umbral():
    """cpu en el seed va de 20 a 70 %; el umbral es 25-60 -> hay violaciones."""
    r = requests.get(
        f"{BASE_URL}/lecturas/fuera-de-umbral",
        params={
            "equipo": EQUIPO,
            "metrica": "cpu",
            "desde": "2026-09-01T00:00:00Z",
            "hasta": "2026-09-01T01:00:00Z",
        },
        timeout=5,
    )
    assert r.status_code == 200
    filas = r.json()
    assert len(filas) > 0
    assert filas[0]["umbral_minimo"] == 25.0
    assert filas[0]["umbral_maximo"] == 60.0


def test_caso_vacio_sin_datos_en_rango():
    """Un rango de fechas fuera de todo el seed debe devolver lista vacía, no error."""
    r = requests.get(
        f"{BASE_URL}/lecturas",
        params={
            "equipo": EQUIPO,
            "metrica": "cpu",
            "desde": "2099-01-01T00:00:00Z",
            "hasta": "2099-01-02T00:00:00Z",
        },
        timeout=5,
    )
    assert r.status_code == 200
    assert r.json() == []


def test_caso_limite_minimo_uno():
    """limite=1 (el mínimo permitido, ge=1 en el esquema) debe devolver exactamente 1 fila."""
    r = requests.get(
        f"{BASE_URL}/lecturas",
        params={
            "equipo": EQUIPO,
            "metrica": "cpu",
            "desde": "2026-09-01T00:00:00Z",
            "hasta": "2026-09-01T01:00:00Z",
            "limite": 1,
        },
        timeout=5,
    )
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_caso_limite_resumen_instante_exacto():
    """desde == hasta en un instante exacto del seed: los límites son inclusivos,
    así que debe encontrar exactamente 1 lectura de cpu en ese instante."""
    r = requests.get(
        f"{BASE_URL}/lecturas/resumen",
        params={
            "equipo": EQUIPO,
            "metrica": "cpu",
            "desde": "2026-09-01T00:00:00Z",
            "hasta": "2026-09-01T00:00:00Z",
        },
        timeout=5,
    )
    assert r.status_code == 200
    filas = r.json()
    assert len(filas) == 1
    assert filas[0]["cantidad"] == 1


def test_fallo_declarado_equipo_inexistente():
    """Un equipo que no existe debe rechazarse con 422, no con un error genérico."""
    r = requests.get(
        f"{BASE_URL}/lecturas",
        params={"equipo": "no-existe-99", "limite": 10},
        timeout=5,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["codigo"] == "equipo_inexistente"