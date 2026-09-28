# ADR-004 — Almacén NoSQL para los eventos CDRL: DynamoDB con tabla única

## Estado

Propuesto. Pasa a **Aceptado** cuando `artifacts/nosql-matrix.json` confirme las
hipótesis H1–H4 de la sección *Evidencia*. Si alguna se refuta, la sección
*Alternativas descartadas* dice hacia dónde se mueve la decisión y por qué.

## Contexto

Hasta M03 todos los eventos del CDRL —las lecturas de telemetría y las alertas
derivadas— viven en PostgreSQL: `equipos`, `lecturas`, `umbrales` y `alertas`.
M04 no pide migrar nada: pide **decidir** qué familia NoSQL soportaría esos
eventos y justificar la elección contra los cuatro candidatos del enunciado
(document, graph, column y object store).

La decisión no se toma en abstracto. El repositorio ya fija el problema:

**Patrones de acceso reales** (`src/queries.py`, `docs/API-lecturas.md`):

| # | Consulta | Forma del acceso |
| --- | --- | --- |
| Q1 | `ULTIMAS_LECTURAS` | Últimas N lecturas de **un** equipo, filtro opcional por métrica y por ventana `medido_en`, orden descendente |
| Q2 | `RESUMEN_POR_METRICA` | Agregación (`COUNT`, `AVG`, `MIN`, `MAX`) por métrica sobre una ventana cerrada de un equipo |
| Q3 | `LECTURAS_FUERA_DE_UMBRAL` | Q1 más una comparación contra el umbral global de esa métrica |
| Q4 | `db/dml/0001_reconciliar_alertas.sql` | Alta idempotente, cierre y reapertura de alertas; **convergente**: reaplicar afecta 0 filas |

**Invariantes que el almacén tiene que poder sostener** (ADR-002):

- Vocabulario **cerrado** de cinco métricas (`cpu`, `memoria`, `disco_libre`,
  `latencia`, `tasa_error`). No es una lista que crezca sola: ampliarla es una
  migración nueva.
- Una lectura es un hecho medido en un instante: se corrige emitiendo otra, no
  editando la anterior (por eso `cdrl_writer` no tiene `UPDATE` ni `DELETE`
  sobre `lecturas`, ADR-003). El flujo de eventos es **append-only**.
- `UNIQUE (lectura_id, umbral_id)` en `alertas`: una lectura no puede violar dos
  veces el mismo umbral.

**Restricciones del entorno** (ADR-000): AWS Academy Learner Lab es el entorno
cloud oficial y Docker Compose el respaldo local; la imagen del motor NoSQL debe
quedar fijada por versión o digest. `docker-compose.yml` ya levanta
`amazon/dynamodb-local:3.3.1` por digest y `scripts/verify_base.sh` ya lo
arranca junto a PostgreSQL — hasta hoy sin que nada lo use.

### Una honestidad taxonómica previa

El enunciado nombra cuatro familias y pide comparar representantes concretos. Las
etiquetas no son limpias: DynamoDB se documenta a sí mismo como key-value **y**
documental, y su modelo de partición-más-rango es el que Cassandra y Bigtable
popularizaron como *wide-column*. Aquí se compara así, y se dice en voz alta:

| Familia | Representante evaluado | Por qué ese |
| --- | --- | --- |
| Column / partición + rango | **DynamoDB** (local 3.3.1, on-demand en Learner Lab) | Es el que el entorno oficial ofrece y el compose ya fija por digest |
| Document | **MongoDB 7** (DocumentDB como equivalente gestionado) | Pipeline de agregación propio, el rival real en Q2 |
| Graph | **Neo4j 5** (Neptune como equivalente gestionado) | Representante estándar de travesías |
| Object store | **S3 + Parquet + Athena** | Capa analítica barata, el rival real en histórico |

Comparar Cassandra *y* DynamoDB habría duplicado la misma respuesta de diseño
con dos operaciones distintas; se elige el que el entorno regala.

## Decisión

**DynamoDB, tabla única (`cdrl_eventos`), para los eventos calientes.**

### Claves

```
PK = "EQ#<codigo_equipo>#<metrica>"
SK = "TS#<medido_en ISO-8601 UTC>#<id_lectura>"
```

- **Q1 y Q3** se vuelven un `Query` sobre una sola partición con
  `ScanIndexForward=false` y `Limit=N`: el orden descendente por `medido_en` lo
  da el índice, no un `sort` posterior. El `id_lectura` en el `SK` rompe empates
  de dos lecturas del mismo milisegundo sin colisionar la clave.
