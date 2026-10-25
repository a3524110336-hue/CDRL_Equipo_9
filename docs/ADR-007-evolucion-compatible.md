# ADR-007 — Evolución compatible de documentos: migración v1 → v2 repetible y reversible

## Estado
Aceptado. Reemplaza la regla «no hay migración masiva» de ADR-006 §1; el resto
de ADR-006 (auditoría y recuperación) sigue vigente. Define el contrato de M07
para `src/events_store.py` y `scripts/migrate_m07.py` (Marco Antonio Osorio
Hernandez) y para las pruebas y la evidencia (Persona 3). Los nombres de campos,
tablas, operaciones y medidas de este ADR son los que usa el código; si el
código necesita otro, se cambia aquí primero.

## Contexto

M07 pide implementar versionado y migración compatible de documentos **sin
romper consumidores existentes**, con cuatro criterios:

1. la versión anterior sigue siendo legible durante la transición;
2. la migración es repetible;
3. el rollback (o una estrategia equivalente) está probado;
4. el ADR establece compatibilidad y límites.

Lo que ya hay y condiciona la decisión:

- Desde M06 conviven dos versiones de lectura en `cdrl_eventos`: **v1** (sin
  `schemaVersion`, documentos de M05) y **v2** (`schemaVersion: 2`, `fuente`,
  `registrado_en`). Todo lo que sale de `EventsStore` pasa por
  `normalizar_lectura`, que sube v1 a v2 **al leer** sin tocar la tabla.
- ADR-006 descartó la migración masiva y dejó escrito cuándo reabrirla. M07 la
  pide de forma explícita: una migración repetible y con rollback probado no se
  puede demostrar sin migrar nada.
- Hay consumidores que **no** pasan por `normalizar_lectura`: el respaldo JSONL
  de `recover_m06.py` copia items crudos, y cualquier exportación o análisis
  que lea la tabla directamente ve documentos de dos formas distintas. Mientras
  existan v1, cada uno de ellos tiene que conocer las dos versiones.
- Las lecturas son append-only (ADR-005 §4). Migrar en sitio es la primera
  escritura que modifica una lectura existente, así que se acota aquí como
  excepción controlada.
- DynamoDB Local no tiene PITR ni respaldos bajo demanda y no aplica IAM.

## Decisión

### 1. Estrategia: expand/contract en tres fases

| Fase | Qué cambia | Estado en M07 |
| --- | --- | --- |
| **Expand** | El código lee v1, v2 y v2 migrada (`normalizar_lectura` acepta `fuente: "migrada"`). Escribe siempre v2 | Se implementa |
| **Migrate** | `migrate_m07.py` convierte las v1 a v2 en sitio, por lotes, guardando una copia exacta de cada original | Se implementa y se prueba, con rollback |
| **Contract** | Se quita la rama v1 de `normalizar_lectura` | **No se hace en M07** (ver §6) |

**Orden de despliegue:** primero el código que lee `fuente: "migrada"`, después
la migración. Si la migración corriera antes, el lector de M06 rechazaría las
lecturas migradas con `DocumentoInvalido`. En este repo los dos llegan en el
mismo merge, pero la regla queda escrita para cuando haya más de un proceso.

### 2. Qué hace la migración `m07-v1-a-v2`

Identificador fijo: **`migracion_id = "m07-v1-a-v2"`**.

A cada lectura v1 se le **agregan** cuatro campos; ninguno existente cambia:

| Campo | Valor |
| --- | --- |
| `schemaVersion` | `2` (número) |
| `fuente` | `"migrada"` |
| `registrado_en` | instante UTC en que la migración escribió el item |
| `migracion_id` | `"m07-v1-a-v2"` |

- `PK`, `SK`, `entidad`, `lectura_id`, `equipo_codigo`, `metrica`, `unidad`,
  `valor`, `medido_en` y `payload` quedan **idénticos**.
- En una lectura migrada, `registrado_en` **no** es la llegada original del dato
  (esa no se conoce): es cuándo se escribió esta versión. `fuente: "migrada"` es
  lo que lo distingue.
- Las alertas no se migran: no tienen versión (ADR-006 §1).

**Cómo se recorre la tabla.** La migración es la única operación que usa
`Scan`, y lo hace como proceso por lotes fuera de línea, nunca en una ruta de
la API:

- `Scan` paginado con `FilterExpression: entidad = lectura`, `Limit = lote`
  (por omisión 25) y `LastEvaluatedKey` como punto de reanudación.
