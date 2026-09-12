-- 0003_umbrales.sql — umbral operativo global por métrica.
--
-- Contrato acordado con Persona 2 (docs/API-lecturas.md): la tabla se llama
-- `umbrales`, expone `metrica`, `minimo` y `maximo`, y `metrica` es única para
-- que el JOIN de LECTURAS_FUERA_DE_UMBRAL no multiplique filas de `lecturas`.
--
-- IF NOT EXISTS es idempotente, no convergente. Si la tabla ya existe con otra
-- forma, esta migración no la corrige ni avisa. Un cambio de esquema pide una
-- migración NUEVA, nunca editar esta.

BEGIN;

CREATE TABLE IF NOT EXISTS umbrales (
    id        SERIAL           PRIMARY KEY,
    metrica   TEXT             NOT NULL UNIQUE,
    minimo    DOUBLE PRECISION NOT NULL,
    maximo    DOUBLE PRECISION NOT NULL,
    creado_en TIMESTAMPTZ      NOT NULL DEFAULT now(),

    -- El mismo dominio cerrado que lecturas_metrica_valida: un umbral para una
    -- métrica que `lecturas` no puede almacenar sería letra muerta.
    CONSTRAINT umbrales_metrica_valida CHECK (
        metrica IN ('cpu', 'memoria', 'disco_libre', 'latencia', 'tasa_error')
    ),

    -- `valor` en lecturas es DOUBLE PRECISION y admite NaN/Infinity a nivel de
    -- tipo; el umbral los rechaza para que la comparación siempre tenga sentido.
    -- Con NaN en un extremo, `valor < minimo` y `valor > maximo` son ambos
    -- falsos y la violación se volvería invisible.
    CONSTRAINT umbrales_extremos_finitos CHECK (
        minimo <> 'NaN'::double precision
        AND minimo <> 'Infinity'::double precision
        AND minimo <> '-Infinity'::double precision
        AND maximo <> 'NaN'::double precision
        AND maximo <> 'Infinity'::double precision
        AND maximo <> '-Infinity'::double precision
    ),

    CONSTRAINT umbrales_rango_ordenado CHECK (minimo <= maximo)
);

INSERT INTO schema_migrations (version)
VALUES ('0003_umbrales')
ON CONFLICT (version) DO NOTHING;

COMMIT;
