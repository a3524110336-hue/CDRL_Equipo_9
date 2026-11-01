# ADR-008 — Paginación estable, índice GSI2 por equipo y benchmark contra línea base

## Estado
Aceptado. Agrega el índice `GSI2` y la consulta Q5 a `db/nosql/indexes.json`;
Q1-Q4 de ADR-005 no cambian. Define el contrato de M08 para
`src/events_store.py` y `scripts/backfill_m08.py` (Marco Antonio Osorio
Hernandez) y para las pruebas, el benchmark y la evidencia (Persona 3). Los
nombres de campos, funciones, opciones y medidas de este ADR son los que usa el
código; si el código necesita otro, se cambia aquí primero.

## Contexto

M08 pide **medir y mejorar consultas documentales bajo una carga reproducible,
sin convertir rendimiento en una afirmación no verificada**, con tres criterios:

1. paginación estable ante inserciones;
2. benchmark con p50/p95, muestra y entorno;
3. comparación con línea base y trade-off explicado.

Lo que ya hay y condiciona la decisión:

- Q1 sin filtro de métrica (`EventsStore.ultimas_lecturas`) hace **5 Query
  secuenciales**, una por métrica, y mezcla los resultados en el cliente por
  `SK` descendente. Devuelve las últimas N, pero **no tiene cursor**: no hay
  forma de pedir «la página siguiente».
- Sin cursor, la única paginación posible es por desplazamiento: para la página
  *n* se piden `n × tamaño` lecturas y se descartan las primeras. El costo crece
  con la profundidad y, si entran lecturas nuevas entre una página y otra, todo
  se recorre hacia abajo: la página siguiente **repite** lecturas que ya se
  vieron.
- `query_lecturas` (una métrica) ya expone `LastEvaluatedKey`, pero crudo: el
  consumidor tiene que guardar un dict de DynamoDB y nadie valida que lo
  devuelva intacto.
- La clave de rango de las lecturas, `SK = TS#{medido_en_utc}#{lectura_id:019d}`,
  **no cambia nunca**: las lecturas son append-only (ADR-005 §4) y la migración
  de M07 solo agregó campos fuera de la clave (ADR-007 §6).
- DynamoDB Local no tiene particiones reales, ni red, ni throttling: sus
  latencias no son las de AWS.
- `GET /lecturas` de la API lee de PostgreSQL, no del almacén documental. M08
  habla de consultas documentales; la API relacional queda fuera (ver §6).

## Decisión

### 1. Índice `GSI2`: lecturas por equipo, en el mismo orden que la tabla

| Atributo | Valor |
| --- | --- |
| `IndexName` | `GSI2` |
| Clave de partición | `GSI2PK = EQ#{equipo_codigo}` |
| Clave de rango | **`SK`, el mismo atributo de la tabla** |
| Proyección | `ALL` |
| Entidades | solo `lectura` (índice disperso: las alertas no llevan `GSI2PK`) |

Se declara en `indexes.json` junto con la consulta **Q5 `LECTURAS_POR_EQUIPO`**:
`GSI2PK = EQ#<equipo> AND SK BETWEEN TS#<desde># AND TS#<hasta>#~`,
`ScanIndexForward = false`, de 1 a 1000 por página.

**Por qué la clave de rango es `SK` y no un `GSI2SK` nuevo.** `SK` ya ordena por
instante UTC y desempata por id numérico, que es exactamente el orden de Q1
mezclado en el cliente. Usar el mismo atributo da tres cosas: Q5 devuelve las
lecturas **en el mismo orden que la línea base**, de modo que el benchmark puede
comprobar que A y B devuelven los mismos ids; la escritura solo agrega **un**
atributo (`GSI2PK`); y no hay una segunda fórmula de orden que mantener. Dentro
de un equipo, `SK` no se repite porque `lectura_id` viene del `BIGSERIAL` de
PostgreSQL.

### 2. Escritura del índice y relleno de las lecturas viejas

- **`put_lectura`** agrega `GSI2PK = "EQ#" + equipo_codigo` a toda lectura
  nueva. Es lo único que cambia en la escritura.