- Cada item se clasifica: sin `schemaVersion` → se migra; `schemaVersion == 2`
  → `ya_v2`, no se toca; cualquier otro valor → `version_desconocida`, se reporta
  con `VersionDesconocida` y **no se toca**.
- Q1-Q4 siguen sin usar `Scan` (ADR-005).

**Cada documento se migra en una transacción** (`TransactWriteItems`) con dos
escrituras que se confirman juntas o no se confirma ninguna:

1. `Put` en `cdrl_migracion_respaldo` de la copia exacta del original, con
   `attribute_not_exists(PK)`;
2. `Update` de la lectura que agrega los cuatro campos, con la condición
   `attribute_not_exists(schemaVersion)`.

Consecuencias de hacerlo así:

- **Repetible.** Una segunda corrida encuentra todo con `schemaVersion` y
  migra 0; si dos procesos corren a la vez, la condición hace que solo uno gane
  en cada documento (el otro cuenta `ya_v2`).
- **Reanudable.** Si la corrida se cae a la mitad de un lote, cada documento
  quedó migrado **con** su respaldo, o intacto **sin** respaldo; nunca a medias.
  Volver a correr termina lo que falta sin duplicar nada.
- **Sin pérdida para el rollback.** No puede existir una lectura migrada sin su
  copia original.

### 3. Tabla de respaldo: `cdrl_migracion_respaldo`

Declarada en **`db/nosql/migration-backup-table.json`** (variable
`DYNAMODB_MIGRATION_TABLE`), igual que `indexes.json` y `audit-table.json`.
`ensure_table` la crea y la compara contra ese archivo.

| Clave / campo | Valor |
| --- | --- |
| `PK` | `MIG#<migracion_id>` → `MIG#m07-v1-a-v2` |
| `SK` | `<PK de la lectura>|<SK de la lectura>` |
| `migracion_id` | `"m07-v1-a-v2"` |
| `respaldado_en` | instante UTC |
| `documento` | el item original **completo y tal cual** (mapa) |

Con esta clave, el rollback lee todos los respaldos de una migración con un
`Query PK = MIG#m07-v1-a-v2`, sin `Scan`. Va en una tabla aparte por lo mismo
que la auditoría (ADR-006 §2): permisos por tabla en AWS y ninguna operación
sobre eventos arrastra los respaldos.

El respaldo sí lleva `valor` y `payload`, porque es una copia del dato, no un
registro de auditoría. Por eso **no** se escribe en `cdrl_auditoria` ni se
exporta en el artifact.

### 4. Rollback: `revertir_migracion("m07-v1-a-v2")`

Por cada respaldo (`Query` a `MIG#m07-v1-a-v2`), una transacción:

1. `Put` de `documento` sobre la lectura, con la condición
   `migracion_id = :id`. Solo se revierte lo que esta migración tocó; una lectura
   que no lleva ese `migracion_id` no se pisa.
2. `Delete` del respaldo, con `attribute_exists(PK)`.

- El resultado es la v1 **idéntica** al original: sin `schemaVersion`, sin
  `fuente`, sin `registrado_en` y sin `migracion_id`. Se verifica campo por campo
  contra la copia.
- **Repetible:** una vez revertido, el respaldo ya no existe; una segunda
  corrida revierte 0.
- Las lecturas que ya eran v2 antes de la migración no tienen respaldo y no se
  tocan.
- Si la condición falla (la lectura se borró o cambió después de migrar), se
  cuenta como `no_revertible`, **se conserva el respaldo** y el caso se reporta.
  Nunca se crea una lectura que no existía.

### 5. Lectura durante la transición y auditoría

**`normalizar_lectura`** acepta tres formas y devuelve siempre el mismo formato:

- sin `schemaVersion` → v1: `schemaVersion: 1`, `fuente: "desconocida"`,
  `registrado_en: None` (igual que en M06);
- `schemaVersion == 2` con `fuente` en `api`, `lote` o `fixture` → v2 nativa;
- `schemaVersion == 2` con `fuente: "migrada"` → exige
  `migracion_id == "m07-v1-a-v2"`; si falta o es otro, `DocumentoInvalido`;
- cualquier otra versión → `VersionDesconocida` (igual que en M06).

**Auditoría.** Se amplían las enumeraciones de `db/nosql/audit-table.json`; la
lista blanca de campos no cambia:

