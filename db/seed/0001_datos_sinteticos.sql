-- 0001_datos_sinteticos
--
-- Datos SINTÉTICOS de arranque. No hay nada real aquí: ni equipos reales, ni
-- ubicaciones de clientes, ni mediciones capturadas de ningún sistema.
--
-- IDEMPOTENTE Y DETERMINISTA. Las dos cosas van juntas:
--   * Los instantes salen de una fecha base fija, no de now(), y los valores
--     de una fórmula, no de random(). Dos corridas generan exactamente las
--     mismas filas.
--   * Por eso el ON CONFLICT DO NOTHING puede reconocerlas y no duplicar.
-- Si aquí se usara now() o random(), cada corrida inventaría filas nuevas que
-- ningún ON CONFLICT sabría reconocer, y el seed dejaría de ser repetible.

BEGIN;

-- 5 equipos. El código es la identidad; el conflicto se resuelve contra él.
INSERT INTO equipos (codigo, nombre, ubicacion) VALUES
    ('edge-01',    'Nodo de borde 01',      'Puebla'),
    ('edge-02',    'Nodo de borde 02',      'Puebla'),
    ('gateway-01', 'Gateway de planta 01',  'Monterrey'),
    ('sensor-01',  'Concentrador sensor 01', 'Guadalajara'),
    ('sensor-02',  'Concentrador sensor 02', 'Guadalajara')
ON CONFLICT (codigo) DO NOTHING;

-- 12 lecturas por equipo y métrica: 5 equipos x 5 métricas x 12 = 300 filas.
-- Cada instante va 5 minutos después del anterior, arrancando en la fecha base.
-- El valor oscila con un seno alrededor de su base, así que la serie se ve
-- como telemetría y no como una recta, pero sigue siendo reproducible.
INSERT INTO lecturas (equipo_id, metrica, unidad, valor, medido_en)
SELECT
    e.id,
    m.metrica,
    m.unidad,
    round((m.base + m.amplitud * sin((e.id * 7 + n) / 3.0))::numeric, 4),
    TIMESTAMPTZ '2026-09-01 00:00:00+00' + (n * INTERVAL '5 minutes')
FROM equipos e
CROSS JOIN (VALUES
    -- métrica,      unidad,         base,     amplitud   -> rango resultante
    ('cpu',         'porcentaje',      45.0,       25.0),  --   20 .. 70 %
    ('memoria',     'porcentaje',      60.0,       15.0),  --   45 .. 75 %
    ('disco_libre', 'bytes',        2.0e10,      5.0e9),   -- 15GB .. 25GB
    ('latencia',    'milisegundos',   120.0,       60.0),  --   60 .. 180 ms
    ('tasa_error',  'proporcion',      0.02,      0.015)   -- .005 .. .035
) AS m(metrica, unidad, base, amplitud)
CROSS JOIN generate_series(0, 11) AS n
ON CONFLICT ON CONSTRAINT lecturas_sin_duplicados DO NOTHING;

COMMIT;
