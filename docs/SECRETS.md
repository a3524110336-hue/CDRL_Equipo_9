# M03: fuentes de credenciales y rotación

Esta guía documenta la configuración de la API de Marco y su integración con los roles de Jonathan. El modelo de privilegios está en [ADR-003](ADR-003-roles-y-minimo-privilegio.md) y en `db/migrations/0005_roles_privilegios.sql`. La API no crea roles, aplica migraciones ni cambia contraseñas.

## Qué credencial usa cada proceso

| Proceso u operación | Usuario de PostgreSQL | Fuente de contraseña |
| --- | --- | --- |
| API: `POST /lecturas` | `cdrl_writer`, fijo en el código | `CDRL_WRITER_PASSWORD` |
| API: los tres GET y `POST /lecturas/validar` | `cdrl_reader`, fijo en el código | `CDRL_READER_PASSWORD` |
| Herramientas que se conecten directamente como migrador | `cdrl_migrator` | `CDRL_MIGRATOR_PASSWORD` |
| Herramientas de diagnóstico | `cdrl_ops` | `CDRL_OPS_PASSWORD` |
| Arranque y administración de PostgreSQL | `POSTGRES_USER` | `POSTGRES_PASSWORD`, según el método de conexión |

La API utiliza `POSTGRES_HOST`, `POSTGRES_PORT` y `POSTGRES_DB` para localizar la base. No utiliza `POSTGRES_USER` ni `POSTGRES_PASSWORD` para autenticarse, ni cambia al administrador si falta una contraseña de rol. Los usuarios reader y writer no se eligen desde una solicitud HTTP.

Writer puede consultar `equipos` y consultar e insertar `lecturas`, con acceso a su secuencia. Reader puede consultar `equipos`, `lecturas`, `umbrales` y `alertas`. La validación sin guardar usa reader porque solo consulta. Migrator y ops quedan fuera de las conexiones de la API.

**El aplicador de migraciones tiene un contrato distinto del login directo como migrador.** `scripts/apply_migrations.sh` se conecta dentro del contenedor como `POSTGRES_USER` administrativo y usa `SET ROLE cdrl_migrator` cuando el rol ya existe. La migración 0005 ejecuta `RESET ROLE` para realizar sus operaciones administrativas. Ese script no consume `CDRL_MIGRATOR_PASSWORD` y no debe arrancarse sustituyendo el administrador por `cdrl_migrator`.

## Fuentes y almacenamiento

- `.env.example` versiona nombres de variables y valores no secretos. `POSTGRES_PASSWORD` y las cuatro variables `CDRL_*_PASSWORD` se dejan vacías; hay que proporcionar valores propios antes de usar los servicios correspondientes.
- El responsable genera contraseñas aleatorias e independientes por rol en su gestor de contraseñas y las proporciona a los consumidores indicados en la tabla. Los nombres de rol y la plantilla no sirven como contraseñas; los valores generados no se agregan a esta guía.
- En desarrollo se puede copiar la plantilla a `.env`, que está en `.gitignore`, y completar los valores con un editor local. Usar contraseñas diferentes por rol y limitar el acceso al archivo. No adjuntar `.env` a Classroom, capturas, evidencia ni commits.
- La API carga el `.env` de la raíz del repositorio sin sustituir variables que ya existen en el entorno del proceso. Un valor vacío exportado también tiene prioridad; corregirlo o retirarlo antes de reiniciar la API.
- En CI y despliegue, el responsable del entorno suministra las contraseñas desde su almacén de secretos. Inyectar en la API únicamente las claves reader y writer y los datos de conexión. Las credenciales administrativas, migrator y ops pertenecen a los procesos que las necesitan.

Un `.env` local compartido puede contener todas las claves del laboratorio. Eso facilita el trabajo local, pero no separa el acceso al archivo entre procesos: la API carga ese archivo completo aunque solo use dos claves para conectarse. En un despliegue no se debe montar ese archivo compartido ni pasarlo completo mediante `env_file` al servicio de la API.

## Primera preparación

1. Configurar la contraseña administrativa de PostgreSQL en la fuente local o de despliegue. Iniciar PostgreSQL y aplicar las migraciones y el seed de Jonathan. Los roles nuevos de la migración 0005 tienen `PASSWORD NULL`; con autenticación por contraseña todavía no pueden conectarse.
2. Proporcionar las contraseñas de los roles que se van a utilizar y ejecutar el rotador existente. Este script lee variables **exportadas en el entorno de Bash**; no lee ni exporta automáticamente el contenido de `.env`.
3. Suministrar las mismas claves reader y writer al proceso de la API y arrancar Uvicorn según [API-lecturas.md](API-lecturas.md). Configurar migrator y ops cuando sus herramientas responsables requieran login directo.

Desde una terminal Bash interactiva, en la raíz del repositorio, este ejemplo captura las dos contraseñas de la API sin mostrarlas ni incluirlas como literales en el comando:

```bash
read -r -s -p 'Contraseña para cdrl_writer: ' CDRL_WRITER_PASSWORD
export CDRL_WRITER_PASSWORD
read -r -s -p 'Contraseña para cdrl_reader: ' CDRL_READER_PASSWORD
export CDRL_READER_PASSWORD
bash scripts/rotate_db_passwords.sh writer reader
```