- **La métrica va en la PK, no en el SK.** Es la decisión discutible del diseño,
  así que va su razón: con la métrica en el `SK` toda consulta por métrica
  leería la partición completa del equipo y filtraría después, pagando lecturas
  que se tiran. Con la métrica en la `PK`, el caso «todas las métricas» de Q1
  cuesta cinco `Query` en paralelo — y **cinco es un número cerrado**, garantizado
  por `lecturas_metrica_valida`. Se paga un fan-out fijo y conocido para no pagar
  lecturas desperdiciadas en el caso frecuente.
- **Append-only por construcción**: cada lectura es un item nuevo; no hay
  `UpdateItem` sobre lecturas. Coincide con el privilegio que `cdrl_writer` ya
  no tiene en PostgreSQL.

### Alertas en la misma tabla

```
PK = "ALERTA#<estado>#<shard 0..3>"     SK = "SEV#<severidad>#TS#<medido_en>"
```

- El alta idempotente de Q4 se traduce a `PutItem` con
  `ConditionExpression="attribute_not_exists(PK)"`, usando
  `LECTURA#<id>#UMBRAL#<id>` como atributo de identidad: es la misma invariante
  que `UNIQUE (lectura_id, umbral_id)`, expresada donde el motor la puede
  imponer.
- El `shard 0..3` no es adorno: sin él, *todas* las alertas abiertas caen en una
  única partición `ALERTA#abierta` y esa partición se calienta hasta el
  throttling. Cuatro shards reparten la escritura y el lector hace cuatro
  `Query` y mezcla. Es el caso límite que las pruebas de M04 tienen que provocar.

### Consistencia

Lecturas **eventualmente consistentes** para Q1/Q2/Q3, y consistencia fuerte solo
en la reevaluación de alertas. Se puede porque el DML de M02 es *convergente*, no
solo idempotente: si un lector ve un estado rancio, la siguiente pasada corrige y
reaplicar sobre una base ya reconciliada afecta 0 filas. Un almacén con
consistencia fuerte universal aquí sería pagar por una garantía que el diseño de
M02 ya no necesita.

### Lo que esta decisión NO resuelve

**Q2 es el punto débil y conviene decirlo antes que la rúbrica.** DynamoDB no
agrega en el servidor: `COUNT`, `AVG`, `MIN` y `MAX` sobre una ventana obligan a
leer los items de esa ventana y agregarlos en el cliente, con costo lineal en
número de items. Es aceptable mientras la ventana típica sea pequeña; deja de
serlo en el volumen que fija H2. Ahí es donde el object store entra como capa
fría, no antes.

## Matriz de decisión

Pesos asignados por lo que el proyecto ya demostró que hace: Q1/Q3 son la ruta
caliente y aparecen en los tres endpoints documentados, así que el ajuste al
patrón de acceso pesa más que cualquier otra cosa. Puntuación 1–5.

| # | Criterio | Peso | DynamoDB | Document (Mongo) | Graph (Neo4j) | Object (S3+Athena) | Evidencia |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C1 | Ajuste a Q1/Q3 (última N por equipo, orden temporal) | 30 | 5 | 4 | 2 | 1 | Q1/Q3 en `src/queries.py`; H1 |
| C2 | Ajuste a Q2 (agregación por ventana) | 15 | 2 | 5 | 2 | 5 | Q2 en `src/queries.py`; H2 |
| C3 | Escritura sostenida append-only y crecimiento de la serie | 15 | 5 | 4 | 2 | 3 | H3 |
| C4 | Invariantes de Q4 (alta idempotente, convergencia) | 10 | 4 | 4 | 3 | 1 | ADR-002 + `ConditionExpression`; H4 |
| C5 | Viabilidad en Learner Lab y reproducibilidad local | 15 | 5 | 3 | 2 | 4 | `docker-compose.yml` fija el digest; ADR-000 |
| C6 | Comportamiento ante fallos y recuperación | 10 | 4 | 3 | 3 | 4 | Prueba de fallo declarado de M04 |
| C7 | Costo de operación y encaje con `make verify` | 5 | 4 | 3 | 2 | 3 | `scripts/verify_base.sh` ya levanta el servicio |
| | **Total ponderado** | **100** | **4,30** | **3,85** | **2,20** | **2,75** | |

Cálculo de DynamoDB, para que se pueda auditar a mano:
`(5·30 + 2·15 + 5·15 + 4·10 + 5·15 + 4·10 + 4·5) / 100 = 430 / 100 = 4,30`.

El margen contra el document store es de 0,45 sobre 5 — **estrecho**. No se
presenta como una goleada: si H2 se refuta con números duros, C2 se lleva la
decisión al otro lado (ver *Alternativas descartadas*).

## Evidencia e hipótesis falsables

Cada criterio se apoya en algo del repositorio o en una hipótesis con su umbral
de refutación. Los números los produce la corrida de M04 en
`artifacts/nosql-matrix.json`; este ADR se cierra con ellos dentro.

