# ADR-003 — Cuatro roles de base de datos y mínimo privilegio

## Estado
Aceptado.

## Contexto

Hasta M02 todo el proyecto hablaba con PostgreSQL usando `cdrl_dev`, el usuario
que crea la imagen `postgres:16-alpine`: superusuario y dueño de la base. Las
migraciones, la API, el seed y cualquier consulta manual compartían esa
identidad. Con ese diseño, un `DELETE` mal filtrado en un endpoint o una
inyección en cualquier punto de la aplicación tienen permiso para borrar tablas
enteras — las restricciones de M02 protegen la forma de los datos, no su
existencia.

M03 pide separar los cuatro oficios que ya existían mezclados —migración,
escritura, lectura y operación— y demostrar que cada uno puede hacer **solo** lo
declarado, manteniendo los secretos fuera del repositorio.

## Decisión

### Cuatro roles, uno por oficio (migración `0005_roles_privilegios.sql`)

| Rol | Puede | No puede |
| --- | --- | --- |
| `cdrl_migrator` | Es **dueño** del esquema: DDL, y DML sobre todas las tablas (seed y reconciliación) | Crear roles, cambiar contraseñas, tocar la base fuera de `public` |
| `cdrl_writer` | `SELECT` en `equipos`; `SELECT, INSERT` en `lecturas`; `USAGE` de su secuencia | `UPDATE`, `DELETE`, cualquier DDL, y **leer `umbrales` o `alertas`** |
| `cdrl_reader` | `SELECT` en `equipos`, `lecturas`, `umbrales`, `alertas` | Escribir cualquier cosa; leer `schema_migrations` |
| `cdrl_ops` | `SELECT` en `schema_migrations` y las vistas `pg_stat_*` vía `pg_monitor` | **Leer una sola fila de negocio** |

Tres decisiones dentro de ese reparto merecen justificación:

- **`cdrl_writer` no puede `UPDATE` ni `DELETE` sobre `lecturas`.** Una lectura
  es un hecho medido en un instante: se corrige emitiendo otra lectura, no
  editando la anterior. El privilegio que no existe no se puede abusar.
- **`cdrl_ops` no ve datos de negocio.** Operar es saber qué migraciones
  corrieron, qué consultas hay vivas y cómo se comporta el motor; nada de eso
  necesita el valor de una métrica. Es el rol que más manos tocan (quien
  diagnostica a las tres de la mañana) y el que menos debe poder filtrar.
- **`cdrl_reader` no lee `schema_migrations`.** El estado del esquema es
  información de operación. Mantener esa frontera hace que el reparto se pueda
  comprobar en las dos direcciones, no solo negando escrituras.

El DDL no se puede otorgar en PostgreSQL: `ALTER` y `DROP` son competencia del
dueño. Por eso la migración **traspasa la propiedad** de las cinco tablas y de
`severidad_alerta()` a `cdrl_migrator`; sin ese paso el rol podría crear tablas
nuevas pero no modificar las de M01 y M02.

### `REVOKE` antes que `GRANT`, y `PUBLIC` primero

`PUBLIC` no es «los usuarios anónimos»: es todo rol presente y futuro. Mientras
`PUBLIC` conserve `CONNECT` sobre la base y `USAGE` sobre `public`, los permisos
finos de abajo no limitan nada. La migración revoca primero en la base y en el
esquema, y solo después otorga lo mínimo a cada rol.

Los bloques de privilegios revocan también sobre los tres roles no
administrativos antes de otorgar. Eso hace la migración **convergente**, no solo
idempotente: un `GRANT` suelto que alguien haya hecho a mano desaparece al
reaplicarla, igual que el DML de M02 reconcilia alertas.

`ALTER DEFAULT PRIVILEGES` cierra el problema del olvido: toda tabla que cree
`cdrl_migrator` en adelante nace legible por `cdrl_reader`, y toda función nace
sin el `EXECUTE` que PostgreSQL regala a `PUBLIC`. La **escritura no se hereda**:
cada tabla escribible se otorga a mano en la migración que la crea.

### Quién aplica las migraciones

`scripts/apply_migrations.sh` antepone `SET ROLE cdrl_migrator;` a cada archivo,
de modo que el dueño de lo que se cree sea el rol y no quien abrió la conexión.
Sin eso, los `ALTER DEFAULT PRIVILEGES` no aplicarían a las tablas nuevas —
están declarados `FOR ROLE cdrl_migrator`— y la API se quedaría sin poder leer
una tabla futura, con un fallo que solo aparece en producción.

La `0005` es la excepción y lo resuelve ella misma: empieza con `RESET ROLE`
porque `CREATE ROLE` y `REVOKE ... ON DATABASE` exigen privilegios
administrativos que `cdrl_migrator` **no tiene a propósito**. En una base recién
creada el rol todavía no existe y el script lo detecta, así que las migraciones
hasta la `0005` corren con el usuario administrador, que es justo quien debe
crear roles.

### Secretos: ninguna contraseña versionada

Los roles se crean con `PASSWORD NULL`, que bajo `scram-sha-256` no autentica
ninguna conexión: quedan creados pero inservibles. Las contraseñas las inyecta
`scripts/rotate_db_passwords.sh` desde el entorno
(`CDRL_MIGRATOR_PASSWORD`, `CDRL_WRITER_PASSWORD`, `CDRL_READER_PASSWORD`,
`CDRL_OPS_PASSWORD`), nunca desde un archivo del repositorio.

