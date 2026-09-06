#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_USER:=cdrl_dev}"
: "${POSTGRES_DB:=cdrl}"

echo "Aplicando migraciones..."
for f in db/migrations/*.sql; do
  echo "  -> $f"
  docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f - < "$f"
done

echo "Aplicando seed..."
for f in db/seed/*.sql; do
  echo "  -> $f"
  docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f - < "$f"
done

echo "Migraciones y seed aplicados."