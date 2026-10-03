# Eventos NoSQL M04/M05 — contrato de implementación

Autor: Marco Antonio Osorio Hernandez.

`src/events_store.py` implementa el almacén experimental del ADR-004 y el
contrato documental del ADR-005. PostgreSQL conserva las lecturas, umbrales y
endpoints existentes. La API no hace escrituras dobles entre motores. Las
lecturas de DynamoDB siguen append-only (ADR-003): solo alta y consulta. El
cambio de estado y la baja se aplican a alertas.

El servicio `app` de Compose prepara la tabla DynamoDB antes de iniciar Uvicorn;
si no puede prepararla, termina con error. Para M05, el entorno que ejecuta ese
inicializador debe incluir también `db/nosql/indexes.json`, además de `src/`.
El servicio transitorio `dynamodb-init` asigna el directorio del volumen al
UID/GID 1000 de la imagen antes del arranque. DynamoDB continúa ejecutándose
sin root y el volumen conserva sus datos; no se montan ni persisten los JARs.

## Configuración

| Variable | Uso |
| --- | --- |
| `DYNAMODB_ENDPOINT` | URL obligatoria del servicio. En Compose, por defecto `http://dynamodb:8000`; desde el host usar el puerto publicado. |
| `DYNAMODB_TABLE` | Nombre de tabla; por defecto `cdrl_eventos`. Usar nombres independientes por prueba/benchmark. |
| `DYNAMODB_LOCAL` | `true` activa firma con credenciales efímeras generadas en memoria; solo admite los hosts `localhost`, `127.0.0.1`, `::1` y `dynamodb`. Predeterminado en Python: `false`; en Compose: `true`. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | Región obligatoria; se prioriza `AWS_REGION`. Compose usa `AWS_REGION`, con valor local por defecto `us-east-1`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | Solo para AWS con `DYNAMODB_LOCAL=false`; boto3 admite también su cadena habitual de proveedores. No guardar valores en Git. |

El modo local ignora credenciales AWS reales del entorno. DynamoDB Local acepta
firmas de laboratorio y usa `-sharedDb`, por lo que cambiar la firma no cambia la
base. No se incluye ninguna credencial fija en código o Compose. Para Learner
Lab, configurar explícitamente endpoint regional HTTPS y `DYNAMODB_LOCAL=false`.

## Tabla e índice

`db/nosql/indexes.json` declara la tabla con capacidad `PAY_PER_REQUEST`, PK/SK
de tipo String y un índice disperso `GSI1` (`GSI1PK`, `GSI1SK`, proyección `ALL`).
Las lecturas no se proyectan al GSI. El archivo también relaciona las consultas
Q1–Q4 con sus índices.

`ensure_table()` lee ese archivo desde la raíz del repositorio, relativa a
`src/events_store.py`; no depende del directorio desde el que se invoca Python.
Crea una tabla ausente a partir de las declaraciones y espera disponibilidad.
En una tabla existente compara `KeySchema`, `AttributeDefinitions` y todos los
`GlobalSecondaryIndexes`, incluidos su nombre, clave y proyección. Rechaza
índices adicionales, índices locales, tablas incompatibles e índices no activos;
no borra ni reemplaza datos. Los metadatos descriptivos `Sparse` y `Entities` no
se envían a la API de creación de DynamoDB.

| Entidad | PK | SK |
| --- | --- | --- |
| Lectura | `EQ#<equipo_codigo>#<metrica>` | `TS#<UTC>#<lectura_id>` |
| Alerta | `LECTURA#<lectura_id>` | `UMBRAL#<umbral_id>` |

| Índice de alertas | Valor |
| --- | --- |
| `GSI1PK` | `ALERTA#<estado>#<shard 0..3>` |
| `GSI1SK` | `SEV#<rango>#TS#<UTC>#LECTURA#<id>#UMBRAL#<id>` |

