#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_USER:=cdrl_dev}"
: "${POSTGRES_DB:=cdrl}"

# Desde M03 el esquema es de cdrl_migrator (db/migrations/0005). Cada archivo se
# aplica con SET ROLE para que el dueño de lo que se cree sea ese rol y no quien
# abrió la conexión; si no, los ALTER DEFAULT PRIVILEGES de la 0005 no aplicarían
# a las tablas nuevas y la API se quedaría sin poder leerlas.
#
# En una base recién creada el rol todavía no existe: las migraciones hasta la
# 0005 corren con el usuario administrador, que es justo quien debe crear roles.
# La propia 0005 empieza con RESET ROLE, así que reaplicarla nunca queda atrapada
# dentro de un SET ROLE.
rol_de_migracion_disponible() {
  docker compose exec -T postgres \
    psql -tAqX -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c "SELECT 1 FROM pg_roles WHERE rolname = 'cdrl_migrator'" 2>/dev/null | grep -q '^1$'
}

# ON_ERROR_STOP=1 es obligatorio: sin él psql informa el error, sigue con la
# sentencia siguiente y termina en 0, así que una migración rota pasaría el
# verify en silencio.
aplicar() {
  local etapa="$1" directorio="$2"
  echo "$etapa"

  local prefijo=""
  if rol_de_migracion_disponible; then
    prefijo="SET ROLE cdrl_migrator;"
  fi

  for f in "$directorio"/*.sql; do
    [ -e "$f" ] || continue
    echo "  -> $f"
    { printf '%s\n' "$prefijo"; cat "$f"; } | docker compose exec -T postgres \
      psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -
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