# ADR-002 — Umbrales globales por métrica y alertas derivadas

## Estado
Aceptado.

## Contexto
M02 pide cerrar el modelo relacional operativo del CDRL. Sobre `equipos` y
`lecturas` (M01) falta el criterio que separa una lectura normal de una anómala,
y el registro de las anomalías detectadas.

Persona 2 ya había escrito `src/queries.py` y `docs/API-lecturas.md` contra un
contrato provisional, antes de que existieran estas migraciones:

> `umbrales(metrica, minimo, maximo)`: un umbral global por métrica
> (`metrica UNIQUE`), extremos numéricos finitos no nulos y `minimo <= maximo`.

El DDL de este ADR respeta ese contrato al pie de la letra. Es una restricción de
diseño, no una elección: cambiar los nombres obligaría a reescribir
`LECTURAS_FUERA_DE_UMBRAL` y su documentación a un día del cierre.

## Decisión

### `umbrales` (migración 0003)
Un umbral **global por métrica**, no por equipo: `metrica TEXT NOT NULL UNIQUE`.
Invariantes en el esquema, no en la aplicación:

- `umbrales_metrica_valida` — el mismo dominio cerrado de cinco métricas que
  `lecturas_metrica_valida`. Un umbral para una métrica que `lecturas` no puede
  almacenar sería letra muerta.
- `umbrales_extremos_finitos` — `minimo` y `maximo` rechazan `NaN` e
  `Infinity`. `DOUBLE PRECISION` los admite a nivel de tipo, y con `NaN` en un
  extremo tanto `valor < minimo` como `valor > maximo` son falsos: la violación
  se volvería invisible en lugar de dar error.
- `umbrales_rango_ordenado` — `minimo <= maximo`. Se permite la igualdad: un
  umbral de ancho cero es un valor exacto exigido, no un error.

### `alertas` (migración 0004)
Una fila por lectura que sale de su umbral. Es la **única tabla escribible** que
aporta M02; `LECTURAS_FUERA_DE_UMBRAL` es de solo lectura y no pasa por aquí.

- `UNIQUE (lectura_id, umbral_id)` — hace idempotente el alta del DML: una
  lectura solo puede violar una vez el mismo umbral.
- `alertas_cierre_coherente` — `(estado = 'cerrada') = (cerrada_en IS NOT NULL)`.
  Los dos campos se ponen y se quitan juntos; cualquier otra combinación
  describe una alerta que nadie puede interpretar.
- `alertas_cierre_posterior`, `alertas_valor_finito` y dominios cerrados en
  `tipo`, `severidad` y `estado`.
- `ON DELETE CASCADE` hacia `lecturas` y `umbrales`: una alerta no tiene
  significado sin el par que la originó.

La regla de severidad vive en la función `severidad_alerta(valor, minimo,
maximo)`, no en el DML: la necesitan dos sentencias (alta y reevaluación) y una
copia en cada una acabaría desincronizada. Una desviación pasa de `advertencia`
a `critica` cuando excede el **10 % del ancho del umbral**.

### DML de reconciliación (`db/dml/0001_reconciliar_alertas.sql`)
Tres sentencias **convergentes**, no solo idempotentes: reaplicar el archivo
sobre una base ya reconciliada afecta 0 filas.

1. `DELETE` — la lectura ya no viola su umbral (alguien lo ensanchó).
2. `INSERT` — alta de las violaciones nuevas, con `ON CONFLICT DO NOTHING`.
3. `UPDATE` — reevalúa `tipo` y `severidad` de las alertas abiertas contra el
   umbral vigente. Las cerradas no se tocan: son historial.

`detectada_en` toma `lecturas.registrado_en` —el instante en que el sistema supo
del valor— y no `now()`, para que reaplicar el DML no mueva la fecha.

### Seed (`db/seed/0002_umbrales.sql`)
Determinista, sin `now()` ni `random()`. Los extremos se eligieron contra los
rangos reales que produce `0001_datos_sinteticos.sql` para que cada métrica
registrada genere lecturas **dentro y fuera** del umbral; si todas cayeran
dentro, el endpoint de violaciones devolvería siempre vacío y no habría nada que
demostrar.

