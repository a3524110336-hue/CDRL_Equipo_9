# ADR-006 — Evolución de documentos, auditoría y recuperación del almacén de eventos

## Estado
Aceptado. Define el contrato de M06 para `src/events_store.py` (Persona 2) y
para las pruebas y la evidencia (Persona 3). Los nombres de campos, tablas y
operaciones de este ADR son los que usa el código; si el código necesita otro,
se cambia aquí primero.

## Contexto

M06 pide tres cosas sobre el almacén documental de M05 (ADR-005):

1. **Evolución:** que versiones antigua y nueva de un documento coexistan o
   migren, según una estrategia declarada.
2. **Auditoría:** que una operación quede registrada **sin secretos**.
3. **Recuperación:** que un procedimiento recupere un fixture tras una falla
   controlada y **mida** el resultado.

Lo que ya hay y condiciona la decisión:

- Las lecturas son **append-only** (ADR-005 §4): se escriben con
  `attribute_not_exists(PK)` y nunca se actualizan. Reescribirlas para migrarlas
  rompería esa regla.
- La validación es estricta: `LecturaEntrada` tiene `extra="forbid"` y el
  documento se valida **antes** de escribir (ADR-005 §2). Un campo nuevo no
  puede colarse sin cambiar el contrato.
- DynamoDB Local no tiene respaldos bajo demanda ni PITR, y no aplica IAM. Todo
  lo que en AWS haría el servicio, aquí se hace con operaciones explícitas, y
  así se declara en *Limitaciones*.
- La retroalimentación de M03 (2026-10-10) pidió reporte por caso, escaneo de
  secretos dentro de `make verify` y que la documentación describa lo que el
  código hace de verdad. Este ADR lo incorpora como contrato de la evidencia.

## Decisión

### 1. Evolución: `schemaVersion` y lectura con upcast, sin migración masiva

**Qué cambia de v1 a v2 (solo lecturas):**

| Campo | v1 (M05, documentos existentes) | v2 (M06 en adelante) |
| --- | --- | --- |
| `schemaVersion` | **ausente** | `2` (número) |
| `fuente` | ausente | obligatorio: `"api"`, `"lote"` o `"fixture"` |
| `registrado_en` | ausente | obligatorio: instante UTC en que el store escribió el item |
| resto (`PK`, `SK`, `entidad`, `lectura_id`, `equipo_codigo`, `metrica`, `unidad`, `valor`, `medido_en`, `payload` opcional) | igual | igual |

Por qué estos dos campos y no otros: `registrado_en` es lo que permite a la
recuperación (§3) distinguir lo que llegó **después** del respaldo, y `fuente`
dice si una lectura entró por la API, por una carga o por un fixture de prueba.
Ninguno cambia claves ni índices: **`indexes.json` no se toca** y Q1-Q4 siguen
igual.

**Reglas:**

- **Escritura:** `put_lectura` escribe **siempre** v2. `fuente` la pasa quien
  llama; `registrado_en` y `schemaVersion` los pone el store, nunca el cliente.
- **Lectura:** toda lectura que sale de `EventsStore` (`get_lectura`,
  `query_lecturas`, `iter_lecturas`, `ultimas_lecturas`) pasa por **una sola
  función**, `normalizar_lectura(item) -> dict`, que devuelve el mismo formato
  para las dos versiones:
  - sin `schemaVersion` → v1: se devuelve con `schemaVersion: 1`,
    `fuente: "desconocida"` y `registrado_en: None`;
  - `schemaVersion == 2` → se valida que traiga `fuente` y `registrado_en`;
  - **cualquier otro valor** (`3`, `"2"` como cadena, `0`, `true`) →
    `VersionDesconocida` (subclase de `DocumentoInvalido`). No se adivina: un
    documento de una versión que el código no conoce es un fallo declarado.
- **No hay migración masiva.** Los documentos v1 se quedan v1 para siempre. El
  upcast es una función pura que corre al leer.
