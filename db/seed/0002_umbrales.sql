-- 0002_umbrales.sql — umbrales sintéticos, deterministas y reejecutables.
--
-- Sin now() ni random(): dos corridas sobre una base limpia dejan exactamente
-- las mismas filas, igual que 0001_datos_sinteticos.sql.
--
-- Los extremos están elegidos contra los rangos que genera ese seed para que
-- cada métrica registrada produzca lecturas dentro Y fuera del umbral. Si todas
-- las lecturas cayeran dentro, el endpoint de violaciones devolvería siempre
-- vacío y no habría nada que demostrar.
--
--   cpu         seed 20.0 .. 69.8 %    umbral 25 .. 60    -> viola por ambos lados
--   memoria     seed 45.0 .. 74.9 %    umbral 40 .. 70    -> viola por arriba
--   latencia    seed 60.0 .. 179.4 ms  umbral  0 .. 150   -> viola por arriba
--   tasa_error  seed .005 .. .0349     umbral  0 .. 0.03  -> viola por arriba
--
-- El mínimo de 25 % en `cpu` no es relleno para generar datos: un nodo de borde
-- que baja sostenidamente de ese uso suele estar sin tráfico o con el proceso
-- caído, y eso se reporta igual que la saturación. Es también la única métrica
-- que ejercita el tipo 'bajo_minimo': las otras tres tienen mínimo 0 porque un
-- valor bajo de latencia, error o memoria no describe ninguna falla.
--
-- `disco_libre` queda a propósito sin umbral: el seed lo mide en bytes
-- absolutos y el modelo no guarda la capacidad total de cada equipo, así que un
-- único mínimo global en bytes sería falso para equipos de distinto tamaño.
-- El hueco además deja observable la ruta "métrica sin umbral registrado", que
-- docs/API-lecturas.md define como "no produce violaciones".

INSERT INTO umbrales (metrica, minimo, maximo) VALUES
    ('cpu',       25.0,  60.0),
    ('memoria',   40.0,  70.0),
    ('latencia',   0.0, 150.0),
    ('tasa_error', 0.0,   0.03)
ON CONFLICT (metrica) DO NOTHING;
