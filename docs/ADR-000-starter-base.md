# ADR-000 — Base inicial reproducible del CDRL

## Estado

Aceptado como punto de partida del curso.

## Contexto

Los equipos necesitan un punto de inicio común, pero deben crear y administrar su propio repositorio GitHub. El lenguaje de la aplicación se seleccionará por equipo durante M01.

## Decisión

La base incluye una interfaz Makefile común, configuración sintética, Docker Compose para PostgreSQL y DynamoDB local, directorios de evidencia y un workflow de retroalimentación. Cada equipo ampliará esta base y documentará cualquier diferencia entre el entorno local y AWS Academy Learner Lab.

## Alternativas consideradas

- Fijar un lenguaje único: se descarta porque limitaría la comparación de decisiones de ingeniería.
- Exigir una cuenta AWS personal: se descarta por seguridad y costo.
- Entregar solamente un documento: se descarta porque no permite comprobar reproducibilidad.

## Límites conocidos

- La imagen de DynamoDB local debe ser registrada por versión o digest por el equipo cuando se use como respaldo.
- Esta base no implementa todavía el esquema, la API ni las pruebas de los hitos; esas capacidades se agregan de forma incremental.
