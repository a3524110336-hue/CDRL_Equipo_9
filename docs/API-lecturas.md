# API, validación y consultas de lecturas

Autor: Marco Antonio Osorio Hernandez. GitHub: [a3524110303-cpu](https://github.com/a3524110303-cpu).

## Contrato en una frase

Una lectura contiene `equipo_codigo`, `metrica`, `unidad`, `valor` y `medido_en`: el equipo debe existir, la métrica y su unidad deben coincidir, el valor debe ser finito y estar en su rango, la fecha debe incluir zona horaria, y no se permiten campos extra ni repetir equipo, métrica e instante.

## Arranque local con Python

Requiere Python 3.11 o posterior y un PostgreSQL con las migraciones y el seed de Jonathan aplicados. La configuración usa las variables `POSTGRES_*` de `.env.example`; puedes copiarlo a `.env`. Las variables del entorno tienen prioridad sobre `.env`.

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

## Escritura y validación de M01

`POST /lecturas/validar` y `POST /lecturas` reciben exactamente los mismos cinco campos:

```json
{
  "equipo_codigo": "edge-01",
  "metrica": "cpu",
  "unidad": "porcentaje",
  "valor": 42.5,
  "medido_en": "2026-09-06T12:00:00Z"
}
```

`equipo_codigo` corresponde al código estable de `equipos.codigo`, de 3 a 63 caracteres, con minúsculas ASCII, dígitos y guiones interiores. Jonathan identifica ese código como la identidad que envía el agente; la API resuelve su `equipo_id` interno. `id`, `equipo_id` y `registrado_en` no se envían: se resuelven o generan en el servidor.

| Métrica | Unidad exacta | Rango inclusivo |
| --- | --- | --- |
| cpu | porcentaje | 0 a 100 |
| memoria | porcentaje | 0 a 100 |
| disco_libre | bytes | 0 en adelante |
| latencia | milisegundos | 0 en adelante |
| tasa_error | proporcion | 0 a 1 |

El SQL de Jonathan fija métricas, unidades y finitud. La API agrega los límites físicos anteriores; los valores de ejemplo del seed no son límites del contrato. Todos los campos son obligatorios: se rechazan nulos, números como texto, booleanos como números, NaN e infinitos. `medido_en` exige una fecha ISO 8601 con `T` y zona horaria explícita. No se añaden reglas sobre fechas futuras ni equipos inactivos, que no están restringidos en el esquema compartido.

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
| Lectura, JSON o parámetros de consulta inválidos | 422 | lectura_invalida |
| Equipo inexistente | 422 | equipo_inexistente |
| Mismo equipo, métrica e instante | 409 | lectura_duplicada |
| Conexión no disponible o interrumpida | 503 | base_no_disponible |
| Tabla o columnas de umbrales no disponibles para consultar violaciones | 503 | modelo_no_disponible |

Una lectura mal formada responde 422 antes de consultar PostgreSQL. Validar no reserva el instante: la escritura posterior todavía puede devolver 409. La restricción `lecturas_sin_duplicados` arbitra las escrituras concurrentes. PostgreSQL reconoce el mismo instante aunque se envíe con offsets distintos.

El guardado confirma la transacción antes de responder 201 y revierte si falla. Si se pierde la conexión durante el commit, el cliente puede desconocer si se confirmó: reintentar la misma clave devuelve 201 o 409 sin duplicar la lectura. Los mensajes no exponen SQL ni credenciales. Un esquema sin migrar requiere corregir la preparación de la base.

## Consultas parametrizadas de M02

Los tres endpoints GET reciben parámetros en la URL, sin cuerpo JSON. Las respuestas exitosas siempre son listas JSON. Un equipo existente sin lecturas que coincidan responde **200 con `[]`**, también en el resumen. Un código válido que no existe en `equipos` responde 422 con `detail.codigo = "equipo_inexistente"`.

| Parámetro | Contrato |
| --- | --- |
| `equipo` | Obligatorio en los tres GET. Código estable con las mismas reglas que `equipo_codigo` de M01; por ejemplo `edge-01`. |
| `metrica` | Opcional. Una de `cpu`, `memoria`, `disco_libre`, `latencia`, `tasa_error`. Al omitirla, se consultan todas las métricas. |
| `desde` | Fecha ISO 8601 con `T` y zona horaria. Obligatoria para `/lecturas/resumen`; opcional en los otros GET. Incluye las lecturas de ese instante. |
| `hasta` | Mismo formato y obligatoriedad que `desde`. Incluye las lecturas de ese instante. |
| `limite` | Entero de 1 a 1000, inclusivos; predeterminado 100. En el resumen limita grupos de métricas, sin recortar las lecturas usadas por los agregados. |

Cuando se proporcionan ambas fechas, `desde` debe ser menor o igual a `hasta`. Se comparan instantes con zona horaria: offsets diferentes que representan el mismo instante son equivalentes. En los GET donde las fechas son opcionales, omitir un extremo deja ese lado del rango abierto. Enviar una cadena vacía no equivale a omitir un parámetro. Las fechas sin zona, fechas numéricas, métricas desconocidas, límites fuera de rango y parámetros adicionales se rechazan con 422 antes de consultar la base.

Los errores de parámetros conservan el formato de M01: `detail.codigo` es `lectura_invalida`, `detail.mensaje` es `La lectura no cumple el contrato de datos.` y `detail.errores` describe los campos inválidos. Por compatibilidad, ese código y mensaje también se utilizan para consultas GET.

### Últimas N lecturas de un equipo

`GET /lecturas` aplica los filtros y devuelve hasta `limite` lecturas, de la más reciente a la más antigua por `medido_en DESC`. Los empates se resuelven por `id DESC`. Cada objeto contiene los mismos campos que la respuesta de `POST /lecturas`: `id`, `equipo_id`, `metrica`, `unidad`, `valor`, `medido_en`, `registrado_en`.

Ejemplo con curl en Linux/macOS:

```sh
curl --get 'http://localhost:8001/lecturas' \
  --data-urlencode 'equipo=edge-01' \
  --data-urlencode 'metrica=cpu' \
  --data-urlencode 'desde=2026-09-03T00:00:00Z' \
  --data-urlencode 'hasta=2026-09-03T23:59:59Z' \
  --data-urlencode 'limite=2'
```

`--data-urlencode` conserva correctamente los signos `+` de offsets como `+00:00`; pegarlos sin codificar en una URL puede convertirlos en espacios. En PowerShell se puede usar `curl.exe` para invocar el ejecutable curl, o el ejemplo de `Invoke-RestMethod` mostrado más adelante.

### Resumen por métrica y rango de fechas

`GET /lecturas/resumen` requiere `desde` y `hasta`. Agrupa todas las lecturas coincidentes por `metrica` y `unidad`, calcula `COUNT`, `AVG`, `MIN` y `MAX`, y ordena los grupos por `metrica ASC`. `limite=1` devuelve el primer grupo completo; no calcula estadísticas sobre una sola lectura.

```sh
curl --get 'http://localhost:8001/lecturas/resumen' \
  --data-urlencode 'equipo=edge-01' \
  --data-urlencode 'metrica=cpu' \
  --data-urlencode 'desde=2026-09-03T00:00:00Z' \
  --data-urlencode 'hasta=2026-09-03T23:59:59Z' \
  --data-urlencode 'limite=1'
```

Con exactamente tres lecturas CPU de valores 10, 20 y 30 en ese rango, el resultado esperado es:

```json
[
  {
    "metrica": "cpu",
    "unidad": "porcentaje",
    "cantidad": 3,
    "promedio": 20.0,
    "minimo": 10.0,
    "maximo": 30.0
  }
]
```

Sin lecturas coincidentes, el resultado es `[]`; no se crea una fila artificial con agregados nulos.

### Lecturas que violan umbrales

`GET /lecturas/fuera-de-umbral` consulta el umbral vigente de cada métrica y devuelve lecturas con `valor < minimo` o `valor > maximo`. Los valores exactamente iguales a cualquiera de los extremos se consideran dentro del umbral. El orden y el límite coinciden con `GET /lecturas`.

Cada objeto contiene los campos de `LecturaSalida` y agrega `umbral_minimo` y `umbral_maximo`. Una métrica sin umbral registrado no produce violaciones. Esta consulta es de solo lectura: no genera ni modifica registros en `alertas`.

```sh
curl --get 'http://localhost:8001/lecturas/fuera-de-umbral' \
  --data-urlencode 'equipo=edge-01' \
  --data-urlencode 'metrica=cpu' \
  --data-urlencode 'desde=2026-09-03T00:00:00Z' \
  --data-urlencode 'hasta=2026-09-03T23:59:59Z' \
  --data-urlencode 'limite=100'
```

**Dependencia de integración pendiente:** al sincronizar el repositorio en `c117ba2`, todavía no estaban publicadas las migraciones M02 de Jonathan. Esta consulta utiliza el contrato provisional `umbrales(metrica, minimo, maximo)`: un umbral global por métrica (`metrica UNIQUE`), extremos numéricos finitos no nulos y `minimo <= maximo`. Jonathan debe confirmar los nombres y restricciones con sus migraciones 0003/0004 y su seed. Si cambian, hay que adaptar la consulta antes de declarar integrada la entrega. La API no crea tablas ni aplica migraciones.

Si faltan la tabla de umbrales o las columnas que necesita esta consulta, responde 503 con `detail.codigo = "modelo_no_disponible"`. Si el esquema existe y no hay violaciones, responde 200 con `[]`. Una falla de conexión conserva 503 con `base_no_disponible`.

### Consultar desde PowerShell

Este ejemplo codifica los parámetros, incluidos los offsets positivos, y puede usarse con cualquiera de los tres GET cambiando `$ruta`:

```powershell
$ruta = '/lecturas/resumen'
$parametros = [ordered]@{
    equipo = 'edge-01'
    metrica = 'cpu'
    desde = '2026-09-03T00:00:00+00:00'
    hasta = '2026-09-03T23:59:59+00:00'
    limite = '100'
}
$consulta = ($parametros.GetEnumerator() | ForEach-Object {
    [uri]::EscapeDataString([string]$_.Key) + '=' +
    [uri]::EscapeDataString([string]$_.Value)
}) -join '&'
Invoke-RestMethod -Method Get -Uri ('http://localhost:8001' + $ruta + '?' + $consulta)
```

### Seguridad de las consultas

`src/queries.py` contiene las consultas GET. Los valores se pasan por separado a `execute()` mediante marcadores `%s`, incluido `LIMIT`; los nombres de tablas, columnas y criterios de orden son constantes del código. No se construye SQL con f-strings, concatenaciones ni interpolación de valores. Los filtros opcionales utilizan casts explícitos para que PostgreSQL pueda determinar el tipo cuando un parámetro es nulo. Este uso corresponde a [Passing parameters to SQL queries, documentación de psycopg](https://www.psycopg.org/psycopg3/docs/basic/params.html).

Los modelos de parámetros concentran tipos, rangos y rechazo de campos adicionales mediante `extra="forbid"`, siguiendo [Query Parameter Models, documentación de FastAPI](https://fastapi.tiangolo.com/tutorial/query-param-models/).

## Integración para Alejandro

El punto de entrada es `src.main:app` y las dependencias de ejecución están en `requirements.txt`. La API lee `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` y `POSTGRES_PASSWORD` de las variables del entorno o del `.env` local existente; la conexión tiene un timeout de tres segundos.

Para ejecutarla dentro de un contenedor, el comando es `python -m uvicorn src.main:app --host 0.0.0.0 --port 8001`. Alejandro integra este comando con Docker, `make setup/verify/run`, sus pruebas y el pipeline. Jonathan proporciona las migraciones y el seed; la API no los ejecuta al arrancar.

Los escenarios de M01 para la verificación del equipo siguen siendo: lectura válida (200 al validar y 201 al guardar), extremos inclusivos de cada métrica, dato fuera de rango o campo extra (422), lectura repetida (409), equipo inexistente (422) y base detenida (503). La preparación de la evidencia de M01, el ADR de stack y el tag `week-01-final` corresponden a Alejandro.

### Propuesta de casos M02 para la integración del equipo

La siguiente propuesta permite comprobar resultados exactos en una base de pruebas preparada por el equipo. No constituye una ejecución de las pruebas oficiales ni evidencia de integración final. Para `edge-01`, preparar tres lecturas CPU de valores 10, 20 y 30 en `2026-09-03T00:00:00Z`, `2026-09-03T00:05:00Z` y `2026-09-03T00:10:00Z`, respectivamente. Estas fechas quedan fuera del seed M01. Como fixture provisional, usar un umbral CPU de 15 a 25; su creación corresponde a la preparación de Jonathan/Alejandro.

| Categoría | Comprobación y resultado esperado |
| --- | --- |
| Normal | `/lecturas` con `limite=2` devuelve valores 30 y 20 en ese orden. El resumen del rango completo devuelve cantidad 3, promedio 20, mínimo 10 y máximo 30. Fuera de umbral devuelve 30 y 10, cada uno con umbrales 15 y 25. |
| Vacío | Consultar los tres GET en un rango preparado sin lecturas, por ejemplo `2026-09-04T00:00:00Z` a `2026-09-04T00:10:00Z`, devuelve 200 y `[]`. Mantener el equipo existente y el esquema M02 aplicado para comprobar realmente el caso vacío. |
| Límites | `limite=1` y `limite=1000` se aceptan; 0 y 1001 devuelven 422. `desde=hasta=2026-09-03T00:05:00Z` incluye la lectura 20. En el resumen completo, `limite=1` conserva cantidad 3. Preparar además valores exactamente 15 y 25 en otros instantes para comprobar que no aparecen como violaciones. |
| Fallo declarado | Solicitar `metrica=temperatura` devuelve 422 y `detail.codigo = "lectura_invalida"`. También comprobar fechas invertidas o sin zona, parámetro extra y una cadena de inyección como valor de `equipo`: se rechazan como parámetros inválidos. |

Como verificaciones adicionales, un equipo desconocido debe responder 422 `equipo_inexistente`, una base detenida 503 `base_no_disponible` y el modelo de umbrales ausente 503 `modelo_no_disponible` al consultar violaciones. Las pruebas deben aislar su preparación o restablecer sus fixtures para que los POST no choquen con duplicados al repetir la ejecución.

Alejandro prepara `tests/test_queries.py`, la actualización de `.gitignore` y `scripts/verify_base.sh`, los resultados de `artifacts/`, `evidence/m02-relational-model.json`, las dos ejecuciones consecutivas de `make verify`, CI y el tag `week-02-final`. Jonathan confirma el modelo y las migraciones, su repetibilidad y el ADR-002. Esta guía documenta el contrato e integración de las consultas de Marco.
