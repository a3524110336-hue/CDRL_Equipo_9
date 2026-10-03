# ADR-005 — Almacén documental de eventos: forma, validación, índices e idempotencia

## Estado

Propuesto. Pasa a **Aceptado** cuando `evidence/m05-document-store.json`
registre en verde las seis pruebas de la sección *Evidencia* y la prueba de que
Q1–Q4 nunca usan `Scan`. Si alguna falla, se corrige el diseño aquí antes que el
código.

Corrige una parte de ADR-004 (la clave de las alertas, ver *Corrección a
ADR-004*); el resto de ADR-004 sigue vigente.

## Contexto

M04 eligió DynamoDB con tabla única (`cdrl_eventos`) y dejó funcionando
`src/events_store.py`: alta condicional de lecturas y alertas, consultas Q1/Q3
por partición y un índice `GSI1` para las alertas. Cada item de DynamoDB ya es un
documento JSON, así que M05 no cambia de motor: pide convertir ese almacén en un
**almacén documental con contrato**.

Lo que M04 dejó abierto, y este ADR cierra:

- **Forma del documento.** El código escribe documentos, pero en ningún lugar
  está escrito cuáles son sus campos, cuáles son obligatorios y cuáles sobran.
- **Validación.** Las lecturas se validan con el contrato relacional
  (`src/schemas.py`); las alertas solo comprueban estado y severidad. No hay una
  regla única que diga cuándo un documento se rechaza.
- **Índices declarados.** `ensure_table()` tiene la forma de la tabla escrita a
  mano en el código. No existe un archivo que diga qué índices hay y qué consulta
  atiende cada uno, así que no se puede probar que una consulta va por un índice
  *declarado*.
- **CRUD incompleto.** No hay `get` de lectura, ni cambio de estado ni baja de
  alertas, y por lo tanto nada que decir todavía sobre su idempotencia.

## Decisión

### 1. Dos tipos de documento en la misma tabla

Un atributo `entidad` distingue el tipo y decide qué validador se aplica.
Ningún documento admite campos fuera de esta lista (*extra = prohibido*, igual
que `LecturaBase` en `src/schemas.py`).

**Lectura** — un hecho medido; append-only (ADR-003).

| Campo | Tipo | Regla |
| --- | --- | --- |
| `PK` | S | `EQ#<equipo_codigo>#<metrica>` — la escribe el almacén, no el cliente |
| `SK` | S | `TS#<medido_en UTC>#<lectura_id a 19 dígitos>` — ídem |
| `entidad` | S | Exactamente `"lectura"` |
| `lectura_id` | N | Entero positivo de 64 bits |
| `equipo_codigo` | S | 3–63 caracteres, `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$` |
| `metrica` | S | Una de las cinco: `cpu`, `memoria`, `disco_libre`, `latencia`, `tasa_error` |
| `unidad` | S | La única válida para su métrica (`porcentaje`, `bytes`, `milisegundos`, `proporcion`) |
| `valor` | N | Finito y dentro del rango de su métrica (`cpu`/`memoria` 0–100, `tasa_error` 0–1, el resto ≥ 0) |
| `medido_en` | S | ISO 8601 **con zona horaria**; se guarda normalizado a UTC con microsegundos y `Z` |
| `payload` | S | Opcional. Solo fixtures sintéticos para medir el límite de tamaño |

**Alerta** — una lectura que viola un umbral; su estado sí cambia.

| Campo | Tipo | Regla |
| --- | --- | --- |
| `PK` | S | `LECTURA#<lectura_id a 19 dígitos>` |
| `SK` | S | `UMBRAL#<umbral_id a 19 dígitos>` |
| `GSI1PK` | S | `ALERTA#<estado>#<shard 0..3>` |
| `GSI1SK` | S | `SEV#<rango>#TS#<medido_en UTC>#LECTURA#<id>#UMBRAL#<id>` |
| `entidad` | S | Exactamente `"alerta"` |
| `lectura_id`, `umbral_id` | N | Enteros positivos de 64 bits |
| `estado` | S | `abierta`, `reconocida` o `cerrada` (mismo dominio que `alertas_estado_valido`) |
| `severidad` | S | `advertencia` (rango 1) o `critica` (rango 2) |
| `medido_en` | S | Instante de la lectura, UTC normalizado |
| `shard` | N | `SHA-256(identidad) mod 4`; estable entre procesos |
| `cerrada_en` | S | UTC normalizado. **Presente si y solo si** `estado = cerrada` |

