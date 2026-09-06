"""
Pruebas de integración contra la API de lecturas (M01).

Requieren que la API esté corriendo en http://localhost:8001
(scripts/verify_base.sh la levanta automáticamente con Docker antes de
correr estas pruebas).

Incluye:
- 1 caso normal
- 2 casos límite
- 1 fallo declarado
"""

import requests

BASE_URL = "http://localhost:8001"
EQUIPO = "edge-01"  # ya existe en el seed de Persona 1


def _lectura(valor, medido_en):
    return {
        "equipo_codigo": EQUIPO,
        "metrica": "cpu",
        "unidad": "porcentaje",
        "valor": valor,
        "medido_en": medido_en,
    }


def test_caso_normal_cpu_valida_se_guarda():
    payload = _lectura(42.5, "2026-09-02T08:00:00Z")

    r_validar = requests.post(f"{BASE_URL}/lecturas/validar", json=payload, timeout=5)
    assert r_validar.status_code == 200
    assert r_validar.json()["valida"] is True

    r_guardar = requests.post(f"{BASE_URL}/lecturas", json=payload, timeout=5)
    assert r_guardar.status_code == 201
    cuerpo = r_guardar.json()
    assert cuerpo["valor"] == 42.5
    assert cuerpo["equipo_id"] > 0


def test_caso_limite_cpu_valor_minimo_0():
    """Límite inferior exacto del rango permitido (0%): debe aceptarse."""
    payload = _lectura(0, "2026-09-02T09:00:00Z")
    r = requests.post(f"{BASE_URL}/lecturas", json=payload, timeout=5)
    assert r.status_code == 201


def test_caso_limite_cpu_valor_maximo_100():
    """Límite superior exacto del rango permitido (100%): debe aceptarse."""
    payload = _lectura(100, "2026-09-02T10:00:00Z")
    r = requests.post(f"{BASE_URL}/lecturas", json=payload, timeout=5)
    assert r.status_code == 201


def test_fallo_declarado_cpu_fuera_de_rango():
    """Fallo declarado a propósito: 150% de CPU no existe, debe rechazarse."""
    payload = _lectura(150, "2026-09-02T11:00:00Z")
    r = requests.post(f"{BASE_URL}/lecturas", json=payload, timeout=5)
    assert r.status_code == 422
    detalle = r.json()["detail"]
    assert detalle["codigo"] == "lectura_invalida"