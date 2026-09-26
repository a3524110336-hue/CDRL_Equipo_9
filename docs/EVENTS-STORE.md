# Eventos NoSQL M04 — contrato de implementación

Autor: Marco Antonio Osorio Hernandez.

`src/events_store.py` implementa el almacén experimental del ADR-004. PostgreSQL
conserva las lecturas, umbrales y endpoints existentes. La API no hace escrituras
dobles entre motores. El servicio `app` de Compose espera la tabla DynamoDB y su
índice antes de iniciar Uvicorn; si no puede prepararlos, termina con error.
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

Tabla con capacidad `PAY_PER_REQUEST`, PK/SK de tipo String y un índice disperso
`GSI1` (`GSI1PK`, `GSI1SK`, proyección `ALL`). Las lecturas no se proyectan al GSI.
`ensure_table()` crea la tabla si falta, espera disponibilidad y rechaza tablas
existentes incompatibles; no borra ni reemplaza datos.

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

**Precisión necesaria respecto del ADR propuesto:** la identidad de alerta
`(lectura_id, umbral_id)` debe estar en la clave base. Una condición
`attribute_not_exists(PK)` no impone unicidad sobre un atributo independiente:
si la clave base incluyera estado o severidad, cambiar esos campos permitiría
insertar otra alerta del mismo par. Por eso el acceso por estado y shard se
materializa en el GSI; la identidad añadida a su SK evita empates ambiguos.
Jona debe incorporar esta precisión al cerrar el ADR.

## Interfaz para las pruebas de Alejandro

```python
from src.events_store import EventsStore, ALERT_SHARDS

store = EventsStore()  # También admite table_name y resource boto3 inyectado.
store.ensure_table()
lectura = {
    "equipo_codigo": "fixture-m04-001", "metrica": "cpu",
    "unidad": "porcentaje", "valor": 42.5,
    "medido_en": "2026-09-01T00:00:00Z",
}
store.put_lectura(1, lectura)
page = store.query_lecturas("fixture-m04-001", "cpu", limite=100)
items = store.ultimas_lecturas("fixture-m04-001", limite=100)
store.put_alerta(lectura_id=1, umbral_id=1, estado="abierta",
                 severidad="advertencia", medido_en=lectura["medido_en"])
alerta = store.get_alerta(1, 1)  # ConsistentRead=True en la tabla base.
pages = [store.query_alertas("abierta", shard) for shard in range(ALERT_SHARDS)]
```

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
- **Q4:** `put_alerta` impone alta única por par; `get_alerta` permite lectura
  fuerte. Cierre/reapertura y reconciliación completa siguen en el DML SQL de
  M02; este adaptador no implementa un reconciliador NoSQL. `query_alertas`
  retorna una página por shard; el consumidor pagina y mezcla los cuatro.
  El GSI siempre es eventualmente consistente.

`put_lectura` y `put_alerta` son condicionales. Un duplicado conserva el dato
original y propaga `ClientError` con código `ConditionalCheckFailedException`.
Los errores de conexión, throttling y tamaño se propagan; no se convierten en
listas vacías ni en escrituras exitosas. Los timeouts de conexión/lectura son
2/3 segundos y el SDK hace como máximo dos intentos por petición. El
inicializador CLI reintenta conexiones durante una ventana de 30 segundos,
además del tiempo acotado de la petición en curso y de creación de tabla.

`build_lectura(..., payload="...")` y `put_lectura(..., payload="...")`
permiten un atributo sintético para probar tamaño. El límite real de 400 KiB
incluye nombres y valores de atributos; no equivale al tamaño del JSON enviado.
Dejar que el motor valide el límite y comprobar `ValidationException` al
rebasarlo. `store.table` y `store.resource` están disponibles para medir
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

## Integración pendiente de otros integrantes

Ale debe añadir sus pruebas, mediciones `artifacts/nosql-matrix.json`, evidencia
`evidence/m04-nosql-decision.json` y su exigencia en `verify_base.sh`. Debe
exportar endpoint local, modo local, región y una tabla aislada en el proceso de
pytest, porque el entorno del contenedor app no se hereda al runner de pruebas.
El Compose sí prepara la tabla al arrancar app. No atribuir a DynamoDB Local
pruebas de capacidad, particiones físicas o costo del servicio AWS: el límite
de partición caliente real y las hipótesis del ADR requieren interpretación de
las mediciones por Jona y Ale.

Esta entrega no añade el tag final, no cierra el ADR ni presenta resultados de
la matriz. El contrato `make setup && make verify && make run` se conserva.

## Referencias técnicas

- [PutItem condicional](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_PutItem.html)
- [Consistencia y GSI](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html)
- [Límite de tamaño](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Constraints.html)
- [DynamoDB Local](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.DownloadingAndRunning.html)
