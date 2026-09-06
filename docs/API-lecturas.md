# API y validación de lecturas

Autor: Marco Antonio Osorio Hernandez. GitHub: [a3524110303-cpu](https://github.com/a3524110303-cpu).

## Contrato en una frase

Una lectura contiene `equipo_codigo`, `metrica`, `unidad`, `valor` y `medido_en`: el equipo debe existir, la métrica y su unidad deben coincidir, el valor debe ser finito y estar en su rango, la fecha debe incluir zona horaria, y no se permiten campos extra ni repetir equipo, métrica e instante.

## Arranque local con Python

Requiere Python 3.11 o posterior y un PostgreSQL con las migraciones y el seed de Persona 1 aplicados. La configuración usa las variables `POSTGRES_*` de `.env.example`; puedes copiarlo a `.env`. Las variables del entorno tienen prioridad sobre `.env`.

PowerShell, desde la carpeta del repositorio:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn src.main:app --host 127.0.0.1 --port 8001
```

En Linux/macOS, desde la raíz del repositorio:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8001
```

La API escucha en http://localhost:8001. Abre http://localhost:8001/docs para probar los endpoints; http://localhost:8001/openapi.json contiene el contrato ejecutable. Se usa 8001 porque DynamoDB local ya ocupa 8000. Para otro puerto, invoca Uvicorn con `--port`.

## Solicitud

Los dos endpoints reciben exactamente los mismos cinco campos:

```json
{
  "equipo_codigo": "edge-01",
  "metrica": "cpu",
  "unidad": "porcentaje",
  "valor": 42.5,
  "medido_en": "2026-09-06T12:00:00Z"
}
```

`equipo_codigo` corresponde al código estable de `equipos.codigo`, de 3 a 63 caracteres, con minúsculas ASCII, dígitos y guiones interiores. Persona 1 identifica ese código como la identidad que envía el agente; la API resuelve su `equipo_id` interno. `id`, `equipo_id` y `registrado_en` no se envían: se resuelven o generan en el servidor.

| Métrica | Unidad exacta | Rango inclusivo |
| --- | --- | --- |
| cpu | porcentaje | 0 a 100 |
| memoria | porcentaje | 0 a 100 |
| disco_libre | bytes | 0 en adelante |
| latencia | milisegundos | 0 en adelante |
| tasa_error | proporcion | 0 a 1 |

El SQL de Persona 1 fija métricas, unidades y finitud. La API agrega los límites físicos anteriores; los valores de ejemplo del seed no son límites del contrato. Todos los campos son obligatorios: se rechazan nulos, números como texto, booleanos como números, NaN e infinitos. `medido_en` exige una fecha ISO 8601 con `T` y zona horaria explícita. No se añaden reglas sobre fechas futuras ni equipos inactivos, que no están restringidos en el esquema compartido.

Ejemplo de validación en PowerShell:

```powershell
$lectura = @{
    equipo_codigo = 'edge-01'
    metrica = 'cpu'
    unidad = 'porcentaje'
    valor = 42.5
    medido_en = '2026-09-06T12:00:00Z'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://localhost:8001/lecturas/validar' -ContentType 'application/json' -Body $lectura
Invoke-RestMethod -Method Post -Uri 'http://localhost:8001/lecturas' -ContentType 'application/json' -Body $lectura
```

La primera solicitud responde 200 con `valida`, `equipo_id` y `lectura`, sin guardar. La segunda responde 201 con la fila persistida: `id`, `equipo_id`, `metrica`, `unidad`, `valor`, `medido_en` y `registrado_en`. Repetir la escritura, o validar una lectura ya existente, responde 409.

Todos los errores de contrato usan esta estructura; los errores de campos también contienen `detail.errores` con su ubicación y descripción:

```json
{
  "detail": {
    "codigo": "lectura_duplicada",
    "mensaje": "Ya existe una lectura del equipo, métrica e instante."
  }
}
```

| Condición | HTTP | `detail.codigo` |
| --- | --- | --- |
| Lectura o JSON inválidos | 422 | lectura_invalida |
| Equipo inexistente | 422 | equipo_inexistente |
| Mismo equipo, métrica e instante | 409 | lectura_duplicada |
| Conexión no disponible o interrumpida | 503 | base_no_disponible |

Una lectura mal formada responde 422 antes de consultar PostgreSQL. Validar no reserva el instante: la escritura posterior todavía puede devolver 409. La restricción `lecturas_sin_duplicados` arbitra las escrituras concurrentes. PostgreSQL reconoce el mismo instante aunque se envíe con offsets distintos.

El guardado confirma la transacción antes de responder 201 y revierte si falla. Si se pierde la conexión durante el commit, el cliente puede desconocer si se confirmó: reintentar la misma clave devuelve 201 o 409 sin duplicar la lectura. Los mensajes no exponen SQL ni credenciales. Un esquema sin migrar requiere corregir la preparación de la base.

## Integración para Persona 3

El punto de entrada es `src.main:app` y las dependencias de ejecución están en `requirements.txt`. La API lee `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` y `POSTGRES_PASSWORD` de las variables del entorno o del `.env` local existente; la conexión tiene un timeout de tres segundos.

Para ejecutarla dentro de un contenedor, el comando es `python -m uvicorn src.main:app --host 0.0.0.0 --port 8001`. Persona 3 integra este comando con Docker, `make setup/verify/run`, sus pruebas y el pipeline. Persona 1 proporciona las migraciones y el seed; la API no los ejecuta al arrancar.

Los escenarios para la verificación del equipo son: lectura válida (200 al validar y 201 al guardar), extremos inclusivos de cada métrica, dato fuera de rango o campo extra (422), lectura repetida (409), equipo inexistente (422) y base detenida (503). La preparación de la evidencia de M01, el ADR de stack y el tag `week-01-final` corresponden a Persona 3.