- **`ensure_table`**: si la tabla existe y la **única** diferencia con
  `indexes.json` son GSI declarados que faltan, los crea con `UpdateTable` y
  espera a que estén `ACTIVE`. Cualquier otra diferencia sigue siendo
  `ValueError`, como hoy. Las tablas de `make verify` y de las pruebas se crean
  desde cero y ya nacen con el GSI2.
- **Relleno:** `scripts/backfill_m08.py` (`make backfill-gsi2`) pone `GSI2PK` a
  las lecturas que no lo tienen:
  - recorre con `Scan` paginado, filtro
    `begins_with(PK, "EQ#") AND attribute_not_exists(GSI2PK)`, en lotes de 25;
  - escribe con `UpdateItem SET GSI2PK = :pk` condicionado a
    `attribute_exists(PK) AND attribute_not_exists(GSI2PK)`; si la condición
    falla, la lectura cuenta como `ya_indexada`, no como error;
  - es **repetible**: la segunda corrida da `actualizadas: 0`;
  - no toca ningún campo del documento ni la clave; por eso no pasa por el
    respaldo de M07;
  - deja un registro de auditoría por lote con `operacion: "backfill_gsi2"`
    (ya agregada a los valores permitidos en `audit-table.json`),
    `actor_rol: "migracion"`, `entidad: "lote_lecturas"`,
    `clave_afectada: "LOTE#<uuid4 hex>"` y `conteo`; sin `valor` ni `payload`;
  - opciones `--dry-run` y `--aplicar`; imprime `escaneadas`, `actualizadas`,
    `ya_indexadas` y `fallidas`, y sale con código distinto de 0 si
    `fallidas > 0`.
- La migración de M07 y la restauración de M06 **no cambian**. Una lectura
  restaurada desde un respaldo viejo llega sin `GSI2PK`; después de restaurar se
  corre `make backfill-gsi2`.

Orden de despliegue: (1) `ensure_table` crea el GSI2; (2) se despliega el
`put_lectura` que escribe `GSI2PK`; (3) relleno; (4) los lectores empiezan a usar
Q5. Hasta el paso 3, Q5 no ve las lecturas viejas, así que Q1 sigue siendo la
consulta de producción.

### 3. Paginación con cursor opaco

```python
def paginar_lecturas_equipo(self, equipo_codigo: str, *, desde=None, hasta=None,
                            limite: int = 100, cursor: str | None = None) -> dict:
    ...  # devuelve {"items": [...], "cursor": str | None}
```

- Valida `equipo_codigo`, `desde`, `hasta` y `limite` (1..1000) con
  `ConsultaLecturas`, igual que Q1.
- **Primera página** (`cursor=None`): si no viene `hasta`, se fija en el instante
  UTC actual. Ese `hasta` queda guardado en el cursor y **no se mueve** en las
  páginas siguientes.
- Pide `Limit = limite + 1` a GSI2 con `ScanIndexForward=False` y
  `ReturnConsumedCapacity="INDEXES"`. Si DynamoDB corta en 1 MiB antes de juntar
  `limite + 1`, sigue con `ExclusiveStartKey` hasta juntarlos o agotar la
  ventana. El elemento de más solo sirve para saber si hay otra página: no se
  devuelve.
- Los items pasan por `normalizar_lectura`, como en Q1.
- **`cursor`** es `None` si no hay más lecturas. Si hay, se construye con la
  **última lectura devuelta**, no con `LastEvaluatedKey`. Así, la última página
  sale exacta y sin cursor, aunque tenga justo `limite` lecturas.

**Formato del cursor:** base64url sin relleno de un JSON compacto:

```json
{"v": 1, "equipo": "edge-01", "desde": null, "hasta": "2026-11-01T18:00:00.000000Z",
 "ultima": {"PK": "EQ#edge-01#cpu", "SK": "TS#2026-11-01T17:58:00.000000Z#0000000000000004211"}}
```

Para continuar, `ExclusiveStartKey = {"PK": ultima.PK, "SK": ultima.SK,
"GSI2PK": "EQ#" + equipo}`: la clave de la tabla más la del índice, que es lo que
DynamoDB pide para un GSI.

**`CursorInvalido(ValueError)`**, que se lanza **antes de consultar DynamoDB**,
si:

- no se puede decodificar (base64 o JSON);
- `v` no es `1`, o sobran o faltan claves;
- `equipo` no es el `equipo_codigo` pedido;
- llegan `desde` o `hasta` distintos de los del cursor;
- `ultima.PK` no empieza por `EQ#<equipo>#` o `ultima.SK` no tiene el formato
  de `SK` de lectura, o queda fuera de la ventana `desde`/`hasta`.

