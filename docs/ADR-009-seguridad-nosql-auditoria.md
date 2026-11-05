# ADR-009 — Controles por colección y operación, pruebas de abuso y auditoría sin secretos

## Estado
Aceptado. Agrega `db/nosql/access-policy.json` (matriz de acceso),
`db/nosql/iam/` (políticas IAM derivadas de la matriz) y
`scripts/build_iam_policies.py`; amplía `db/nosql/audit-table.json` sin quitar
nada de ADR-006. Define el contrato de M09 para `src/nosql_access.py` y los
cambios de `src/events_store.py` (Persona 2, Marco Antonio Osorio Hernandez) y
para las pruebas de abuso, el artifact y la evidencia (Persona 3, Jesús
Alejandro Trujillo Castro). Los nombres de este ADR son los que usa el código;
si el código necesita otro, se cambia aquí primero.

## Contexto

M09 pide **diseñar controles por colección y operación, probar abuso y
demostrar una auditoría útil sin registrar secretos**, con tres criterios:

1. las lecturas y escrituras no autorizadas fallan;
2. cada evento incluye actor, acción, recurso y resultado, sin secretos;
3. se documenta y se prueba una remediación.

Lo que ya hay y condiciona la decisión:

- El almacén documental tiene cuatro colecciones en tres tablas: `lecturas` y
  `alertas` comparten `cdrl_eventos` (se distinguen por la clave de partición,
  `EQ#…` y `LECTURA#…`), `auditoria` vive en `cdrl_auditoria` (ADR-006) y
  `respaldo_migracion` en `cdrl_migracion_respaldo` (ADR-007).
- La auditoría de ADR-006 ya es solo-agregar, valida una lista blanca de campos
  y rechaza textos que parezcan secretos. Registra `actor_rol`, `operacion`,
  `clave_afectada` y `resultado`, pero **no registra quién** (solo el rol) y
  solo audita escrituras sobre alertas y lotes.
- **Nada revisa permisos en el almacén documental.** El rol llega como
  argumento: `delete_alerta(..., actor_rol="cdrl_writer")` borra la alerta y la
  auditoría la guarda como `ok`. Las consultas no piden rol y un intento que
  debería negarse no deja rastro. Es el hallazgo H1 (§6).
- Los mínimos privilegios de M03 cubren PostgreSQL (roles `cdrl_writer`,
  `cdrl_ops`, `cdrl_migrator`, ADR-003); el almacén documental se quedó fuera.
- DynamoDB Local **no evalúa IAM**: acepta cualquier credencial. El Learner
  Lab no deja crear roles ni políticas (solo existe `LabRole`).
- Hoy usan `Scan` la migración de M07 (`migrar_lecturas_v1`), el backfill de
  M08 (`backfill_gsi2`) y la comprobación de tabla vacía de
  `load_events.py --batch`.

## Decisión

### 1. El control vive en el código y se declara en un solo archivo

Como DynamoDB Local no aplica IAM, una política IAM sola no se puede probar en
este entorno: el control que se demuestra es el de la aplicación. Por eso hay
**una sola fuente**, `db/nosql/access-policy.json`, de la que salen las dos
capas:

- **En el código** (`src/nosql_access.py`), que se prueba contra DynamoDB Local.
- **En AWS** (`db/nosql/iam/<rol>.json`), generadas por
  `scripts/build_iam_policies.py`. `tests/test_m09_access_policy.py` falla si
  alguien edita una de las dos sin regenerar la otra.

### 2. Matriz rol × colección × operación, negada por defecto

Operaciones: `get`, `query`, `put`, `update`, `delete` y `scan`. Lo que no
aparece está prohibido.

| Rol | Actores | `lecturas` | `alertas` | `auditoria` | `respaldo_migracion` |
| --- | --- | --- | --- | --- | --- |
| `cdrl_writer` | `svc-api`, `svc-carga` | put | put | — | — |
| `cdrl_ops` | `ops-01`, `ops-02` | get, query | get, query, update, delete | — | — |
| `cdrl_auditor` | `aud-01` | — | — | query | — |
| `migracion` | `job-migracion`, `job-backfill` | get, query, scan, put, update | — | — | get, query, put, delete |
| `recuperacion` | `job-recuperacion` | get, query, put | — | — | — |
| `simulacro` | `job-simulacro` | delete | — | — | — |