`cerrada_en` replica `alertas_cierre_coherente` de PostgreSQL: un documento
cerrado sin fecha de cierre, o abierto con ella, se rechaza.

La alerta **no** copia `valor` ni `tipo`. El valor ya está en el documento de la
lectura (a un `GetItem` de distancia por su clave) y el tipo se deriva del umbral
vigente, que sigue viviendo en PostgreSQL. Copiarlos abriría la puerta a que la
copia y el original discrepen.

### 2. Reglas de validación: se valida antes de escribir, y si falla no se escribe nada

Toda escritura pasa por el validador de su `entidad` **antes** de llamar a
DynamoDB. Un documento inválido produce un error que nombra el campo y la regla
rota, y la tabla queda exactamente como estaba.

| Caso | Ejemplo | Resultado |
| --- | --- | --- |
| Falta un campo obligatorio | lectura sin `unidad` | Rechazo, nada escrito |
| Sobra un campo | lectura con `"origen": "x"` | Rechazo — el documento no crece sin ADR |
| Tipo incorrecto | `valor: "42"`, `lectura_id: 1.0`, `true` como número | Rechazo, sin conversión implícita |
| Métrica fuera de las cinco | `metrica: "temperatura"` | Rechazo — ampliar el vocabulario es una migración, no un dato |
| Unidad que no corresponde | `cpu` en `milisegundos` | Rechazo |
| Valor fuera de rango o no finito | `cpu: 140`, `NaN`, `Infinity` | Rechazo |
| Fecha sin zona | `2026-10-03T10:00:00` | Rechazo — un instante sin zona es ambiguo |
| Estado o severidad fuera del dominio | `estado: "pendiente"` | Rechazo |
| Cierre incoherente | `cerrada` sin `cerrada_en` | Rechazo |
| Documento sobre el límite | item > 400 KB | Rechazo, nada escrito |

**El límite de tamaño lo arbitra el motor.** DynamoDB acepta items de hasta
**400 KB (409.600 bytes)** y los mide sumando los **nombres** de los atributos y
sus valores (UTF-8 en cadenas; los números ocupan según sus dígitos
significativos), no el tamaño del JSON enviado. El validador estima ese tamaño
con la misma regla para rechazar pronto, pero quien decide en la frontera exacta
es DynamoDB: si el motor responde `ValidationException`, el almacén lo traduce al
mismo error de "documento sobre el límite". Por eso el caso límite se prueba
contra el motor y no contra una estimación: un documento de exactamente 409.600
bytes entra y uno de 409.601 no.

### 3. Qué índice usa cada consulta

Solo existen dos índices, y los dos están declarados en `db/nosql/indexes.json`:
la **tabla base** (`PK`/`SK`) y **`GSI1`** (`GSI1PK`/`GSI1SK`, proyección
`ALL`, disperso: solo lo tienen las alertas). Ninguna consulta usa `Scan` ni
`FilterExpression`.

| # | Consulta | Índice | Operación DynamoDB | Llamadas |
| --- | --- | --- | --- | --- |
| Q1 | `ULTIMAS_LECTURAS` — últimas N de un equipo | Tabla base | `Query PK = EQ#e#m`, `SK BETWEEN` la ventana, `ScanIndexForward=false`, `Limit=N` | 1 por métrica; 5 si no se filtra métrica |
| Q2 | `RESUMEN_POR_METRICA` — count/avg/min/max por ventana | Tabla base | `Query PK = EQ#e#m`, `SK BETWEEN desde y hasta`, paginando | 5 (una por métrica); agrega el cliente |
| Q3 | `LECTURAS_FUERA_DE_UMBRAL` | Tabla base | La misma `Query` de Q1; la comparación contra el umbral la hace el cliente | Igual que Q1 |
| Q4 | `ALERTAS_POR_ESTADO` — lado de lectura de la reconciliación | `GSI1` | `Query GSI1PK = ALERTA#<estado>#<shard>`, `ScanIndexForward=false` (críticas primero) | 4 (una por shard); mezcla el cliente |

Tres decisiones dentro de esta tabla:

- **Q3 compara en el cliente, no con `FilterExpression`.** Un filtro en el
  servidor no ahorra unidades de lectura —DynamoDB cobra lo leído, no lo
  devuelto— y el cliente ya tiene los umbrales vigentes, porque siguen en
  PostgreSQL como fuente de verdad (ADR-004, *Consecuencias*). La comparación
  es la misma de `docs/API-lecturas.md`: un valor igual a un extremo está
  **dentro** del umbral.