El cursor **no va firmado**. Solo lleva posiciones dentro de las lecturas del
mismo equipo que el consumidor ya puede consultar. Manipularlo da
`CursorInvalido` o una posición válida, nunca datos de otro equipo. Firmarlo con
HMAC exige gestionar un secreto, y eso queda fuera de M08 (ver §6).

### 4. Por qué es estable ante inserciones

La paginación es por **clave** (keyset), no por desplazamiento: cada página
empieza justo después de la última `SK` vista, y `SK` no cambia nunca. Para un
recorrido que empieza en el instante T0, con su `hasta` fijo:

| Situación durante el recorrido | Qué pasa |
| --- | --- |
| Lecturas que ya existían en T0 dentro de la ventana | Salen **exactamente una vez**: ni duplicadas ni saltadas |
| Lecturas nuevas con `medido_en > hasta` | No salen en este recorrido y **no desplazan** ninguna página |
| Lecturas atrasadas (`medido_en ≤ hasta`) que llegan durante el recorrido | Salen si caen por debajo del cursor; si caen por encima, no salen en este recorrido. En ningún caso duplican |
| Lectura escrita justo antes de pedir la primera página | Puede no estar todavía en el GSI (consistencia eventual, §6) |

**Línea base por desplazamiento**, para comparar: la página *n* de tamaño *k*
es `ultimas_lecturas(limite=n·k)[(n-1)·k:]`. Si entran *m* lecturas nuevas entre
la página *n* y la *n+1*, la página *n+1* repite *m* lecturas de la anterior.
Pedir la página *n* también cuesta *n·k* lecturas por métrica.

### 5. Benchmark contra línea base

**`scripts/bench_m08.py`** (`make bench`). También lo corre `make verify`, que
genera `artifacts/m08-pagination-performance.json`.

- **Carga:** 10 000 lecturas sintéticas, 5 equipos (`fixture-m08-00` a
  `fixture-m08-04`) × 5 métricas × 400, semilla fija 42, una por minuto con
  segundos al azar y `lectura_id` únicos. Se escriben con `put_lectura`, en una
  tabla aislada que se borra al terminar.
- **Variantes**, todas con `k = 100` sobre `fixture-m08-00`:

  | Id | Qué mide | Cómo |
  | --- | --- | --- |
  | `A1` | Línea base, primera página | `ultimas_lecturas(equipo, limite=100)`: 5 Query y mezcla |
  | `B1` | GSI2, primera página | `paginar_lecturas_equipo(equipo, limite=100)`: 1 Query |
  | `A10` | Línea base, página 10 por desplazamiento | `ultimas_lecturas(equipo, limite=1000)[900:]` |
  | `B10` | GSI2, página 10 con cursor | `paginar_lecturas_equipo(..., cursor=<cursor de la página 9>)` |

- **Muestra:** 5 corridas de calentamiento que no cuentan y **50 medidas por
  variante**, intercaladas (A1, B1, A10, B10, A1, …) para que la carga de la
  máquina afecte a todas por igual. Tiempo con `time.perf_counter_ns()` alrededor
  de la llamada completa, mezcla en el cliente incluida.
- **Se reporta por variante:** `n`, `p50_ms`, `p95_ms`, `min_ms`, `max_ms`,
  `media_ms`, `llamadas` (Query por página), `items_leidos` y `rcu` (suma de
  `ConsumedCapacity`). Los percentiles salen de
  `statistics.quantiles(muestras, n=100, method="inclusive")`: p50 es el
  índice 49 y p95 el 94.
- **Comprobación de resultado:** las 100 `SK` de `A1` y de `B1` son
  **idénticas y en el mismo orden**, y lo mismo `A10` contra `B10`
  (`resultados_iguales: true`). Si no coinciden, el benchmark falla: no se
  compara la velocidad de dos consultas que devuelven cosas distintas.
- **Costo de escritura:** WCU de `put_lectura` con `GSI2PK` contra un `PutItem`
  igual sin `GSI2PK`, con `ReturnConsumedCapacity="INDEXES"`. Si DynamoDB Local
  no informa la capacidad por índice, se reporta la calculada (1 WCU por KB en
  la tabla y otra en cada índice que recibe el item), con
  `"fuente": "calculada"`.