- **H1 (C1).** Un `Query` de las últimas 100 lecturas de un equipo y una métrica
  responde en menos de **25 ms** p95 contra `dynamodb-local`, y su costo en
  unidades de lectura no crece con el tamaño de la partición.
  *Se refuta si* el p95 pasa de 25 ms o si el consumo escala con los items
  almacenados en vez de con los devueltos.
- **H2 (C2).** La ventana típica de agregación de Q2 —24 h de un equipo, cinco
  métricas— cabe por debajo de **5.000 items**, y agregarla en el cliente se
  mantiene bajo **150 ms** p95.
  *Se refuta si* la ventana real excede 5.000 items o el p95 pasa de 150 ms: con
  eso, C2 deja de ser un inconveniente y pasa a ser un defecto estructural.
- **H3 (C3).** La escritura de lecturas sostiene **≥ 500 items/s** en lotes de 25
  (`BatchWriteItem`) sin `ProvisionedThroughputExceededException` en la
  configuración local.
  *Se refuta si* aparece throttling por debajo de ese ritmo con la clave de
  partición propuesta.
- **H4 (C4).** El `PutItem` condicional rechaza el duplicado
  `(lectura_id, umbral_id)` con `ConditionalCheckFailedException` y **sin**
  escribir, de forma que reprocesar el mismo lote de alertas converge a 0
  cambios.
  *Se refuta si* un reprocesamiento produce una segunda alerta para el mismo par.

Las cuatro son comprobables con `dynamodb-local`, sin cuenta AWS. Las que
dependan de Learner Lab quedan fuera a propósito: el entorno no está garantizado
y el ADR no puede apoyarse en algo que el evaluador quizá no pueda reproducir.

## Alternativas descartadas

**Document store (MongoDB / DocumentDB) — la que más cerca quedó.** Gana C2 con
claridad: `$group` resuelve Q2 en el servidor y no obliga a traer la ventana al
cliente. Pierde en C5, que en este curso no es un detalle: el compose ya fija
`dynamodb-local` por digest como exige ADR-000, Learner Lab expone DynamoDB
directamente, y meter un segundo motor añade un servicio, un cliente y una ruta
de fallo a `make verify` sin cambiar ninguna respuesta de Q1/Q3.
**Se reabre si H2 se refuta**: con ventanas de agregación grandes, la agregación
en servidor deja de ser un lujo y Mongo pasa a ganar la matriz.

**Object store (S3 + Parquet + Athena).** Es la opción más barata por byte
almacenado y la mejor para el histórico: columnar, comprimido, ideal para Q2 a
escala de meses. Se descarta **para la ruta caliente**, no por costo sino por
latencia y granularidad: una consulta Athena responde en segundos y el objeto
mínimo útil agrupa muchas lecturas, así que Q1 («las últimas 100 de este
equipo») obligaría a leer y descartar. Queda anotada como **capa fría futura**:
volcar las particiones cerradas a Parquet y dejar en DynamoDB solo la ventana
caliente es la evolución natural si H2 aprieta. No entra en M04 porque duplicar
el almacén sin medir primero es diseñar a ciegas.

**Graph (Neo4j / Neptune).** Se descarta sin empate. El modelo del CDRL tiene una
relación 1:N (`equipos → lecturas`) y una comparación por métrica contra
`umbrales`; no hay travesías de profundidad variable, ni caminos más cortos, ni
componentes conexas — nada de lo que un grafo cobra su precio por resolver.
Elegirlo sería pagar ingesta más lenta y un modelo más caro de operar para
expresar dos `JOIN` que cualquiera de los otros tres hace mejor.
*Qué lo reabriría*: que el CDRL empezara a modelar dependencias entre equipos
(«si cae el switch, qué alertas son consecuencia y no causa»). Hoy eso no existe
en el esquema, ni en los endpoints, ni en el enunciado.

## Consecuencias

- Aparece `boto3` como dependencia y `src/events_store.py` como único punto de
  contacto con DynamoDB; el endpoint `DYNAMODB_ENDPOINT` llega por variable de
  entorno, nunca escrito en el código (ADR-003 y `docs/SECRETS.md` siguen
  vigentes: no se versiona ninguna credencial).
- PostgreSQL **no se retira**. M04 es una decisión de arquitectura sobre los
  eventos, no una migración: los cuatro roles, las restricciones y las consultas
  de M02/M03 siguen siendo la fuente de verdad relacional.
- El fan-out de cinco `Query` para «todas las métricas» es deuda aceptada y
  documentada. Si el vocabulario de métricas dejara de ser cerrado, esta decisión
  de clave se revisa antes que cualquier otra cosa de este ADR.
- La partición caliente de alertas queda mitigada con cuatro shards, no
  eliminada: es un límite conocido y las pruebas de M04 deben provocarlo.
- Q2 se resuelve en el cliente mientras H2 se sostenga. El día que no, la salida
  ya está escrita arriba y no hay que rediseñar de cero.