- **Q4 lee del GSI, pero decide sobre la tabla base.** El GSI es siempre
  eventualmente consistente: justo después de un cambio de estado puede seguir
  mostrando la alerta en su partición anterior. Por eso el GSI solo sirve para
  *encontrar* alertas; antes de cambiar una, se relee con `GetItem` fuerte por
  su clave base.
- **No hay búsqueda de lectura por `lectura_id` solo.** La clave de la lectura
  necesita equipo, métrica e instante. Ninguna de Q1–Q4 busca una lectura suelta
  por id, y añadir un índice para eso sería pagar escritura extra en cada lectura
  para una consulta que nadie hace. `get_lectura` recibe la clave completa.

### 4. CRUD y qué operación es idempotente

| Operación | Índice | Mecanismo | ¿Idempotente? | Al repetirla |
| --- | --- | --- | --- | --- |
| `put_lectura` | Tabla base | `PutItem` con `attribute_not_exists(PK)` | Sí en efecto | El segundo intento falla con `ConditionalCheckFailedException` y el original queda intacto |
| `get_lectura` | Tabla base | `GetItem` por `PK`+`SK` | Sí (solo lee) | Si no existe devuelve `None`, no un error |
| `put_alerta` | Tabla base | `PutItem` con `attribute_not_exists(PK)` | Sí en efecto | Igual que `put_lectura`: no hay segunda alerta para el mismo par |
| `get_alerta` | Tabla base | `GetItem` fuerte | Sí (solo lee) | Si no existe devuelve `None` |
| `actualizar_estado_alerta` | Tabla base | `UpdateItem` condicional | **Sí** | Pedir el estado que ya tiene no cambia nada y no es error |
| `delete_alerta` | Tabla base | `DeleteItem` | **Sí** | Borrar algo que ya no existe termina bien e informa que no existía |
| update / delete de lectura | — | **No existen** | — | Una lectura se corrige emitiendo otra (ADR-003) |

Por qué cada una es como es:

- **Las altas fallan ruidosamente en vez de "no hacer nada" en silencio.** El
  estado final es el mismo (idempotencia en efecto), pero quien llama necesita
  distinguir "lo escribí" de "ya existía": un cargador que reprocesa un lote
  cuenta duplicados, y un duplicado inesperado puede delatar un id reciclado.
  Es lo mismo que hace `ON CONFLICT DO NOTHING` en el DML de M02, solo que aquí
  el aviso llega como excepción y no como `INSERT 0 0`.
- **El cambio de estado sigue un ciclo de vida de una sola dirección:**

  ```
  abierta ──► reconocida ──► cerrada
     └─────────────────────────▲
  ```

  `abierta → reconocida`, `reconocida → cerrada` y `abierta → cerrada` son
  válidas; `cerrada` es terminal, igual que en el DML de M02, donde las cerradas
  "son historial" y no se reevalúan. El `UpdateItem` lleva
  `ConditionExpression = attribute_exists(PK) AND estado = :estado_anterior`, y
  en la misma escritura atómica reescribe `estado`, `GSI1PK` (la alerta se muda
  de partición en el índice) y, al cerrar, `cerrada_en`. Si la condición falla
  hay tres casos y se distinguen releyendo con `GetItem` fuerte:
  1. la alerta **ya está** en el estado pedido → éxito sin cambios (idempotente);
  2. **no existe** → error de ausencia;
  3. está en un estado desde el que la transición no es válida (por ejemplo,
     reabrir una cerrada) → error de transición.
- **La baja es idempotente por naturaleza.** `DeleteItem` sobre una clave que no
  existe no falla en DynamoDB. Se pide `ReturnValues=ALL_OLD` para devolver si
  había algo que borrar. Es el equivalente del paso 1 del DML de M02: la lectura
  dejó de violar el umbral porque alguien lo ensanchó.

### 5. Corrección a ADR-004 (precisión de Marco)

ADR-004 proponía para las alertas la clave base
`PK = ALERTA#<estado>#<shard>`, `SK = SEV#<severidad>#TS#<medido_en>`, con la
identidad `(lectura, umbral)` como atributo aparte. **Eso no impone unicidad.**
`attribute_not_exists(PK)` solo mira la clave del item que se está escribiendo:
si la clave contiene el estado o la severidad, cambiar cualquiera de los dos
permitiría escribir una segunda alerta del mismo par `(lectura, umbral)` en otra
clave, y la condición nunca la vería.

