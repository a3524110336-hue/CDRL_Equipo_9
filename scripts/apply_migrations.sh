#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_USER:=cdrl_dev}"
: "${POSTGRES_DB:=cdrl}"

# ON_ERROR_STOP=1 es obligatorio: sin él psql informa el error, sigue con la
# sentencia siguiente y termina en 0, así que una migración rota pasaría el
# verify en silencio.
aplicar() {
  local etapa="$1" directorio="$2"
  echo "$etapa"
  for f in "$directorio"/*.sql; do
    [ -e "$f" ] || continue
    echo "  -> $f"
    docker compose exec -T postgres \
      psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f - < "$f"
  done
}

aplicar "Aplicando migraciones..." db/migrations
aplicar "Aplicando seed..."        db/seed
aplicar "Aplicando DML..."         db/dml

# Segunda pasada: es la prueba de la idempotencia que declara ADR-002. Si alguna
# migración o seed no fuera reaplicable, ON_ERROR_STOP=1 aborta aquí; y el DML
# convergente debe reportar 0 filas afectadas en esta pasada.
aplicar "Reaplicando migraciones (prueba de idempotencia)..." db/migrations
aplicar "Reaplicando seed (prueba de idempotencia)..."        db/seed
aplicar "Reaplicando DML (prueba de convergencia)..."         db/dml

echo "Migraciones, seed y DML aplicados dos veces: idempotencia verificada."