- **Restaurar no actualiza la versión:** un documento v1 que sale de un respaldo
  vuelve a la tabla **idéntico**, sin `schemaVersion` (§3). Si la restauración
  lo "modernizara", ya no sería el mismo documento y la comparación contra el
  respaldo dejaría de servir.
- **Alertas:** no cambian en M06. No llevan `schemaVersion`; `_validate_alerta`
  sigue rechazando cualquier campo extra.

**Orden de despliegue** (para cuando haya más de un proceso): primero el código
que **lee** v2, después el que la **escribe**. En este repo los dos cambian en el
mismo commit, así que no hay ventana mixta, pero la regla queda escrita.

### 2. Auditoría: tabla aparte, append-only, lista blanca de campos

**Dónde:** tabla **`cdrl_auditoria`** (variable `DYNAMODB_AUDIT_TABLE`),
declarada en **`db/nosql/audit-table.json`** igual que `indexes.json` declara la
de eventos. Va separada de `cdrl_eventos` por tres razones: los permisos de AWS
se dan por tabla (la app puede tener `PutItem` sin `UpdateItem`/`DeleteItem`
solo ahí), restaurar o borrar eventos nunca toca la evidencia, y su volumen y
retención son otros.

**Claves e índices:**

| Clave | Valor |
| --- | --- |
| `PK` | `AUD#<YYYY-MM-DD>` del instante UTC |
| `SK` | `TS#<ocurrido_en>#<operacion_id>` |
| GSI `AUD_CLAVE` | `clave_afectada` + `SK` |

- QA1: la auditoría de un día en orden → `Query` a la tabla.
- QA2: el historial de una alerta o de un lote → `Query` a `AUD_CLAVE`.
- Ninguna usa `Scan`, igual que Q1-Q4.

**Campos: lista blanca.** Solo existen estos. Cualquier otro nombre hace que el
registro se rechace con `AuditoriaInvalida` (subclase de `DocumentoInvalido`) y
**no se escribe nada**.

| Campo | Obligatorio | Contenido |
| --- | --- | --- |
| `schemaVersion` | sí | `1` |
| `operacion_id` | sí | 32 hex aleatorios (`uuid4().hex`) |
| `operacion` | sí | `actualizar_estado_alerta`, `delete_alerta`, `falla_controlada`, `restaurar_lecturas` |
| `actor_rol` | sí | `cdrl_writer`, `cdrl_ops` o `recuperacion`: el **rol**, nunca una persona |
| `ocurrido_en` | sí | instante UTC |
| `entidad` | sí | `alerta` o `lote_lecturas` |
| `clave_afectada` | sí | `LECTURA#<19 dígitos>#UMBRAL#<19 dígitos>` o `LOTE#<32 hex>` |
| `resultado` | sí | `ok` o `rechazada` |
| `codigo_error` | no | nombre de la excepción o código de boto3, solo letras (`TransicionAlertaInvalida`) |
| `estado_anterior`, `estado_nuevo` | no | estados de alerta |
| `conteo` | no | entero ≥ 0, documentos afectados en operaciones por lote |
| `motivo` | no | `snake_case`, máximo 64 caracteres (`borrado_accidental`) |

Los patrones exactos están en `audit-table.json`. Que `motivo` y `codigo_error`
solo admitan letras, dígitos y `_` es deliberado: impide pegar una URL, un
mensaje de error con la cadena de conexión o un token en un campo "de texto".

**Lo que nunca va en la auditoría:**

- contraseñas, tokens, claves de AWS (`AKIA…`), cabeceras `Authorization`;
- el endpoint de DynamoDB o cualquier cadena de conexión;
- **datos de negocio**: `valor`, `payload` ni el documento completo antes o
  después. Para una alerta basta el estado anterior y el nuevo;
- nombres de personas o direcciones IP.

