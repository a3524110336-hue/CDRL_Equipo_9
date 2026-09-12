-- check_constraints.sql — comprueba que el esquema M02 RECHAZA datos inválidos.
--
-- El enunciado exige que las restricciones rechacen datos inválidos. Las pruebas
-- de tests/ entran por la API y la API solo escribe en `lecturas`, así que las
-- invariantes de `umbrales` y `alertas` no se pueden ejercitar desde ahí.
--
-- Cada caso corre en su propio subbloque: si el INSERT falla, la restricción
-- hizo su trabajo; si pasa, es un agujero y el script aborta con error.
--
--   docker compose exec -T postgres psql -v ON_ERROR_STOP=1 \
--     -U cdrl_dev -d cdrl -f - < scripts/check_constraints.sql

DO $$
DECLARE
    caso    RECORD;
    fallos  TEXT[] := '{}';
    lectura BIGINT;
    umbral  INTEGER;
BEGIN
    SELECT id INTO lectura FROM lecturas ORDER BY id LIMIT 1;
    SELECT id INTO umbral  FROM umbrales ORDER BY id LIMIT 1;
    IF lectura IS NULL OR umbral IS NULL THEN
        RAISE EXCEPTION 'aplica las migraciones, el seed y el DML antes de correr esto';
    END IF;

    FOR caso IN
        SELECT * FROM (VALUES
            ('umbrales: metrica fuera del dominio',
             $q$INSERT INTO umbrales (metrica, minimo, maximo) VALUES ('temperatura', 0, 1)$q$),
            ('umbrales: minimo > maximo',
             $q$INSERT INTO umbrales (metrica, minimo, maximo) VALUES ('disco_libre', 10, 5)$q$),
            ('umbrales: minimo NaN',
             $q$INSERT INTO umbrales (metrica, minimo, maximo) VALUES ('disco_libre', 'NaN', 5)$q$),
            ('umbrales: maximo Infinity',
             $q$INSERT INTO umbrales (metrica, minimo, maximo) VALUES ('disco_libre', 0, 'Infinity')$q$),
            ('umbrales: minimo nulo',
             $q$INSERT INTO umbrales (metrica, minimo, maximo) VALUES ('disco_libre', NULL, 5)$q$),
            ('umbrales: metrica duplicada',
             $q$INSERT INTO umbrales (metrica, minimo, maximo) VALUES ('cpu', 0, 1)$q$),

            ('alertas: tipo fuera del dominio',
             $q$INSERT INTO alertas (lectura_id, umbral_id, tipo, severidad, valor_observado, detectada_en)
                SELECT %L, %L, 'desviada', 'critica', 1, now()$q$),
            ('alertas: severidad fuera del dominio',
             $q$INSERT INTO alertas (lectura_id, umbral_id, tipo, severidad, valor_observado, detectada_en)
                SELECT %L, %L, 'sobre_maximo', 'urgente', 1, now()$q$),
            ('alertas: estado fuera del dominio',
             $q$INSERT INTO alertas (lectura_id, umbral_id, tipo, severidad, valor_observado, detectada_en, estado)
                SELECT %L, %L, 'sobre_maximo', 'critica', 1, now(), 'pendiente'$q$),
            ('alertas: cerrada sin cerrada_en',
             $q$INSERT INTO alertas (lectura_id, umbral_id, tipo, severidad, valor_observado, detectada_en, estado)
                SELECT %L, %L, 'sobre_maximo', 'critica', 1, now(), 'cerrada'$q$),
            ('alertas: abierta con cerrada_en',
             $q$INSERT INTO alertas (lectura_id, umbral_id, tipo, severidad, valor_observado, detectada_en, cerrada_en)
                SELECT %L, %L, 'sobre_maximo', 'critica', 1, now(), now()$q$),
            ('alertas: cierre anterior a la deteccion',
             $q$INSERT INTO alertas (lectura_id, umbral_id, tipo, severidad, valor_observado, detectada_en, estado, cerrada_en)
                SELECT %L, %L, 'sobre_maximo', 'critica', 1, now(), 'cerrada', now() - INTERVAL '1 hour'$q$),
            ('alertas: valor observado NaN',
             $q$INSERT INTO alertas (lectura_id, umbral_id, tipo, severidad, valor_observado, detectada_en)
                SELECT %L, %L, 'sobre_maximo', 'critica', 'NaN', now()$q$),
            ('alertas: lectura inexistente',
             $q$INSERT INTO alertas (lectura_id, umbral_id, tipo, severidad, valor_observado, detectada_en)
                SELECT 999999999, %2$L, 'sobre_maximo', 'critica', 1, now()$q$),
            ('alertas: par (lectura, umbral) duplicado',
             $q$INSERT INTO alertas (lectura_id, umbral_id, tipo, severidad, valor_observado, detectada_en)
                SELECT a.lectura_id, a.umbral_id, a.tipo, a.severidad, a.valor_observado, a.detectada_en
                FROM alertas AS a ORDER BY a.id LIMIT 1$q$)
        ) AS t(nombre, sentencia)
    LOOP
        BEGIN
            EXECUTE format(caso.sentencia, lectura, umbral);
            fallos := fallos || caso.nombre;
            RAISE WARNING 'NO RECHAZADO -> %', caso.nombre;
        EXCEPTION WHEN others THEN
            RAISE NOTICE 'rechazado (%) -> %', SQLSTATE, caso.nombre;
        END;
    END LOOP;

    IF array_length(fallos, 1) > 0 THEN
        RAISE EXCEPTION 'restricciones que no rechazaron: %', array_to_string(fallos, '; ');
    END IF;

    RAISE NOTICE 'M02: las 15 restricciones rechazaron sus datos invalidos.';
END
$$;