Reglas:

- **`scan` está prohibido para todos menos `migracion` sobre `lecturas`.** Los
  dos jobs por lotes necesitan recorrer la tabla entera y no tienen una clave
  por la cual consultar. `load_events.py --batch` deja de hacer `Scan` para
  saber si la tabla está vacía y usa `DescribeTable` (`ItemCount`).
- **Nadie tiene `update` ni `delete` sobre `auditoria`, y nadie tiene `put`
  directo.** Los eventos los anexa el propio almacén (§5).
- **`cdrl_writer` solo escribe documentos nuevos.** No lee, no actualiza y no
  borra: si le roban la credencial a la API, puede meter basura, pero no puede
  leer ni destruir lo que ya existe.
- **`cdrl_auditor` es nuevo.** Revisar la auditoría es una función distinta de
  operar: el operador no consulta la bitácora que registra lo que él hace.
- **`simulacro` es nuevo y solo existe en `local` y `ci`** (variable
  `CDRL_ENTORNO`). Es quien borra las 30 lecturas en el simulacro de M06; antes
  lo hacía `recuperacion`, que así podía borrar lo que tenía que restaurar.
- **`migracion` tiene `put` sobre `lecturas`** porque `revertir_migracion`
  restaura el documento original con `PutItem` condicionado a su
  `migracion_id`. `indexes.json` sigue listando «UpdateItem sobre lecturas»
  como prohibido desde ADR-005; ADR-007 ya lo había exceptuado para la
  migración, y aquí queda escrita esa excepción con el rol que la tiene.

### 3. El principal sale de la sesión, nunca de un argumento

`Principal(actor, rol)` se arma una vez al construir el almacén, desde
`CDRL_ACTOR` y `CDRL_ROL` (o desde el fixture de pruebas), y se valida contra la
matriz: el rol tiene que existir, el actor tiene que estar en sus `actors` y,
si el rol declara `environments`, `CDRL_ENTORNO` tiene que estar en la lista.
Si algo no cuadra, el almacén no se construye.

Ninguna operación pública acepta ya `actor_rol`. Los actores son
identificadores sintéticos (`svc-…`, `ops-…`, `aud-…`, `job-…`) y nunca llevan
un nombre, un correo ni una matrícula.

`autorizar(principal, coleccion, operacion)` se llama **antes** de construir
cualquier petición a DynamoDB. Si la matriz no lo permite, lanza
`AccesoDenegado` (subclase de `PermissionError`), anexa el evento `denegada`
(§5) y no toca la tabla.

### 4. Políticas IAM para AWS, validadas por estructura

`db/nosql/iam/<rol>.json` traduce la matriz a IAM:

- `Allow` por colección con las acciones de su fila y la condición
  `ForAllValues:StringLike dynamodb:LeadingKeys`. Así `cdrl_ops` puede
  actualizar `LECTURA#…` (alertas), pero no `EQ#…` (lecturas), aunque las dos
  estén en `cdrl_eventos`. Las claves incluyen las de cada GSI consultable
  (`ALERTA#…` en GSI1, `EQ#…` en GSI2 y `clave_afectada` en `AUD_CLAVE`).
- `Allow PutItem` sobre `cdrl_auditoria` para todos, porque todos anexan
  eventos.
- `Deny` explícito de `UpdateItem`, `DeleteItem` y `BatchWriteItem` sobre
  `cdrl_auditoria`, y de `Scan` en todas las tablas que el rol no recorre.
- Ningún `BatchWriteItem` permitido: esa acción también borra, y
  `LeadingKeys` no distingue un put de un delete dentro del lote. El modo
  `--batch` de `load_events.py` queda solo para local.
- Región y cuenta van como `*`: ningún identificador real entra al repositorio.

Lo que IAM no puede expresar, y por eso se queda en el código: distinguir el
anexo de auditoría que hace el almacén de un `PutItem` directo; exigir que el
`PutItem` lleve `attribute_not_exists(PK)`; y que el actor corresponda al rol.

