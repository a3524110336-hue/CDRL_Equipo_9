-- 0006_vista_operador.sql — lo que cdrl_ops necesita ver para operar, sin datos.
--
-- Hasta aquí cdrl_ops solo leía schema_migrations y las vistas pg_stat_* de
-- pg_monitor (0005). Eso le dice si la base está viva, pero no si la ingesta lo
-- está: para saber si un equipo dejó de reportar tendría que leer `lecturas`, y
-- eso es justo lo que su rol le niega. Estas dos vistas le dan la respuesta
-- operativa sin abrirle las tablas.
--
-- Qué SÍ expone: conteos, instantes y el código del equipo (que es el
-- identificador con el que se opera). Qué NO: valores medidos, umbrales,
-- valor_observado, nombre ni ubicación del equipo, ni identificadores de filas.
--
-- Por qué funciona sin GRANT sobre las tablas: una vista se ejecuta con los
-- privilegios de su dueño (cdrl_migrator, que aplica esta migración con
-- SET ROLE), no con los de quien la consulta. security_barrier impide que un
-- filtro del consultante se evalúe antes que los de la vista y filtre datos por
-- efectos laterales. Son agregadas, así que tampoco son actualizables.
--
-- IDEMPOTENTE y CONVERGENTE: CREATE OR REPLACE y revocar antes de otorgar. La
-- 0005 revoca ALL ON ALL TABLES (que incluye vistas) al reaplicarse; esta
-- migración corre después y deja otra vez exactamente estos privilegios.

BEGIN;

-- Una fila por equipo y métrica: cuántas lecturas hay y cuándo llegó la última.
CREATE OR REPLACE VIEW ops_estado_ingesta
WITH (security_barrier = true) AS
SELECT
    e.codigo             AS equipo,
    l.metrica            AS metrica,
    count(*)             AS lecturas,
    max(l.medido_en)     AS ultima_medicion,
    max(l.registrado_en) AS ultimo_registro
FROM lecturas l
JOIN equipos e ON e.id = l.equipo_id
GROUP BY e.codigo, l.metrica;

-- Una fila por estado y severidad: cuántas alertas hay y desde cuándo.
CREATE OR REPLACE VIEW ops_resumen_alertas
WITH (security_barrier = true) AS
SELECT
    a.estado            AS estado,
    a.severidad         AS severidad,
    count(*)            AS alertas,
    min(a.detectada_en) AS mas_antigua,
    max(a.detectada_en) AS mas_reciente
FROM alertas a
GROUP BY a.estado, a.severidad;

-- En una base nueva la primera pasada de apply_migrations.sh corre con el
-- administrador (cdrl_migrator aún no existía al empezar), así que la vista
-- nacería suya: privilegios de superusuario y, en la segunda pasada, un
-- CREATE OR REPLACE que cdrl_migrator no podría hacer por no ser el dueño.
ALTER VIEW ops_estado_ingesta  OWNER TO cdrl_migrator;
ALTER VIEW ops_resumen_alertas OWNER TO cdrl_migrator;

-- Solo cdrl_ops. El reader (API) no las necesita y el writer menos: cada rol
-- ve lo de su oficio. Se revoca también a los defaults de 0005, que darían
-- SELECT a cdrl_reader sobre toda vista nueva de cdrl_migrator.
REVOKE ALL ON ops_estado_ingesta, ops_resumen_alertas
    FROM PUBLIC, cdrl_writer, cdrl_reader, cdrl_ops;
GRANT SELECT ON ops_estado_ingesta, ops_resumen_alertas TO cdrl_ops;

INSERT INTO schema_migrations (version)
VALUES ('0006_vista_operador')
ON CONFLICT (version) DO NOTHING;

COMMIT;
