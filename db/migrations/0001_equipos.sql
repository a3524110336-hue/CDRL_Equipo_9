-- 0001_equipos
--
-- Catálogo de equipos: los dispositivos que emiten telemetría.
--
-- IDEMPOTENTE: este archivo se puede aplicar N veces sin cambiar el resultado.
-- Todo objeto se crea con IF NOT EXISTS y el registro de la migración usa
-- ON CONFLICT DO NOTHING.
--
-- Matiz importante: IF NOT EXISTS es idempotente, no convergente. Si la tabla
-- ya existe con otra forma, esta migración no la corrige ni avisa. Un cambio
-- de esquema pide una migración NUEVA, nunca editar esta.

BEGIN;

-- Control de qué migraciones se han aplicado.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    aplicada_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS equipos (
    id        SERIAL      PRIMARY KEY,
    codigo    TEXT        NOT NULL UNIQUE,
    nombre    TEXT        NOT NULL,
    ubicacion TEXT        NOT NULL,
    activo    BOOLEAN     NOT NULL DEFAULT TRUE,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- El código es la identidad estable del equipo (lo que manda el agente).
    -- Etiqueta tipo DNS: minúsculas, dígitos y guiones interiores, 3..63.
    CONSTRAINT equipos_codigo_valido CHECK (
        char_length(codigo) BETWEEN 3 AND 63
        AND codigo ~ '^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$'
    )
);

INSERT INTO schema_migrations (version)
VALUES ('0001_equipos')
ON CONFLICT (version) DO NOTHING;

COMMIT;
