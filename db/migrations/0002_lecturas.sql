-- 0002_lecturas
--
-- Serie temporal de mediciones: una fila por lectura de un equipo.
-- Depende de 0001_equipos.
--
-- IDEMPOTENTE: igual que 0001, se puede aplicar N veces.

BEGIN;

CREATE TABLE IF NOT EXISTS lecturas (
    id            BIGSERIAL        PRIMARY KEY,
    equipo_id     INTEGER          NOT NULL REFERENCES equipos(id) ON DELETE CASCADE,
    metrica       TEXT             NOT NULL,
    unidad        TEXT             NOT NULL,
    valor         DOUBLE PRECISION NOT NULL,
    medido_en     TIMESTAMPTZ      NOT NULL,
    registrado_en TIMESTAMPTZ      NOT NULL DEFAULT now(),

    -- Vocabulario cerrado: una métrica fuera de esta lista es un error de
    -- quien envía, no un dato nuevo.
    CONSTRAINT lecturas_metrica_valida CHECK (
        metrica IN ('cpu', 'memoria', 'disco_libre', 'latencia', 'tasa_error')
    ),

    -- Cada métrica se mide siempre en la misma unidad. Sin esto, dos equipos
    -- podrían reportar la misma columna en escalas distintas y la serie sería
    -- incomparable.
    CONSTRAINT lecturas_unidad_coherente CHECK (
        unidad = CASE metrica
            WHEN 'cpu'         THEN 'porcentaje'
            WHEN 'memoria'     THEN 'porcentaje'
            WHEN 'disco_libre' THEN 'bytes'
            WHEN 'latencia'    THEN 'milisegundos'
            WHEN 'tasa_error'  THEN 'proporcion'
        END
    ),

    -- DOUBLE PRECISION acepta NaN e infinitos. Si entran, envenenan en
    -- silencio cualquier AVG o SUM posterior, así que se rechazan aquí.
    CONSTRAINT lecturas_valor_finito CHECK (
        valor <> 'NaN'::double precision
        AND valor <> 'Infinity'::double precision
        AND valor <> '-Infinity'::double precision
    ),

    -- Un equipo no puede tener dos lecturas de la misma métrica en el mismo
    -- instante. Además es lo que hace repetible el seed: el reintento choca
    -- contra esta restricción en vez de duplicar filas.
    CONSTRAINT lecturas_sin_duplicados UNIQUE (equipo_id, metrica, medido_en)
);

-- Consulta esperada: las últimas lecturas de un equipo.
CREATE INDEX IF NOT EXISTS lecturas_equipo_medido_idx
    ON lecturas (equipo_id, medido_en DESC);

INSERT INTO schema_migrations (version)
VALUES ('0002_lecturas')
ON CONFLICT (version) DO NOTHING;

COMMIT;