- **Entorno** (sección `entorno` del artifact): imagen de DynamoDB Local con su
  digest (de `docker-compose.yml`), versión de Python y de boto3,
  `platform.platform()`, procesador y número de núcleos, `docker version`, y
  si corrió en CI o en local.

**Regla para el ADR:** el artifact reporta lo que salga. **No se fija de
antemano que B gane.** Lo que sí se espera por construcción es
`llamadas: 5 → 1` en la primera página y que `A10` lea muchos más items que
`B10` (hasta 1 000 por métrica, contra 101). Si las latencias de B no mejoran, se escribe así en la sección de
resultados de este ADR y se explica; no se ajusta la medida.

### 6. Trade-off y límites

**Lo que se gana:**

- Una página es **1 Query** en lugar de 5, sin mezcla en el cliente.
- Pedir la página *n* cuesta lo mismo que pedir la primera; con desplazamiento,
  el costo crece con *n*.
- Hay cursor, y la paginación es estable ante inserciones (§4).

**Lo que se paga:**

- **Escritura:** cada lectura escribe también en GSI2. Con items de menos de
  1 KB pasa de 1 a **2 WCU por lectura**, el doble de costo de escritura.
- **Almacenamiento:** la proyección `ALL` duplica el espacio de las lecturas.
- **Consistencia:** los GSI son **eventualmente consistentes**. Q5 no admite
  `ConsistentRead`: una lectura recién escrita puede tardar en aparecer. Q1 con
  métrica sigue en la tabla base, con lectura fuerte, para quien la necesite.
- **Partición caliente:** todas las lecturas de un equipo, de las 5 métricas,
  caen en la misma partición de GSI2, cuando en la tabla se reparten en 5. Si un
  equipo pasara del límite de escritura de una partición (1 000 WCU/s), el
  throttling del GSI frenaría también las escrituras en la tabla. Con el ritmo
  del proyecto, una lectura por minuto y métrica, queda varios órdenes de
  magnitud por debajo. Si algún día hiciera falta, se reparte `GSI2PK` en
  shards como Q4, con el costo de volver a un fan-out.

**Límites:**

- **Las latencias de DynamoDB Local no son las de AWS**: no hay red, ni
  particiones, ni throttling. Los números sirven para comparar A contra B en la
  misma máquina y la misma corrida, no como cifra absoluta ni para extrapolar a
  producción.
- **50 medidas en una máquina:** el p95 sale de las 3 medidas más lentas y es
  sensible a cualquier pausa del sistema. Por eso las variantes van intercaladas
  y se reporta también `max_ms`.
- El cursor no va firmado (§3) ni caduca. Si la API lo expusiera a terceros,
  habría que firmarlo con HMAC y ponerle vigencia.
- `GET /lecturas` (PostgreSQL) sigue paginando por `LIMIT` sin cursor. Ya tiene
  el índice `lecturas_equipo_medido_idx`, y una paginación por clave
  `(medido_en, id)` queda como trabajo futuro, fuera de M08.
- Q2 y Q3 siguen en la tabla base, por métrica: no usan GSI2.

### 7. Pruebas y evidencia

**`tests/test_m08_indices.py`** (contrato de este ADR): GSI2 creado `ACTIVE`
con `GSI2PK`/`SK` y proyección `ALL`; Q5 declarada sobre GSI2; GSI2 devuelve las
lecturas de un equipo en el mismo orden que `SK` y deja fuera las alertas.

**`tests/test_m08_pagination.py`**, contra DynamoDB Local, con 1 000 lecturas
sintéticas de `fixture-m08-pag` (semilla fija) y páginas de 100:

