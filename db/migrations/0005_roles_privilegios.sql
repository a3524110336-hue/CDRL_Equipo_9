-- 0005_roles_privilegios.sql — cuatro roles con el privilegio mínimo de su oficio.
--
-- M03 pide separar migración, escritura, lectura y operación. Hasta M02 todo
-- corría con un único usuario dueño de la base: cualquier error de la API podía
-- borrar una tabla.
--
-- ESTA MIGRACIÓN ES ADMINISTRATIVA. `CREATE ROLE` y `REVOKE ... ON DATABASE`
-- piden un rol con CREATEROLE y propiedad de la base, que `cdrl_migrator` no
-- tiene a propósito. Por eso empieza con RESET ROLE: si el aplicador ya entró
-- con `SET ROLE cdrl_migrator` (ver scripts/apply_migrations.sh), aquí vuelve al
-- usuario administrador de la conexión.
--
-- NO CONTIENE NINGUNA CONTRASEÑA. Los roles nacen con PASSWORD NULL, que en
-- scram-sha-256 no autentica ninguna conexión: quedan creados pero inservibles
-- hasta que scripts/rotate_db_passwords.sh les inyecta una contraseña desde el
-- entorno. Ese mismo script es el procedimiento de rotación (docs/SECRETS.md).
--
-- IDEMPOTENTE y además CONVERGENTE en los privilegios: cada bloque revoca antes
-- de otorgar, así que un GRANT suelto hecho a mano se deshace al reaplicar.

BEGIN;

RESET ROLE;

-- ---------------------------------------------------------------------------
-- 1. Los cuatro roles
-- ---------------------------------------------------------------------------
-- CREATE ROLE no admite IF NOT EXISTS, de ahí el bloque condicional.
DO $$
DECLARE
    rol TEXT;
BEGIN
    FOREACH rol IN ARRAY ARRAY['cdrl_migrator', 'cdrl_writer', 'cdrl_reader', 'cdrl_ops'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rol) THEN
            EXECUTE format('CREATE ROLE %I LOGIN PASSWORD NULL', rol);
        END IF;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 2. La base y el esquema dejan de ser de todos
-- ---------------------------------------------------------------------------
-- PUBLIC es todo rol presente y futuro. Sin este REVOKE, cualquier rol nuevo
-- nacería pudiendo conectarse a la base y crear objetos en `public`, y los
-- GRANTs de abajo no limitarían nada.
DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO cdrl_migrator, cdrl_writer, cdrl_reader, cdrl_ops',
        current_database()
    );
END $$;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO cdrl_migrator, cdrl_writer, cdrl_reader, cdrl_ops;

-- Crear y borrar objetos es competencia exclusiva del rol de migración.
GRANT CREATE ON SCHEMA public TO cdrl_migrator;

-- ---------------------------------------------------------------------------
-- 3. El esquema pasa a ser de cdrl_migrator
-- ---------------------------------------------------------------------------
-- El DDL en PostgreSQL no se puede otorgar: ALTER y DROP son del dueño. Sin
-- este traspaso, `cdrl_migrator` podría crear tablas nuevas pero no tocar las de
-- M01 y M02, y las migraciones siguientes fallarían.
--
-- Cambiar el dueño de una tabla arrastra el de sus secuencias SERIAL.
ALTER TABLE schema_migrations OWNER TO cdrl_migrator;
ALTER TABLE equipos           OWNER TO cdrl_migrator;
ALTER TABLE lecturas          OWNER TO cdrl_migrator;
ALTER TABLE umbrales          OWNER TO cdrl_migrator;
ALTER TABLE alertas           OWNER TO cdrl_migrator;

ALTER FUNCTION severidad_alerta(DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION)
    OWNER TO cdrl_migrator;

-- ---------------------------------------------------------------------------
-- 4. Punto de partida: los tres roles no administrativos no tienen nada
-- ---------------------------------------------------------------------------
-- Lo que hace convergente a esta migración. Todo privilegio concedido a mano
-- fuera de este archivo desaparece al reaplicarlo.
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM cdrl_writer, cdrl_reader, cdrl_ops;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM cdrl_writer, cdrl_reader, cdrl_ops;
REVOKE ALL ON FUNCTION severidad_alerta(DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION)
    FROM PUBLIC, cdrl_writer, cdrl_reader, cdrl_ops;

-- ---------------------------------------------------------------------------
-- 5. cdrl_writer — la ruta de escritura de la API
-- ---------------------------------------------------------------------------
-- POST /lecturas y nada más: resolver el equipo, detectar el duplicado, insertar
-- y devolver la fila insertada. Sin UPDATE ni DELETE: una lectura es un hecho
-- medido, se corrige con otra lectura, no editándola.
GRANT SELECT          ON equipos  TO cdrl_writer;
GRANT SELECT, INSERT  ON lecturas TO cdrl_writer;

-- INSERT en una columna SERIAL necesita la secuencia, que es un objeto aparte.
DO $$
DECLARE
    secuencia TEXT := pg_get_serial_sequence('lecturas', 'id');
BEGIN
    IF secuencia IS NULL THEN
        RAISE EXCEPTION 'lecturas.id no tiene secuencia asociada';
    END IF;
    EXECUTE format('GRANT USAGE ON SEQUENCE %s TO cdrl_writer', secuencia);
END $$;

-- ---------------------------------------------------------------------------
-- 6. cdrl_reader — los GET, solo lectura
-- ---------------------------------------------------------------------------
-- Las cuatro tablas de negocio. `schema_migrations` queda fuera: el estado del
-- esquema es información de operación, no del dominio.
GRANT SELECT ON equipos, lecturas, umbrales, alertas TO cdrl_reader;

-- ---------------------------------------------------------------------------
-- 7. cdrl_ops — operar sin ver los datos
-- ---------------------------------------------------------------------------
-- Diagnostica: qué migraciones corrieron, qué consultas hay vivas, cómo va la
-- caché. Ni una fila de negocio. pg_monitor es un rol predefinido de PostgreSQL
-- y da las vistas pg_stat_* completas sin necesidad de superusuario.
GRANT SELECT ON schema_migrations TO cdrl_ops;
GRANT pg_monitor TO cdrl_ops;

-- ---------------------------------------------------------------------------
-- 8. Lo que se cree de aquí en adelante
-- ---------------------------------------------------------------------------
-- Sin esto, cada migración futura tendría que acordarse de repetir los GRANTs;
-- el día que se olvide, la API se queda ciega en producción y nadie lo ve hasta
-- que falla. Aplica solo a lo que cree cdrl_migrator, que es quien migra.
ALTER DEFAULT PRIVILEGES FOR ROLE cdrl_migrator IN SCHEMA public
    GRANT SELECT ON TABLES TO cdrl_reader;

-- La escritura no se hereda: cada tabla escribible se otorga a mano, en la
-- migración que la crea y con las columnas que toque.
ALTER DEFAULT PRIVILEGES FOR ROLE cdrl_migrator IN SCHEMA public
    REVOKE ALL ON TABLES FROM cdrl_writer, cdrl_ops;

-- PostgreSQL otorga EXECUTE a PUBLIC en toda función nueva. Es el único
-- privilegio que hay que quitar activamente.
ALTER DEFAULT PRIVILEGES FOR ROLE cdrl_migrator IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

INSERT INTO schema_migrations (version)
VALUES ('0005_roles_privilegios')
ON CONFLICT (version) DO NOTHING;

COMMIT;
