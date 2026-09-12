-- 0001_reconciliar_alertas.sql — DML de M02: deja `alertas` coherente con las
-- lecturas y los umbrales vigentes.
--
-- Las tres sentencias son convergentes, no solo idempotentes: reaplicar el
-- archivo sobre una base ya reconciliada afecta 0 filas. Eso permite que
-- scripts/apply_migrations.sh lo corra en cada `make verify` sin acumular basura.
--
-- `alertas` es la única tabla que este proyecto escribe derivando datos. La
-- consulta LECTURAS_FUERA_DE_UMBRAL de src/queries.py es de solo lectura y
-- nunca pasa por aquí.

BEGIN;

-- 1. Baja: la lectura ya no viola su umbral. Ocurre cuando alguien ensancha el
--    umbral después de que la alerta se levantó. Comparación no estricta porque
--    un valor igual a un extremo está DENTRO del umbral
--    (docs/API-lecturas.md).
DELETE FROM alertas AS a
USING lecturas AS l, umbrales AS u
WHERE a.lectura_id = l.id
  AND a.umbral_id  = u.id
  AND l.valor >= u.minimo
  AND l.valor <= u.maximo;

-- 2. Alta: una alerta por cada lectura fuera de su umbral. `detectada_en` toma
--    `registrado_en` —el instante en que el sistema supo del valor, no el
--    instante en que se midió— para que reaplicar el DML no mueva la fecha.
--    La restricción UNIQUE arbitra las reejecuciones.
INSERT INTO alertas (
    lectura_id, umbral_id, tipo, severidad, valor_observado, detectada_en
)
SELECT
    l.id,
    u.id,
    CASE WHEN l.valor < u.minimo THEN 'bajo_minimo' ELSE 'sobre_maximo' END,
    severidad_alerta(l.valor, u.minimo, u.maximo),
    l.valor,
    l.registrado_en
FROM lecturas AS l
JOIN umbrales AS u ON u.metrica = l.metrica
WHERE l.valor < u.minimo
   OR l.valor > u.maximo
ON CONFLICT ON CONSTRAINT alertas_sin_duplicados DO NOTHING;

-- 3. Reevaluación: el umbral cambió pero la lectura sigue violándolo, así que
--    el tipo o la severidad guardados quedaron obsoletos. Las alertas cerradas
--    no se tocan: son historial. El WHERE final es lo que hace que la segunda
--    corrida afecte 0 filas.
UPDATE alertas AS a
SET tipo      = CASE WHEN l.valor < u.minimo THEN 'bajo_minimo' ELSE 'sobre_maximo' END,
    severidad = severidad_alerta(l.valor, u.minimo, u.maximo)
FROM lecturas AS l, umbrales AS u
WHERE a.lectura_id = l.id
  AND a.umbral_id  = u.id
  AND a.estado <> 'cerrada'
  AND (
        a.severidad <> severidad_alerta(l.valor, u.minimo, u.maximo)
        OR a.tipo <> CASE WHEN l.valor < u.minimo THEN 'bajo_minimo' ELSE 'sobre_maximo' END
      );

COMMIT;
