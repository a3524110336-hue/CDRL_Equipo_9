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

echo "Migraciones, seed y DML aplicados."