Las fechas se normalizan a UTC con seis decimales y `Z`; los IDs positivos de
64 bits se rellenan a 19 dígitos para preservar el orden numérico. El shard usa
SHA-256 de la identidad: es estable entre procesos. Severidad crítica tiene
rango 2 y advertencia 1, de modo que el orden descendente prioriza las críticas.

La identidad de alerta `(lectura_id, umbral_id)` está en la clave base, como
establece el ADR-005. Una condición
`attribute_not_exists(PK)` no impone unicidad sobre un atributo independiente:
si la clave base incluyera estado o severidad, cambiar esos campos permitiría
insertar otra alerta del mismo par. Por eso el acceso por estado y shard se
materializa en el GSI; la identidad añadida a su SK evita empates ambiguos.

## Validación documental antes de escribir

El almacén construye las claves y los campos derivados. El cliente no envía
`PK`, `SK`, `entidad`, `GSI1PK`, `GSI1SK` ni `shard`. La entrada de lectura usa
los modelos de `src/schemas.py` y se revalida incluso si ya es una instancia
Pydantic. No se confía en un modelo construido sin validación o modificado
después de crearlo.

| Entrada | Regla |
| --- | --- |
| Lectura | Son obligatorios `equipo_codigo`, `metrica`, `unidad`, `valor` y `medido_en`; cualquier campo extra se rechaza. |
| Identificadores | `lectura_id` y `umbral_id` son enteros positivos de 64 bits; no se admiten booleanos, cadenas ni números con parte decimal. |
| Equipo | Código de 3–63 caracteres conforme al patrón de `LecturaBase`. |
| Métrica/unidad | `cpu` y `memoria`: `porcentaje`; `disco_libre`: `bytes`; `latencia`: `milisegundos`; `tasa_error`: `proporcion`. |
| Valor | Número finito y representable en DynamoDB: CPU/memoria entre 0 y 100; tasa de error entre 0 y 1; disco/latencia mayores o iguales a cero. No se convierten cadenas ni booleanos a números. |
| Fechas | ISO 8601 con `T` y zona horaria, o `datetime` con zona. Se guardan en UTC con microsegundos y `Z`. |
| Alerta | Estado `abierta`, `reconocida` o `cerrada`; severidad `advertencia` o `critica`; instante e identificadores válidos. |
| Cierre | Al crear una alerta `cerrada` se exige `cerrada_en` con zona. Las alertas abiertas o reconocidas no admiten fecha de cierre. |
| `payload` | Cadena opcional y sintética de lectura, pasada como argumento nombrado a `build_lectura`/`put_lectura`; no es un campo adicional del diccionario de entrada. |

Un error de validación produce `DocumentoInvalido`, con el campo o la regla
incumplida, antes de llamar a la escritura. No se guarda parcialmente un
documento inválido. Los campos de alerta se pasan como argumentos nombrados:
un argumento obligatorio omitido o uno extra produce el `TypeError` de Python
que identifica ese argumento, también antes de cualquier escritura.

El límite por item es `MAX_ITEM_BYTES = 409600` (400 KiB). Incluye los nombres
y valores de atributos, también claves y campos derivados; no es el tamaño del
JSON. `estimate_item_size(item)` ofrece una estimación basada en UTF-8 y en la
representación numérica de DynamoDB. Permite preparar fixtures y rechazar
documentos que claramente exceden el límite, pero el motor arbitra la frontera
exacta. Un rechazo de DynamoDB por tamaño se traduce a
`DocumentoDemasiadoGrande`, subclase de `DocumentoInvalido`, sin escribir el
item. Otros errores `ValidationException` conservan su error original.

## Interfaz para las pruebas de Alejandro