Los argumentos admitidos por el procedimiento son `migrator`, `writer`, `reader` y `ops`. Sin argumentos, el script intenta actualizar los cuatro y exige sus cuatro contraseñas. No ejecutar este procedimiento con `bash -x` ni publicar volcados de variables. Al terminar una sesión administrativa que ya no necesite las claves, retirarlas de su entorno con `unset` o cerrar esa terminal.

El rotador usa `docker compose exec -T postgres` y una conexión administrativa dentro del contenedor. `POSTGRES_USER` y `POSTGRES_DB` toman por defecto `cdrl_dev` y `cdrl`; si la instalación cambia esos nombres, exportar también los nombres correctos en la terminal administrativa. No reemplazar `POSTGRES_USER` por writer, reader, migrator u ops para ejecutar el rotador.

Reaplicar la migración 0005 no vuelve a vaciar las contraseñas: `PASSWORD NULL` se usa solo al crear un rol que no existía. El rotador es el mecanismo para asignar o cambiar las claves existentes.

## Integración pendiente del entorno de Alejandro

El `docker-compose.yml` recibido todavía pasa a `app` las credenciales administrativas y no inyecta las variables `CDRL_WRITER_PASSWORD` y `CDRL_READER_PASSWORD`. Añadir variables a `.env.example` no las introduce automáticamente en el contenedor.

Alejandro debe sustituir el bloque `environment` de **app** por este contenido mínimo; las variables administrativas del servicio **postgres** se gestionan por separado:

```yaml
environment:
  POSTGRES_DB: ${POSTGRES_DB:-cdrl}
  POSTGRES_HOST: postgres
  POSTGRES_PORT: "5432"
  CDRL_WRITER_PASSWORD: ${CDRL_WRITER_PASSWORD:?Define CDRL_WRITER_PASSWORD}
  CDRL_READER_PASSWORD: ${CDRL_READER_PASSWORD:?Define CDRL_READER_PASSWORD}
```

El arranque y CI también deben aplicar la migración 0005 y asignar las contraseñas antes de verificar la API. La coordinación de estos pasos con `make setup`, `make verify` y `make run`, las pruebas oficiales y la evidencia corresponde al entorno del equipo. Esta guía no declara completada esa integración con Docker o Make.

## Rotación

1. Preparar una contraseña nueva distinta para cada rol afectado en la fuente de secretos correspondiente. Coordinar una ventana de actualización: el cambio de contraseña y la actualización de los consumidores son pasos separados.
2. En la terminal administrativa, capturar y exportar la nueva clave mediante `read -r -s` como en el ejemplo. Ejecutar `bash scripts/rotate_db_passwords.sh writer` para writer, `reader` para reader, o la lista de roles necesaria. Comprobar el resultado del script; si falla, no asumir que todos los roles seleccionados se actualizaron.
3. Actualizar la fuente que utiliza cada consumidor con ese mismo valor. El script cambia PostgreSQL, pero no modifica `.env`, secretos de CI, variables de procesos existentes ni contenedores.
4. Para Uvicorn local, detener el proceso y volverlo a iniciar después de actualizar su entorno o `.env`. Las variables heredadas por la terminal tienen prioridad sobre `.env`: actualizar o retirar cualquier valor anterior. Para Docker, una vez integrada la inyección anterior, recrear el servicio con `docker compose up -d --force-recreate app`; un simple reinicio conserva el entorno del contenedor existente.
5. Verificar nuevas conexiones y las operaciones permitidas de cada rol, sin mostrar las contraseñas. Si se rota migrator u ops, actualizar y reiniciar sus consumidores responsables de la misma forma.

Cambiar una contraseña no revoca las sesiones de base de datos que ya estaban abiertas. Ante una credencial comprometida, el administrador debe revisar y cerrar las sesiones afectadas, además de rotar la clave. La API abre y cierra una conexión por operación; después de renovar su configuración, las nuevas operaciones usan el valor actualizado.

Este script rota los cuatro roles `cdrl_*`, no el usuario administrativo. Cambiar `POSTGRES_PASSWORD` en `.env` tampoco cambia por sí solo la contraseña de una base ya inicializada; ese cambio se coordina aparte con su administrador.

## Logs y comprobación de secretos

El rotador envía SQL por stdin y desactiva `log_statement` y `log_min_duration_statement` en su sesión. Esto reduce la exposición, pero no garantiza ausencia absoluta de contraseñas en logs: no cambia `log_min_error_statement`, que puede registrar una sentencia fallida. Evitar compartir registros de una rotación fallida sin revisarlos. Véase la [documentación de logging de PostgreSQL 16](https://www.postgresql.org/docs/16/runtime-config-logging.html).

La API devuelve mensajes genéricos: si la clave del rol solicitado falta o está vacía, responde 503 con `configuracion_no_disponible`; si falla la autenticación o la conexión, responde 503 con `base_no_disponible`. No devuelve la contraseña ni la excepción original al cliente.

La ejecución del detector de secretos, la revisión del historial y la evidencia automatizada corresponden a Alejandro. Este documento no constituye un resultado de gitleaks ni certifica que el historial esté libre de secretos.