### 5. Auditoría: qué se registra y con qué campos

`audit-table.json` agrega tres campos opcionales y valores nuevos en los
enumerados, sin quitar nada:

| Campo | Contenido | Ejemplo |
| --- | --- | --- |
| `actor` | id sintético del principal | `ops-01` |
| `accion` | `<coleccion>.<operacion>` | `alertas.delete` |
| `recurso` | `<coleccion>/<clave>`, solo claves y nunca valores | `alertas/LECTURA#0000000000000000007#UMBRAL#0000000000000000002` |
| `resultado` | `ok`, `rechazada` (regla de negocio) o **`denegada`** (matriz) | `denegada` |

Quedan opcionales en el JSON para que `main` siga en verde mientras llega el
código. Cuando lo haga, `build_auditoria` los exige en todo evento nuevo; los
de M06-M08 se quedan como están, porque la auditoría no se reescribe.

**Se registra:** toda escritura permitida (`ok` o `rechazada`), **todo intento
negado, sea de lectura o de escritura** (`denegada`) y toda consulta a
`auditoria`, porque quién mira la bitácora también es un dato de seguridad.

**No se registran** las lecturas permitidas sobre `lecturas` y `alertas`. Son
la mayor parte del tráfico y no cambian nada; registrarlas duplicaría las
escrituras de la consulta más frecuente (Q5 de ADR-008). Es una concesión
consciente: si alguien con permiso lee de más, esta auditoría no lo detecta.

**Sin secretos**, en tres capas:

1. **Lista blanca de campos** (ya existía): nada fuera de `required` y
   `optional` entra al evento; en particular, nunca `valor`, `payload`, el
   documento ni la petición.
2. **`recurso` y `clave_afectada` solo aceptan formas de clave**
   (`EQ#…`, `LECTURA#…`, `AUD#<día>`, `MIG#…`, `COLECCION#…`). Una cadena de
   conexión o un token no cumplen el patrón y se rechazan.
3. **`sanear_evento()`** antes de escribir: rechaza llaves de
   `forbidden_examples` y valores que parezcan secretos (`AKIA…`,
   `://usuario:clave@`, `Bearer …`, `password=`, `token`, `secret`) con
   `AuditoriaInvalida`. Se rechaza el evento, no se enmascara: un evento con
   `***` en medio ya no sirve para reconstruir qué pasó.

Si el evento `denegada` no se puede escribir, la operación sigue negada: un
fallo de auditoría nunca convierte un rechazo en un permiso.

### 6. Hallazgo H1 y remediación: el rol lo declaraba quien llamaba

**Antes** (`main` en `b46a6bf`):

```python
store.delete_alerta(7, 2, actor_rol="cdrl_writer")   # True: alerta borrada
# auditoría: {"actor_rol": "cdrl_writer", "operacion": "delete_alerta", "resultado": "ok"}
```

`build_auditoria` acepta ese evento porque `cdrl_writer` es un valor válido del
enumerado, y `delete_alerta` no revisa nada más. Comprobado contra el código el
2026-11-05. Impacto: cualquier componente con acceso al almacén podía
atribuirse cualquier rol, y la auditoría quedaba firmando como legítimo lo que
no lo era. Es el peor caso posible para una bitácora, porque da una falsa
prueba.

**Después:** `actor_rol` desaparece de las firmas públicas, el rol sale del
principal (§3) y `delete_alerta` pasa por `autorizar(principal, "alertas",
"delete")`. El mismo intento con un principal `cdrl_writer` lanza
`AccesoDenegado`, la alerta sigue en la tabla y queda un evento
`{"actor": "svc-api", "accion": "alertas.delete", "resultado": "denegada"}`.

**Prueba:** `test_regresion_h1_rol_autodeclarado` en
`tests/test_m09_security.py` repite el intento de arriba con un principal
`cdrl_writer` y exige `AccesoDenegado`, la alerta intacta y el evento
`denegada`. Sobre `b46a6bf` esa misma secuencia borra la alerta (es lo que
documenta este apartado); el artifact guarda el antes, tomado de aquí, y el
después, medido en la corrida.