```python
from src.events_store import EventsStore, ALERT_SHARDS, AlertaNoExiste

store = EventsStore()  # También admite table_name y resource boto3 inyectado.
store.ensure_table()
lectura = {
    "equipo_codigo": "fixture-m05-001", "metrica": "cpu",
    "unidad": "porcentaje", "valor": 42.5,
    "medido_en": "2026-10-03T00:00:00Z",
}
store.put_lectura(1, lectura)
guardada = store.get_lectura("fixture-m05-001", "cpu", lectura["medido_en"], 1)
assert guardada is not None and guardada["lectura_id"] == 1
page = store.query_lecturas("fixture-m05-001", "cpu", limite=100)
items = store.ultimas_lecturas("fixture-m05-001", limite=100)
store.put_alerta(lectura_id=1, umbral_id=1, estado="abierta",
                 severidad="advertencia", medido_en=lectura["medido_en"])
alerta = store.get_alerta(1, 1)  # Item o None; lectura fuerte por defecto.
pages = [store.query_alertas("abierta", shard) for shard in range(ALERT_SHARDS)]

reconocida = store.actualizar_estado_alerta(1, 1, "reconocida")
assert store.actualizar_estado_alerta(1, 1, "reconocida") == reconocida
cerrada = store.actualizar_estado_alerta(1, 1, "cerrada")
assert "cerrada_en" in cerrada  # UTC del cierre, generado por el almacén.
assert store.actualizar_estado_alerta(1, 1, "cerrada") == cerrada
assert store.delete_alerta(1, 1) is True
assert store.delete_alerta(1, 1) is False  # Ausencia: éxito sin cambios.
assert store.get_alerta(1, 1) is None
assert store.get_lectura("fixture-m05-001", "cpu", lectura["medido_en"], 2) is None
try:
    store.actualizar_estado_alerta(1, 1, "cerrada")
except AlertaNoExiste:
    pass
```

El ejemplo supone una tabla sintética aislada sin esas identidades. Repetir las
altas sobre la misma tabla produce el error de duplicado descrito abajo.

### Retornos y operaciones por identidad

| Método | Retorno y contrato |
| --- | --- |
| `build_lectura(lectura_id, lectura, *, payload=None)` | Documento validado, con claves y número `Decimal`; no escribe. |
| `put_lectura(lectura_id, lectura, *, payload=None)` | Respuesta de `PutItem`; alta condicional, no sobrescribe. |
| `get_lectura(equipo_codigo, metrica, medido_en, lectura_id, *, consistent_read=True)` | Item o `None`; usa la clave completa normalizada, sin consulta por id solo. |
| `build_alerta(*, lectura_id, umbral_id, estado, severidad, medido_en, cerrada_en=None)` | Documento validado; no escribe. Para estado cerrado exige fecha de cierre. |
| `put_alerta(*, lectura_id, umbral_id, estado, severidad, medido_en, cerrada_en=None)` | Respuesta de `PutItem`; alta única por par lectura/umbral. |
| `get_alerta(lectura_id, umbral_id, *, consistent_read=True)` | Item o `None`. En M05 devuelve el documento directamente, en lugar de la envoltura boto3 con `Item`. |
| `actualizar_estado_alerta(lectura_id, umbral_id, estado, *, cerrada_en=None)` | Item resultante; error si falta la alerta o si la transición no es válida. |
| `delete_alerta(lectura_id, umbral_id)` | `True` si eliminó un item; `False` si ya no existía, usando `ReturnValues=ALL_OLD`. |

Solo se permiten `abierta → reconocida`, `reconocida → cerrada` y
`abierta → cerrada`. Una cerrada no se reabre. Pedir el estado que ya tiene
devuelve el documento sin `UpdateItem`, y conserva la fecha de cierre. Al
cerrar, `cerrada_en` se genera en UTC si el consumidor no proporciona una fecha
con zona; en otros estados no se admite una fecha de cierre.

