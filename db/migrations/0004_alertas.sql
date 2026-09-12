-- 0004_alertas.sql — alertas derivadas de lecturas que salen de su umbral.
--
-- `alertas` es la única tabla escribible de M02. La consulta
-- LECTURAS_FUERA_DE_UMBRAL de Persona 2 es de solo lectura y no toca esta
-- tabla: quien la puebla es el DML de db/dml/.
--
-- IF NOT EXISTS es idempotente, no convergente. Si la tabla ya existe con otra
-- forma, esta migración no la corrige ni avisa. Un cambio de esquema pide una
-- migración NUEVA, nunca editar esta.

BEGIN;

CREATE TABLE IF NOT EXISTS alertas (
    id              BIGSERIAL        PRIMARY KEY,
    lectura_id      BIGINT           NOT NULL REFERENCES lecturas(id)  ON DELETE CASCADE,
    umbral_id       INTEGER          NOT NULL REFERENCES umbrales(id)  ON DELETE CASCADE,
    tipo            TEXT             NOT NULL,
    severidad       TEXT             NOT NULL,
    valor_observado DOUBLE PRECISION NOT NULL,
    estado          TEXT             NOT NULL DEFAULT 'abierta',
    detectada_en    TIMESTAMPTZ      NOT NULL,
    cerrada_en      TIMESTAMPTZ,

    CONSTRAINT alertas_tipo_valido CHECK (
        tipo IN ('bajo_minimo', 'sobre_maximo')
    ),

    CONSTRAINT alertas_severidad_valida CHECK (
        severidad IN ('advertencia', 'critica')
    ),

    CONSTRAINT alertas_estado_valido CHECK (
        estado IN ('abierta', 'reconocida', 'cerrada')
    ),

    CONSTRAINT alertas_valor_finito CHECK (
        valor_observado <> 'NaN'::double precision
        AND valor_observado <> 'Infinity'::double precision
        AND valor_observado <> '-Infinity'::double precision
    ),

    -- `cerrada_en` y el estado 'cerrada' se ponen o se quitan juntos; cualquier
    -- otra combinación describe una alerta que nadie puede interpretar.
    CONSTRAINT alertas_cierre_coherente CHECK (
        (estado = 'cerrada') = (cerrada_en IS NOT NULL)
    ),

    CONSTRAINT alertas_cierre_posterior CHECK (
        cerrada_en IS NULL OR cerrada_en >= detectada_en
    ),

    -- Hace idempotente al INSERT del DML: una lectura solo puede violar una vez
    -- el mismo umbral, así que reaplicar el DML no duplica alertas.
    CONSTRAINT alertas_sin_duplicados UNIQUE (lectura_id, umbral_id)
);

CREATE INDEX IF NOT EXISTS alertas_estado_detectada_idx
    ON alertas (estado, detectada_en DESC);

-- La regla de severidad vive aquí porque el DML la necesita en dos sentencias
-- (alta y reevaluación) y una copia en cada una se desincronizaría.
-- Criterio: la desviación pasa de 'advertencia' a 'critica' cuando excede el
-- 10 % del ancho del umbral. Con un umbral de ancho cero, cualquier desviación
-- es crítica.
CREATE OR REPLACE FUNCTION severidad_alerta(
    valor  DOUBLE PRECISION,
    minimo DOUBLE PRECISION,
    maximo DOUBLE PRECISION
) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE
        WHEN valor < minimo AND (minimo - valor) > 0.10 * (maximo - minimo) THEN 'critica'
        WHEN valor > maximo AND (valor - maximo) > 0.10 * (maximo - minimo) THEN 'critica'
        ELSE 'advertencia'
    END
$$;

INSERT INTO schema_migrations (version)
VALUES ('0004_alertas')
ON CONFLICT (version) DO NOTHING;

COMMIT;