Además de la lista blanca, `registrar_auditoria` revisa cada valor de texto
contra patrones de secreto (`AKIA[0-9A-Z]{16}`, `://usuario:clave@`,
`password`, `secret`, `token`, `bearer`, sin distinguir mayúsculas) y rechaza el
registro si alguno aparece. La lista blanca es la defensa principal; los
patrones son la red por si alguien amplía una enumeración sin pensar.

**Append-only:**

- `EventsStore` solo expone `registrar_auditoria` (un `PutItem` con
  `attribute_not_exists(PK)`) y las dos consultas. **No existe** un método para
  actualizar ni borrar auditoría.
- Repetir el mismo `operacion_id` falla con `ConditionalCheckFailedException`:
  un registro no se puede sobrescribir.
- En AWS, la política IAM del rol de la aplicación permite `PutItem` y `Query`
  sobre `cdrl_auditoria` y **niega** `UpdateItem`, `DeleteItem` y
  `BatchWriteItem`, con PITR activado en la tabla.

**Qué se audita y cómo:**

| Operación | Cuándo | Cómo |
| --- | --- | --- |
| `actualizar_estado_alerta` que cambia el estado | siempre | **`TransactWriteItems`**: el `Update` condicional de la alerta y el `Put` de la auditoría van juntos. O se escriben los dos o ninguno |
| `actualizar_estado_alerta` rechazada por `TransicionAlertaInvalida` | siempre | `Put` simple con `resultado: rechazada` y `codigo_error`; no hubo cambio de datos que proteger |
| `delete_alerta` de una alerta existente | siempre | `TransactWriteItems`: `Delete` con `attribute_exists(PK)` + `Put` de auditoría |
| repetir una operación que no cambia nada (mismo estado, borrar algo ausente) | **no** | no hay cambio; auditarlo solo llenaría la tabla de ruido |
| `falla_controlada` y `restaurar_lecturas` | una vez por corrida | `Put` al final de cada paso, con `conteo` y la clave del lote |

La transacción es la decisión importante de esta sección: sin ella, una alerta
podría cambiar de estado y fallar después la escritura de su auditoría, y la
operación quedaría sin rastro, que es justo lo que M06 pide evitar.
`TransactWriteItems` no devuelve el item nuevo como `ReturnValues=ALL_NEW`; el
resultado se arma con el candidato que ya se validó antes de escribir.
`delete_alerta` sigue devolviendo `True`/`False`: si la condición
`attribute_exists` falla, la transacción se cancela, no se audita y se devuelve
`False`, como en M05.

### 3. Recuperación: respaldo lógico, falla controlada y restauración selectiva

**Escenario:** se borra por error un lote de lecturas mientras siguen llegando
lecturas nuevas. Hay que devolver exactamente lo borrado **sin pisar** lo que
llegó después del respaldo.

**Procedimiento** (`scripts/recover_m06.py`, semilla fija 42, equipo
`fixture-m06`, métricas `cpu` y `memoria`):