El cambio usa `UpdateItem` con
`attribute_exists(PK) AND estado = :estado_anterior`. En la misma escritura
atómica actualiza `estado` y `GSI1PK` y añade `cerrada_en` al cerrar. La identidad
base, la severidad y el instante de la lectura no cambian. Ante un fallo de esa
condición, relee por la tabla base con consistencia fuerte: devuelve éxito si
otro escritor ya alcanzó el estado solicitado; informa ausencia o transición
inválida cuando corresponde; reintenta si la transición aún es válida. Tras
tres conflictos condicionales consecutivos informa `ConflictoAlerta`.

### Consultas Q1–Q4

- **Q1:** `query_lecturas(equipo_codigo, metrica, desde=..., hasta=...,
  limite=..., exclusive_start_key=..., consistent_read=False)` retorna la
  respuesta de DynamoDB con `Items`, `ConsumedCapacity` y `LastEvaluatedKey`.
  Los extremos temporales son inclusivos. No usa `Scan` ni filtros posteriores.
  `ultimas_lecturas` sigue páginas aunque el motor corte a 1 MiB y mezcla las
  cinco métricas cuando se omite el filtro. El fan-out es secuencial en este
  adaptador: los objetos Resource de boto3 no se comparten entre hilos.
- **Q2:** `iter_lecturas(equipo_codigo, metrica, desde=..., hasta=...)` recorre
  toda la ventana. El consumidor calcula count/avg/min/max sobre esas filas,
  repitiendo para las cinco métricas. No existe agregación en el servidor.
- **Q3:** la consulta entrega las lecturas candidatas; el consumidor compara
  contra los umbrales vigentes de PostgreSQL. El prototipo no replica umbrales.
- **Q4:** `query_alertas(estado, shard, limite=..., exclusive_start_key=...)`
  usa `Query` sobre `GSI1` y retorna una página por shard; el consumidor pagina
  y mezcla los cuatro. `get_alerta` permite releer por identidad con consistencia
  fuerte antes de un cambio. El adaptador ofrece cambio de estado y baja, pero
  la reconciliación completa y los umbrales vigentes siguen en PostgreSQL.
  El GSI siempre es eventualmente consistente.

Q1–Q3 usan la clave de la tabla base (`TABLE` en `indexes.json`); Q4 usa
`GSI1`. Estas consultas no usan `Scan` ni `FilterExpression`.

### Errores e idempotencia

| Error | Significado |
| --- | --- |
| `DocumentoInvalido` (`ValueError`) | Campo faltante/extra, tipo, dominio, fecha o cierre incoherente; se rechaza antes de escribir. |
| `TypeError` | Argumento obligatorio omitido o adicional en la interfaz de alertas; se rechaza antes de escribir. |
| `DocumentoDemasiadoGrande` (`DocumentoInvalido`) | Se supera el límite de tamaño del item. |
| `AlertaNoExiste` (`LookupError`) | Se pidió actualizar una alerta ausente; no se crea una nueva. |
| `TransicionAlertaInvalida` (`ValueError`) | Cambio hacia un estado no permitido desde el estado actual. |
| `ConflictoAlerta` (`RuntimeError`) | Se agotaron tres intentos de actualización condicional por cambios concurrentes. |
| `ClientError` con `ConditionalCheckFailedException` | Alta duplicada; el documento original queda intacto. |

`put_lectura` y `put_alerta` son condicionales. Un duplicado conserva el dato
original y propaga `ClientError` con código `ConditionalCheckFailedException`.
Pedir el mismo estado y borrar una alerta ausente son éxitos idempotentes.
Las altas son idempotentes en su efecto, pero el segundo intento informa el
duplicado. Los getters ausentes retornan `None`.

Los errores de conexión, throttling y otros errores del motor se propagan;
no se convierten en listas vacías ni en escrituras exitosas. Los timeouts de
conexión/lectura son 2/3 segundos y el SDK hace como máximo dos intentos por petición. El
inicializador CLI reintenta conexiones durante una ventana de 30 segundos,
además del tiempo acotado de la petición en curso y de creación de tabla.

`store.table` y `store.resource` están disponibles para medir
capacidad y para lotes de fixtures.

## Carga sintética reproducible