`disco_libre` queda **a propósito sin umbral**: el seed lo mide en bytes
absolutos y el modelo no guarda la capacidad total de cada equipo, así que un
único mínimo global en bytes sería falso para equipos de distinto tamaño. El
hueco además deja observable la ruta «métrica sin umbral registrado», que
`docs/API-lecturas.md` define como «no produce violaciones».

## Alternativas consideradas

- **Umbral por equipo y métrica** (`UNIQUE (equipo_id, metrica)`): más realista
  para flotas heterogéneas, pero rompe el `JOIN u.metrica = l.metrica` que
  Persona 2 ya tenía escrito y documentado. Descartado por el contrato, no por
  el modelo; queda como candidato natural para M03.
- **Calcular las violaciones al vuelo, sin tabla `alertas`**: la consulta de
  Persona 2 ya lo hace. Descartado porque una violación necesita ciclo de vida
  —reconocerla, cerrarla— y una vista no lo puede guardar.
- **`minimo` y `maximo` opcionales (`NULL` = sin cota por ese lado)**: más
  expresivo, pero obliga a `IS NULL` en cada comparación y el contrato de
  Persona 2 los declara no nulos. Se consigue lo mismo con `minimo = 0`.
- **Regla de severidad como columna calculada o trigger**: descartado; una
  función `IMMUTABLE` es reutilizable desde el DML y desde cualquier consulta
  sin esconder escrituras.

## Consecuencias

- `LECTURAS_FUERA_DE_UMBRAL` funciona **sin modificar `src/queries.py`**. La
  dependencia de integración pendiente que declara `docs/API-lecturas.md`
  (sección final) queda resuelta: los nombres y restricciones coinciden.
- El camino `503 modelo_no_disponible` de `lecturas_fuera_de_umbral()` deja de
  activarse una vez aplicada la migración 0003. Sigue siendo correcto como
  defensa si alguien corre la API contra una base sin migrar.
- `scripts/apply_migrations.sh` gana una etapa `db/dml/` y, sobre todo,
  `-v ON_ERROR_STOP=1`. Sin esa bandera `psql` informaba el error, seguía con la
  sentencia siguiente y terminaba en 0: una migración rota pasaba el verify en
  silencio.
- Un cambio de umbral no reescribe el historial. Reaplicar el DML reconcilia las
  alertas abiertas y borra las que dejaron de aplicar, pero las cerradas quedan
  como registro de lo que se detectó con el umbral de entonces.
- `scripts/check_constraints.sql` cubre las 15 invariantes de `umbrales` y
  `alertas`. Las pruebas de `tests/` entran por la API y la API solo escribe en
  `lecturas`, así que desde ahí esas invariantes son inalcanzables.

## Verificación local (2026-09-12)

Contra `postgres:16-alpine`, base recreada desde cero con `docker compose down -v`:

| Comprobación | Resultado |
| --- | --- |
| `apply_migrations.sh` sobre base limpia | 5 equipos, 300 lecturas, 4 umbrales, **77 alertas** |
| Segunda corrida del pipeline completo | `INSERT 0 0`, `UPDATE 0`, `DELETE 0` en todo — convergente |
| Cruces `tipo` × `severidad` presentes | los 4 (`bajo_minimo`/`sobre_maximo` × `advertencia`/`critica`) |
| `check_constraints.sql` | 15/15 rechazadas (`23514` CHECK, `23502` NOT NULL, `23505` UNIQUE, `23503` FK) |
| `GET /lecturas/fuera-de-umbral?equipo=edge-01&metrica=cpu` | devuelve filas con `umbral_minimo` 25.0 y `umbral_maximo` 60.0 |
| `GET /lecturas/fuera-de-umbral?...&metrica=disco_libre` | `200` + `[]` — métrica sin umbral, sin violaciones |

## Relacionado
- `docs/ADR-001-stack-tecnologico.md`
- `docs/API-lecturas.md`, sección «Dependencia de integración pendiente»