Como lo señaló Marco en `docs/EVENTS-STORE.md`, **la identidad va en la clave
base** (`LECTURA#…` / `UMBRAL#…`) y el acceso por estado, shard y severidad se
materializa en `GSI1`. Así la invariante `UNIQUE (lectura_id, umbral_id)` de
PostgreSQL vuelve a tener un equivalente que el motor sí impone, y el cambio de
estado mueve la alerta dentro del índice sin tocar su identidad. La tabla
*Alertas en la misma tabla* de ADR-004 queda sustituida por la sección 1 de este
ADR.

### 6. Los índices se declaran en un archivo, no en el código

`db/nosql/indexes.json` es la **fuente de verdad** de la forma de la tabla: el
esquema de clave, las definiciones de atributos, `GSI1`, el formato de cada
clave y qué consulta atiende cada índice. Usa los mismos nombres de campo que
`describe_table` de boto3 (`KeySchema`, `AttributeDefinitions`,
`GlobalSecondaryIndexes`) para que `ensure_table()` lo compare tal cual contra la
tabla real y la cree a partir de él si falta.

La regla que esto habilita: **una consulta que no aparece en `indexes.json` no
existe.** Añadir una consulta nueva obliga a declarar qué índice usa, y si
necesita un índice nuevo, a declararlo aquí y en un ADR antes de escribir código.

## Alternativas descartadas

**Validar solo con el esquema de DynamoDB.** DynamoDB no tiene esquema más allá
de la clave: aceptaría una lectura con `metrica: "temperatura"` o sin `unidad`
sin protestar. La validación tiene que vivir en el almacén.

**JSON Schema como validador en vez de `src/schemas.py`.** Habría dado un
contrato portátil, pero duplicaría las reglas que ya están en los modelos
Pydantic de M01 y que la API usa hoy. Dos fuentes para la misma regla terminan
discrepando; se reutilizan los modelos existentes y este ADR las enumera.

**Un GSI por `lectura_id`.** Permitiría `get_lectura(id)` sin conocer equipo ni
métrica, a costa de una escritura extra en el índice por cada lectura, la ruta
más caliente del sistema. Ninguna de Q1–Q4 lo necesita. Se reabre si aparece un
endpoint que busque lecturas por id.

**Cambio de estado como borrar y volver a crear.** Con la identidad en la clave
base no hace falta: `UpdateItem` cambia el estado y mueve la alerta en `GSI1` en
una sola escritura atómica. Borrar y recrear dejaría una ventana en la que la
alerta no existe y abriría la puerta a perderla si el proceso cae a medias.

**Permitir reabrir alertas cerradas.** El DML de M02 trata las cerradas como
historial; reabrir aquí rompería la equivalencia entre los dos motores. Si una
lectura vuelve a violar el umbral, eso es una lectura nueva y, por lo tanto, una
alerta nueva.

## Evidencia

La produce P3 en `tests/test_m05_document_store.py`, con salida en
`artifacts/m05-document-store.json` y `evidence/m05-document-store.json`:

| Prueba | Qué demuestra de este ADR |
| --- | --- |
| Normal: alta, lectura, cambio de estado, baja | Sección 4 de punta a punta |
| Límite 1: documento de exactamente 409.600 bytes entra; uno más, no | Sección 2, frontera de tamaño arbitrada por el motor |
| Límite 2: repetir la misma operación | Sección 4, columna *Al repetirla* |
| Duplicado: el segundo `put` no sobrescribe | Alta condicional y unicidad del par |
| Ausencia: `get`/`update`/`delete` de algo que no existe | `None`, error de ausencia y baja idempotente |
| Fallo declarado: documento inválido rechazado sin escribir nada | Sección 2 |
| Q1–Q4 van por `Query` a un índice declarado, nunca `Scan` | Secciones 3 y 6 |

## Consecuencias

- `ensure_table()` deja de tener la forma de la tabla escrita en el código: la
  lee de `db/nosql/indexes.json` y rechaza una tabla que no coincida.
- Cada escritura paga una validación en el cliente antes de llegar al motor. Es
  barata (los modelos ya existen) y es lo que permite prometer "si falla, no se
  escribe nada".
- Cambiar de estado una alerta cuesta escritura en la tabla **y** en `GSI1`,
  porque su clave de índice cambia. Es aceptable: las alertas son pocas frente a
  las lecturas y cambian de estado a lo sumo dos veces.
- El almacén sigue siendo experimental, como en M04: la API y la
  reconciliación completa siguen sobre PostgreSQL. M05 no introduce escrituras
  dobles entre motores.
- Los umbrales no se replican en DynamoDB. Q3 y la reevaluación de alertas
  necesitan PostgreSQL; si algún día el almacén documental tuviera que funcionar
  solo, ese es el primer hueco que habría que cerrar.