### 7. Contrato para el código y las pruebas

**Persona 2:**

- `src/nosql_access.py`: `Principal`, `cargar_principal()`,
  `autorizar(principal, coleccion, operacion)`, `AccesoDenegado` y
  `sanear_evento(evento)`.
- `EventsStore(..., principal=...)`. Cada método público llama a `autorizar`
  antes de hablar con DynamoDB y anexa el evento según §5.
  `build_auditoria` generaliza su revisión de operación, entidad y clave a las
  entidades nuevas.
- `migrate_m07.py` y `backfill_m08.py` corren como `migracion`,
  `recover_m06.py` como `recuperacion` (la falla del simulacro, como
  `simulacro`) y `load_events.py` como `cdrl_writer`.
- `make verify` corre `python3 scripts/build_iam_policies.py --check`.

**Persona 3**, en `tests/test_m09_security.py` contra DynamoDB Local:

| Caso | Qué se prueba |
| --- | --- |
| Normal | `cdrl_ops` reconoce una alerta: `ok`, y el evento lleva `actor`, `accion`, `recurso` y `resultado` |
| Límite 1 | El rol tiene la colección, pero no la operación (`cdrl_writer` borra una alerta, `cdrl_auditor` escribe en `auditoria`): `AccesoDenegado`, la tabla no cambia y queda un evento `denegada` |
| Límite 2 | Lectura cruzada y `scan` (`cdrl_writer` consulta lecturas, `cdrl_ops` hace scan, un principal sin rol, con un rol inventado o con un actor de otro rol, y `simulacro` fuera de `local`/`ci`): negado |
| Fallo declarado | Meter un secreto en un evento (`AKIA…`, una cadena de conexión, un token): `AuditoriaInvalida`. Al final se recorre la tabla de auditoría de la prueba y no aparece ninguna coincidencia |
| Remediación | `test_regresion_h1_rol_autodeclarado` (§6) |

Más una prueba parametrizada que recorre **todas las celdas** de la matriz (6
roles × 4 colecciones × 6 operaciones) y compara lo permitido contra lo
obtenido. `scripts/build_m09_artifact.py` escribe
`artifacts/m09-nosql-security.json` con esas celdas (esperado contra obtenido),
el conteo de eventos por resultado, el barrido de secretos y el antes y después
de H1. Lo genera `make verify`, no se edita a mano.

## Consecuencias

- Un acceso no autorizado falla antes de llegar a DynamoDB y deja rastro, con
  un actor identificable aunque nunca se use un dato personal.
- Cambiar un permiso es editar una fila de `access-policy.json` y regenerar
  IAM. Las pruebas detectan si se hace a medias.
- Cada escritura cuesta un `PutItem` más en `cdrl_auditoria` (ya pasaba desde
  ADR-006) y ahora también cada intento negado y cada consulta a la auditoría.
  Una avalancha de intentos negados llena la auditoría; limitar la frecuencia
  queda fuera de M09.
- Todo el código que llama al almacén tiene que pasarle un principal. Es un
  cambio que rompe a quien llamaba sin él, y es justo lo que se busca.

## Límites

- El control probado es el de la aplicación. Las políticas IAM se validan por
  estructura y por consistencia con la matriz, pero no se ejecutan: DynamoDB
  Local no evalúa IAM y el Learner Lab no deja crear políticas.
- Quien tenga las credenciales de la tabla y llame a DynamoDB sin pasar por
  `EventsStore` se salta la capa de la aplicación. En AWS lo frenaría IAM; en
  local nada lo frena.
- La auditoría registra lo que pasa por el almacén. No registra las lecturas
  permitidas (§5) ni lo que se haga directo en la consola de AWS; eso sería
  CloudTrail, que el Learner Lab no deja configurar.
- `ItemCount` de `DescribeTable` en AWS se actualiza cada ~6 horas, así que la
  comprobación de tabla vacía de `load_events.py --batch` solo es exacta en
  local, que es el único lugar donde se permite ese modo.