- `operacion`: se agregan `migrar_lecturas` y `revertir_migracion`;
- `actor_rol`: se agrega `migracion`.

Un registro **por lote** (no por documento), con `entidad: lote_lecturas`,
`clave_afectada: LOTE#<corrida_id>` (32 hex, el mismo para toda la corrida, así
QA2 devuelve el historial completo de una corrida), `conteo` con los documentos
cambiados en ese lote y `motivo: m07_v1_a_v2`. Sin `valor`, `payload` ni el
documento.

### 6. Compatibilidad y límites

**Qué es compatible:**

- Solo se **agregan** campos. Claves, índices y GSI no cambian:
  **`indexes.json` no se toca** y Q1-Q4 devuelven las mismas lecturas, con los
  mismos valores, antes y después de migrar.
- Un consumidor que lee solo los campos de v1 sigue funcionando sobre una
  lectura migrada: todos sus campos están y valen lo mismo.
- `put_lectura` sigue escribiendo v2 nativa; nunca escribe `fuente: "migrada"`
  ni `migracion_id`. Esos dos campos solo los pone la migración.

**Qué no es compatible:**

- El código de M06 **sin** el cambio de §5 rechaza las lecturas migradas. Por
  eso existe el orden de despliegue de §1.
- Un consumidor con validación estricta de campos (`extra="forbid"`) sobre el
  item crudo rechazaría `migracion_id`. Dentro del repo ninguno lee así; todo
  pasa por `normalizar_lectura`.

**Límites:**

- **La fase contract no se hace en M07.** La rama v1 de `normalizar_lectura` se
  quita solo cuando se cumplan las dos condiciones: una corrida de verificación
  cuenta **0 lecturas v1** y ya pasó un ciclo de despliegue sin rollback. Después
  de contract, el rollback deja de ser posible sin redesplegar el lector v1.
- El rollback sirve **solo mientras exista el respaldo**. La tabla
  `cdrl_migracion_respaldo` se conserva hasta contract; borrarla antes deja la
  migración sin vuelta atrás.
- El `Scan` lee la tabla completa: su costo crece con el número de lecturas, no
  con las v1 que queden. En AWS se corre con capacidad reservada o fuera de
  hora y, si la tabla es grande, con `Segment`/`TotalSegments`. En local, con
  120 documentos, `duracion_ms` no extrapola a millones.
- DynamoDB Local no aplica IAM: que solo la migración pueda escribir
  `migracion_id` se garantiza por la API de `EventsStore`, no por una política.
  En AWS, el rol `migracion` es el único con `UpdateItem` sobre `cdrl_eventos` y
  con permisos sobre `cdrl_migracion_respaldo`.
- Sin PITR en local. En AWS se activa PITR en `cdrl_eventos` antes de migrar,
  como segunda red además del respaldo por documento.

### 7. Script, medidas y pruebas

**`scripts/migrate_m07.py`** (semilla fija 42, equipo `fixture-m07`):

| Opción | Qué hace |
| --- | --- |
| `--dry-run` | Recorre y clasifica sin escribir nada |
| `--aplicar` | Migra |
| `--rollback` | Revierte con §4 |
| `--lote N` | Tamaño del lote (por omisión 25) |

En el Makefile: `make migrate-docs` (`--aplicar`) y `make rollback-docs`
(`--rollback`). El script termina con código distinto de 0 si hubo
`version_desconocida` o `no_revertible`.

**Medidas** (sección `migracion` de `artifacts/m07-document-evolution.json`):

| Medida | Criterio de éxito con el fixture |
| --- | --- |
| `escaneadas` | 121 (120 del fixture + 1 v3 del fallo declarado) |
| `migradas` | 40 |
| `ya_v2` | 80 |
| `version_desconocida` | 1, sin modificar |
| `segunda_corrida_migradas` | 0 |
| `reanudacion_migradas_total` | 40 tras interrumpir a la mitad, sin duplicados |
| `respaldos_creados` | 40 |
| `revertidas` | 40 |
| `segunda_corrida_revertidas` | 0 |
| `diferencias_campo_tras_rollback` | 0 |
| `v2_originales_intactas` | 80 de 80 |
| `consultas_iguales_antes_despues` | `true` (Q1 sobre el fixture) |
| `duracion_ms` | se reporta |