| Paso | Qué hace | Por qué |
| --- | --- | --- |
| 1. Fixture | 120 lecturas: **40 v1** escritas en crudo con `table.put_item` (sin `schemaVersion`, como datos de antes de M06) y **80 v2** con `put_lectura(fuente="fixture")` | El fixture tiene que ser mixto, si no la recuperación no prueba la convivencia de versiones |
| 2. Respaldo lógico | `Query` por cada partición del fixture (nunca `Scan`) a un JSONL **fuera del repo**; se registran el conteo y el SHA-256 del archivo | En AWS sería un respaldo bajo demanda o PITR; DynamoDB Local no los tiene |
| 3. Escrituras posteriores | 10 lecturas v2 nuevas después del respaldo | Son las que una restauración ingenua borraría |
| 4. Falla controlada | Borrar **30** lecturas concretas (10 v1 + 20 v2) y auditar `falla_controlada` con `conteo: 30` y `clave_afectada: LOTE#<id>` | Determinista: siempre las mismas 30 |
| 5. Restaurar en tabla aparte | Crear `cdrl_eventos_restore_<id>` con la misma forma que `indexes.json` y cargar ahí el respaldo | En AWS restaurar un respaldo **siempre** crea una tabla nueva; se reproduce igual |
| 6. Diferencia | Faltantes = claves en la tabla de restauración que no están en producción | Se restaura lo que falta, no "todo el respaldo encima" |
| 7. Copia selectiva | `put_item` de cada faltante **tal cual** con `attribute_not_exists(PK)`; si la condición falla, se cuenta como `ya_existia` y no se sobrescribe | Idempotente: correrlo dos veces no duplica ni pisa nada |
| 8. Verificación | Lectura fuerte de cada restaurada y comparación **campo por campo** contra el respaldo; las 10 posteriores siguen intactas | Una restauración no está hecha hasta medirla |
| 9. Auditoría y limpieza | Auditar `restaurar_lecturas` con `conteo`; borrar la tabla de restauración | El respaldo temporal no se queda vivo |

**Mediciones** (van al artifact, §4):

| Medida | Criterio de éxito |
| --- | --- |
| `esperados` | 30 |
| `restaurados` | 30 |
| `ya_existian` | 0 en la primera corrida |
| `faltantes_tras_restaurar` | 0 |
| `diferencias_campo` | 0 |
| `v1_restauradas_sin_schemaVersion` | 10 de 10 |
| `posteriores_intactas` | 10 de 10 |
| `total_final` | 130 (120 + 10) |
| `duracion_restauracion_ms` | se reporta (RTO medido en local; no es una promesa de producción) |
| `escrituras_posteriores_perdidas` | 0 (RPO de la restauración selectiva) |

Una **segunda corrida** de los pasos 6-8 debe dar `restaurados: 0` y
`faltantes_tras_restaurar: 0`: la idempotencia también se mide.

### 4. Evidencia: reporte por caso y escaneo de secretos en `make verify`

Pedido en la revisión de M03 y obligatorio desde M06:

- **Reporte por caso.** `artifacts/m06-evolution-recovery.json` trae una lista
  `casos` donde cada prueba aparece con `id`, `prueba`, `criterio`, `esperado`,
  `obtenido`, `codigo_error` (`null` si no aplica) y `resultado` (`ok`/`fallo`).
  No basta `passed: true`. Lo genera `make verify` en cada corrida, no se escribe
  a mano.
- **Mediciones de recuperación** en el mismo artifact, bajo `recuperacion`, con
  los nombres de la tabla del §3.
- **Escaneo de secretos dentro de `make verify`.** `scripts/verify_base.sh` corre
  gitleaks (imagen de Docker con versión fija, sin instalar nada) sobre el
  historial; si encuentra algo, verify falla. El job de GitHub Actions se queda
  como segunda red.
- `evidence/m06-evolution-recovery.json` con los campos de siempre
  (`assignmentId`, `commitSha`, `commands`, `results`, `assumptions`,
  `limitations`) y agregado a `required_files` y a la revisión de campos de
  `verify_base.sh`.

**Qué prueba cubre cada criterio** (`tests/test_m06_evolution_recovery.py`,
contra el `events_store.py` real sobre DynamoDB Local, **nunca contra un
emulador**):