Desde la raíz del repositorio, después de `make setup`, con las credenciales de
PostgreSQL de desarrollo ya configuradas para que Compose pueda validarse:

```bash
docker compose up -d dynamodb
export DYNAMODB_ENDPOINT="http://localhost:${DYNAMODB_PORT:-8000}"
export DYNAMODB_LOCAL=true
export AWS_REGION=us-east-1
export DYNAMODB_TABLE=cdrl_eventos
.venv/bin/python -m src.events_store
.venv/bin/python -m scripts.load_events --equipos 3 --por-metrica 100
```

En PowerShell, usar `$env:VARIABLE = "valor"` y `.venv\Scripts\python.exe`.
Las variables deben estar exportadas al proceso Python; el cargador no lee
automáticamente `.env`. Para iniciar la API por Compose, usar el endpoint de
red interna (`http://dynamodb:8000`) o quitar la variable de host antes de
`make run`: `localhost` dentro del contenedor apunta al propio contenedor.

La carga predeterminada genera 1.500 lecturas (3 equipos × 5 métricas × 100)
y alertas sintéticas para CPU > 60, críticas si CPU > 85. Esa regla sirve al
fixture; no modifica ni sustituye los umbrales relacionales. IDs y datos son
deterministas. Repetir los mismos parámetros rechaza duplicados sin alterarlos.
`--prefijo` y `--inicio` permiten escenarios independientes. El script emite un
JSON con conteos y duraciones a stdout; cualquier error distinto de duplicado
termina con código no cero.

Para H3, en una **tabla vacía dedicada sin escritores concurrentes**:

```bash
export DYNAMODB_TABLE=cdrl_eventos_benchmark_m04
.venv/bin/python -m scripts.load_events --equipos 1 --por-metrica 1000 --batch
```

`--batch` usa lotes de 25 mediante `batch_writer`, incluyendo reenvío de
`UnprocessedItems`, y rechaza una tabla que ya contenga datos. BatchWriteItem no
admite condiciones, por lo que este modo es solo para carga/medición sintética;
no debe utilizarse como escritura append-only de negocio. `lecturas_seconds`
mide validación más carga de lecturas y excluye la carga posterior de alertas.
Una falla puede dejar carga parcial, que el modo condicional permite completar.

## Integración M05

La entrega de Marco comprende `src/events_store.py` y esta guía. P1 declara
el contrato y los índices en `docs/ADR-005-almacen-documental.md` y
`db/nosql/indexes.json`. P3 aporta `tests/test_m05_document_store.py`, el
resultado `artifacts/m05-document-store.json`, su copia en
`evidence/m05-document-store.json` y la exigencia en `verify_base.sh`.

Las pruebas del equipo deben usar fixtures sintéticos y una tabla aislada,
exportando endpoint local, modo local y región en el proceso de pytest; el
entorno del contenedor app no se hereda al runner. La frontera de 409.600 bytes
debe verificarse contra DynamoDB, incluyendo nombres y valores. La prueba de
Q1–Q4 debe contrastar las operaciones con `queries` en `indexes.json`.

La imagen de ejecución necesita copiar `db/nosql/indexes.json` en la misma
raíz que `src/`. La preparación de esa imagen, las pruebas de entrega, la
evidencia y el tag `week-05-final` corresponden a la integración del equipo.
Esta guía no declara esos resultados como aprobados. El cierre debe comprobar
`make setup && make verify && make run` con los archivos de todos los
integrantes.

## Referencias técnicas

- [PutItem condicional](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_PutItem.html)
- [UpdateItem condicional](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_UpdateItem.html)
- [DeleteItem y ReturnValues](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DeleteItem.html)
- [Consistencia y GSI](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html)
- [Límite de tamaño](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Constraints.html)
- [Cálculo del tamaño de atributos](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/CapacityUnitCalculations.html)
- [DynamoDB Local](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.DownloadingAndRunning.html)