**Pruebas** (`tests/test_m07_document_migration.py`, contra el
`events_store.py` real sobre DynamoDB Local; fixture de 120 lecturas: 40 v1
escritas en crudo y 80 v2 con `put_lectura(fuente="fixture")`):

| Caso | Criterio de M07 | Qué se espera |
| --- | --- | --- |
| Normal | migración + legibilidad | migra las 40 v1; las 80 v2 no cambian; Q1 devuelve lo mismo antes y después |
| Límite 1 | repetible | la segunda corrida da `migradas: 0` y no cambia ningún item |
| Límite 2 | repetible | interrumpir la migración a la mitad de un lote (`detener_tras=30`, que lanza `MigracionInterrumpida`) y reanudar termina las 40, con 40 respaldos y sin duplicados |
| Fallo declarado | evolución | un documento con `schemaVersion: 3` cuenta como `version_desconocida`, se reporta con `VersionDesconocida` y queda idéntico |
| Transición | versión anterior legible | con la migración a medias, `normalizar_lectura` lee v1, v2 nativa y v2 migrada al mismo formato |
| Rollback | rollback probado | revertir deja las 40 v1 iguales campo por campo a su respaldo, sin los cuatro campos nuevos; las 80 v2 intactas; segunda corrida revierte 0 |
| Auditoría | sin secretos | cada lote deja su registro con `conteo`; ninguno trae `valor`, `payload`, el endpoint ni campos fuera de la lista blanca |

**Evidencia:** igual que en M06. Reporte por caso (`casos` con `id`, `prueba`,
`criterio`, `esperado`, `obtenido`, `codigo_error`, `resultado`) generado por
`make verify`, gitleaks dentro de `make verify`, y
`evidence/m07-document-evolution.json` con `assignmentId`, `commitSha`, `tag`,
`commands`, `results`, `assumptions` y `limitations`, registrado en
`required_files` y en la revisión de campos de `verify_base.sh`. **El
`commitSha` de la evidencia tiene que coincidir con el commit del tag final**
(observación del profesor en M06).

## Alternativas descartadas

- **Seguir sin migrar (solo upcast al leer, como en M06).** Cumple la
  legibilidad, pero no hay migración que repetir ni rollback que probar, y los
  consumidores que leen items crudos tendrían que conocer v1 para siempre.
- **Migrar copiando a una tabla nueva (`cdrl_eventos_v2`) y cambiar de tabla.**
  Rollback trivial (volver a la tabla vieja), pero duplica todo el almacenamiento,
  obliga a sincronizar las escrituras que llegan durante la copia y cambia la
  configuración de todos los lectores. Para agregar cuatro campos es mucho más
  riesgo que una transacción por documento.
- **Respaldo único en JSONL antes de migrar, como en `recover_m06.py`.** Sirve
  para recuperar, pero si la corrida se cae a la mitad no dice qué documentos sí
  se migraron. El respaldo por documento dentro de la misma transacción lo dice
  siempre.
- **Rollback quitando los campos con `REMOVE`.** Funciona para estos cuatro
  campos, pero confía en que la migración solo agregó campos. Restaurar la copia
  completa sirve aunque una migración futura cambie valores, y se puede comparar
  campo por campo.
- **Bajar `registrado_en` a `medido_en` en las migradas.** Inventa un dato:
  `medido_en` es cuándo midió el sensor, no cuándo se registró la lectura.

## Consecuencias

- **Marco Antonio Osorio Hernandez:** `migrar_lecturas_v1(lote=25, detener_tras=None)`,
  `revertir_migracion(migracion_id)`, `MigracionInterrumpida`, `normalizar_lectura`
  con `fuente: "migrada"`, `ensure_table` también para
  `migration-backup-table.json`, auditoría por lote, `scripts/migrate_m07.py` y
  los objetivos `migrate-docs` y `rollback-docs` del Makefile.
- **Persona 3:** las pruebas de la tabla del §7, `scripts/build_m07_artifact.py`
  con `casos` y `migracion`, la evidence y su registro en `verify_base.sh`.
- `recover_m06.py` y sus pruebas no cambian: su fixture vive en `fixture-m06` y
  la migración de M07 se prueba sobre `fixture-m07`.

## Relacionado
- `docs/ADR-006-evolucion-auditoria-recuperacion.md`
- `docs/ADR-005-almacen-documental.md`
- `db/nosql/indexes.json`
- `db/nosql/audit-table.json`
- `db/nosql/migration-backup-table.json`