| Caso | Criterio de M06 | Qué se espera |
| --- | --- | --- |
| Normal | versiones coexisten | una v1 cruda y una v2 se leen con `normalizar_lectura` al mismo formato; Q1 devuelve las dos |
| Límite 1 | versiones coexisten | una v1 sin `fuente` ni `registrado_en` se lee con `fuente: "desconocida"` y `registrado_en: None`, sin error |
| Límite 2 | recuperación | restaurar una lectura que ya existe no la duplica ni la sobrescribe (`ya_existia`) |
| Fallo declarado | evolución | `schemaVersion: 3` (y `"2"` como cadena) → `VersionDesconocida` |
| Fallo declarado | auditoría sin secretos | un registro con un campo fuera de la lista blanca, o con `motivo` que contiene `password`, → `AuditoriaInvalida`, y la tabla no cambia |
| Auditoría | operación auditada | cambiar el estado de una alerta deja exactamente un registro con `estado_anterior` y `estado_nuevo`; el registro no contiene `valor`, `payload` ni el endpoint |
| Auditoría | append-only | reescribir el mismo `operacion_id` → `ConditionalCheckFailedException` |
| Recuperación | recupera y mide | el procedimiento del §3 deja `faltantes_tras_restaurar: 0`, `diferencias_campo: 0` y `posteriores_intactas: 10` |

## Alternativas descartadas

- **Migración masiva de v1 a v2.** Reescribir las lecturas viejas rompe el
  append-only de ADR-005, consume escritura de toda la tabla y no aporta nada:
  la conversión es una función pura que se puede hacer al leer. Se reabre solo
  si llega un cambio que no se pueda calcular desde v1 (por ejemplo, un campo
  que exija el dato original del sensor).
- **Auditoría en la misma tabla de eventos** (items con `entidad: auditoria`).
  Descartada: en AWS no se puede dar `PutItem` sin `DeleteItem` a una parte de
  una tabla, y restaurar eventos podría arrastrar la evidencia.
- **Auditar después de la operación, sin transacción.** Más simple, pero deja la
  ventana en la que el dato cambió y la auditoría falló. Es justo el caso que
  M06 pide cubrir.
- **Lista negra de campos prohibidos en lugar de lista blanca.** Una lista negra
  siempre llega tarde: el campo que filtra el secreto es el que nadie pensó en
  prohibir. Se conservan los patrones de secreto, pero como segunda red.
- **Restaurar el respaldo completo encima de producción.** Recupera las 30
  borradas y destruye las 10 que llegaron después. Esas no están en ningún
  respaldo: se pierden para siempre.

## Limitaciones

- DynamoDB Local no aplica IAM: el append-only se demuestra por diseño de la
  API (no hay método para actualizar ni borrar) y por la condición de
  `PutItem`, no por una política. La política IAM queda escrita para AWS.
- DynamoDB Local no tiene respaldos ni PITR: el respaldo es lógico (Query a
  JSONL). En AWS se sustituye el paso 2 por un respaldo bajo demanda y el paso 5
  por su restauración a tabla nueva; los pasos 6-9 no cambian.
- `duracion_restauracion_ms` es local y con 30 documentos; no extrapola a
  millones.
- El escaneo de secretos ve el historial de Git, no el contenido de las tablas;
  por eso la auditoría valida sus propios campos al escribir.

## Consecuencias

- **Persona 2:** `normalizar_lectura`, `VersionDesconocida`, `put_lectura` con
  `fuente`, `registrar_auditoria` con lista blanca, `AuditoriaInvalida`,
  `TransactWriteItems` en `actualizar_estado_alerta` y `delete_alerta`,
  `ensure_table` también para `audit-table.json`, `scripts/recover_m06.py` y
  gitleaks en `verify_base.sh`.
- **Persona 3:** las pruebas de la tabla del §4, el artifact con `casos` y
  `recuperacion`, la evidence y su registro en `verify_base.sh`.
- Las pruebas de M05 que comparan items crudos pueden necesitar `fuente` al
  llamar a `put_lectura`; el contrato de lectura (`normalizar_lectura`) es el
  que deben usar las pruebas nuevas.

## Relacionado
- `docs/ADR-005-almacen-documental.md`
- `db/nosql/indexes.json`
- `db/nosql/audit-table.json`
- `docs/ADR-003-roles-y-minimo-privilegio.md` (roles y secretos)