**Rotar es volver a ejecutar ese mismo script** con otro valor en el entorno. No
hay que tocar un solo `GRANT`: los privilegios cuelgan del rol, no de la
contraseña, así que rotar es una operación de segundos y sin ventana de cambio
de esquema. El script manda el SQL **por stdin y no por la línea de comandos**,
porque `ps` y `docker inspect` muestran los argumentos de cualquier proceso, y
desactiva `log_statement` en la sesión para que la sentencia no acabe en el log
del servidor.

## Alternativas consideradas

- **Roles de grupo `NOLOGIN` más usuarios de login que los heredan.** Es el
  patrón que se usa cuando cada persona necesita su propia identidad auditable.
  Descartado por ahora: duplica ocho objetos para cuatro oficios y aquí quien se
  conecta es un servicio, no una persona. El cambio es aditivo si hace falta —
  los `GRANT` ya están sobre el rol correcto.
- **Dejar la propiedad en `cdrl_dev` y otorgar privilegios a `cdrl_migrator`.**
  Imposible sin trampas: el DDL no es un privilegio otorgable en PostgreSQL.
  Habría exigido `SECURITY DEFINER` envolviendo cada cambio de esquema.
- **Contraseñas en el `docker-compose.yml` con valores de laboratorio.** Es lo
  que hace el starter para `cdrl_dev`, y funciona mientras nadie confunda el
  archivo con producción. Descartado para los roles nuevos precisamente porque
  el hito evalúa la ausencia de secretos versionados: un valor «solo de
  laboratorio» en Git es el primer paso del hábito que acaba subiendo el real.
- **Calcular el verificador SCRAM en el cliente y enviar solo el hash.** Es
  estrictamente mejor —la contraseña en claro no llega a viajar en la sentencia—
  pero obliga a implementar PBKDF2-HMAC-SHA256 en el script. Anotado como deuda
  consciente; la mitigación actual (stdin, sin log) cubre los dos vectores
  realistas de este laboratorio.
- **Un rol único de solo lectura para operación y consulta.** Descartado: junta
  al que diagnostica con el que consulta datos y borra la única frontera que
  impide que una credencial de operación filtre telemetría.

## Consecuencias

- La separación **existe en la base pero todavía no se usa en la ruta de la
  aplicación**: `src/database.py` sigue conectando con `POSTGRES_USER`, que en
  el compose es `cdrl_dev`. Conectar cada operación con su rol es la parte de
  Persona 2 y es lo que convierte este ADR en una defensa real.
- **Contrato para Persona 2:** los `GET` deben usar `cdrl_reader` y el `POST
  /lecturas`, `cdrl_writer`. No es intercambiable: `cdrl_writer` **no puede leer
  `umbrales`**, así que `GET /lecturas/fuera-de-umbral` falla con `permission
  denied` si se sirve con el rol de escritura. Es deliberado —quien escribe no
  necesita el criterio de alerta— y la API ya tiene la ruta `503
  modelo_no_disponible` para degradar si se equivoca el rol.
- **Contrato para Persona 3:** las tres pruebas negativas mínimas del hito ya
  están comprobadas a mano (tabla de abajo) y son directamente automatizables;
  la tabla incluye seis más. Las credenciales de prueba deben salir del entorno,
  no del código de test.
- `cdrl_dev` sigue siendo superusuario y dueño de la base. No se puede eliminar:
  es el usuario de arranque de la imagen oficial, el equivalente al usuario
  maestro de una instancia RDS. Su papel queda reducido a lo administrativo —
  crear roles y rotar contraseñas.
- Una migración futura que cree una tabla escribible por la API debe otorgar el
  `INSERT` explícitamente. Si se olvida, la tabla nace legible pero no
  escribible y el fallo salta en la primera prueba, no en silencio.

## Verificación local (2026-09-18)

Base recreada desde cero (`docker compose down -v`), migraciones y seed
aplicados, y una conexión TCP por rol con `scram-sha-256`:

| Comprobación | Resultado |
| --- | --- |
| `apply_migrations.sh` con la `0005` incluida | aplica y reaplica; `INSERT 0 0`, `UPDATE 0`, `DELETE 0` en la segunda pasada |
| `cdrl_migrator` crea y borra una tabla | permitido |
| `cdrl_writer` lee `equipos` e inserta en `lecturas` | permitido (fila 602, borrada después) |
| `cdrl_reader` lee `lecturas` y `alertas` | permitido (300 y 77 filas) |
| `cdrl_ops` lee `schema_migrations` y `pg_stat_activity` | permitido |
| **`cdrl_reader` intenta `INSERT` en `lecturas`** | `permission denied for table lecturas` |
| **`cdrl_reader` intenta `UPDATE` en `umbrales`** | `permission denied for table umbrales` |
| **`cdrl_writer` intenta `DROP TABLE alertas`** | `must be owner of table alertas` |
| **`cdrl_writer` intenta `CREATE TABLE`** | `permission denied for schema public` |
| **`cdrl_writer` intenta `DELETE FROM lecturas`** | `permission denied for table lecturas` |
| **`cdrl_writer` intenta leer `umbrales`** | `permission denied for table umbrales` |
| **`cdrl_ops` intenta leer `lecturas`** | `permission denied for table lecturas` |
| **`cdrl_ops` intenta leer `equipos`** | `permission denied for table equipos` |
| **`cdrl_reader` intenta leer `schema_migrations`** | `permission denied for table schema_migrations` |
| `GET /lecturas` y `GET /lecturas/fuera-de-umbral` tras la migración | siguen respondiendo con datos |

## Relacionado
- `docs/ADR-001-stack-tecnologico.md`
- `docs/ADR-002-umbrales-y-alertas.md`
- `db/migrations/0005_roles_privilegios.sql`
- `scripts/rotate_db_passwords.sh`