| Caso | Criterio de M08 | Qué se espera |
| --- | --- | --- |
| Normal | paginación | 10 páginas; las 1 000 lecturas, cada una una vez y en `SK` descendente; la 10.ª sin cursor |
| Límite 1 | estable ante inserciones | se insertan 50 lecturas nuevas (`medido_en > hasta`) entre la página 3 y la 4: el recorrido devuelve las 1 000 originales, 0 duplicadas, 0 saltadas, 0 de las nuevas |
| Límite 2 | bordes | la última página sale exacta y con `cursor: None`; un equipo sin lecturas devuelve `items: []` sin cursor; `limite=1000` cabe en una página |
| Fallo declarado | contrato del cursor | cursor alterado, de otro equipo, con otra `v` o con `hasta` distinto da `CursorInvalido`, y DynamoDB no recibe ninguna Query (se comprueba con un doble de `table.query`) |
| Contraste | línea base | con el mismo escenario del límite 1, paginar por desplazamiento devuelve **50 duplicadas**; la cifra va al artifact |
| Relleno | backfill | 30 lecturas sin `GSI2PK`: la primera corrida actualiza 30 y Q5 las ve; la segunda actualiza 0 |

**Artifact** `artifacts/m08-pagination-performance.json`, generado por
`make verify`: `casos` (con `id`, `prueba`, `criterio`, `esperado`, `obtenido`,
`codigo_error` y `resultado`, como en M06 y M07), `paginacion` (`paginas`,
`lecturas_unicas`, `duplicadas`, `saltadas`, `insertadas_durante`,
`duplicadas_linea_base`), `benchmark` (§5) y `entorno`.

**Evidencia:** `evidence/m08-pagination-performance.json`, con `assignmentId`,
`commitSha`, `tag`, `commands`, `results`, `assumptions` y `limitations`,
registrada en `required_files` y en la revisión de campos de `verify_base.sh`.
gitleaks sigue dentro de `make verify`. **El `commitSha` de la evidencia tiene
que coincidir con el commit del tag `week-09-final`.**

## Alternativas descartadas

- **Paginar por desplazamiento sobre Q1.** No necesita índice nuevo, pero repite
  lecturas cuando entran nuevas y el costo crece con la profundidad. Es la línea
  base, no la solución.
- **Cursor compuesto sobre Q1, uno por métrica.** Paginación estable sin índice
  nuevo, pero cada página sigue costando 5 Query, más la mezcla en el cliente, y
  el cursor lleva 5 posiciones. Ataca la estabilidad y deja intacto el costo.
- **`GSI2SK = TS#{medido_en}#{metrica}#{lectura_id}`**, como decía el primer
  reparto. Agrega un segundo atributo que escribir y rellenar. Además, a igual
  instante, desempata por métrica y no por id, así que el orden ya no es el de
  Q1 y A y B no se pueden comparar lectura por lectura.
- **Proyección `KEYS_ONLY` o `INCLUDE`.** Ahorra almacenamiento, pero
  `KEYS_ONLY` obliga a un `BatchGetItem` por página, una segunda ronda que se
  come la ganancia. `INCLUDE` obliga a mantener la lista de campos de v1, v2 y
  v2 migrada que necesita `normalizar_lectura`.
- **Partir `GSI2PK` en shards desde ya.** Resuelve una partición caliente que hoy
  no existe y vuelve a meter el fan-out que este ADR quita.
- **Tabla materializada de «últimas N por equipo».** Una escritura extra por
  lectura, igual que el GSI, pero con la consistencia a cargo del código, y no
  pagina más allá de N.

## Consecuencias

- **Marco Antonio Osorio Hernandez:** `GSI2PK` en `put_lectura`; `ensure_table`
  con `UpdateTable` para los GSI faltantes; `paginar_lecturas_equipo` y
  `CursorInvalido` según §3; `scripts/backfill_m08.py` y `make backfill-gsi2`
  según §2. `ultimas_lecturas` no se toca: es la línea base.
- **Persona 3:** las pruebas de la tabla del §7, `scripts/bench_m08.py` y
  `make bench` según §5, el artifact, la evidence y su registro en
  `verify_base.sh`.
- `tests/test_m05_document_store.py` y `scripts/build_m05_artifact.py` ya no
  exigen exactamente Q1-Q4 sobre `TABLE`/`GSI1`. Exigen que Q1-Q4 existan y que
  toda consulta declarada sea Query sobre la tabla o sobre un GSI declarado en
  `indexes.json`.
- Después de cada `restaurar_lecturas` de M06 hay que correr
  `make backfill-gsi2`.

## Relacionado
- `docs/ADR-005-almacen-documental.md`
- `docs/ADR-007-evolucion-compatible.md`
- `db/nosql/indexes.json`
- `db/nosql/audit-table.json`
