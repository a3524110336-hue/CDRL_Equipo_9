#!/usr/bin/env bash
#
# Pone (o rota) la contraseña de los roles que crea db/migrations/0005.
#
#   bash scripts/rotate_db_passwords.sh              # los cuatro roles
#   bash scripts/rotate_db_passwords.sh writer ops   # solo esos
#
# Lee cada contraseña del entorno y NUNCA la escribe en disco ni la imprime:
#
#   CDRL_MIGRATOR_PASSWORD  CDRL_WRITER_PASSWORD
#   CDRL_READER_PASSWORD    CDRL_OPS_PASSWORD
#
# Rotar es volver a ejecutarlo con otro valor en el entorno; no hay que tocar
# ningún GRANT, porque los privilegios cuelgan del rol y no de la contraseña.
# Procedimiento completo y reparto de responsabilidades: docs/SECRETS.md.
set -euo pipefail

: "${POSTGRES_USER:=cdrl_dev}"
: "${POSTGRES_DB:=cdrl}"

roles=("$@")
if [ ${#roles[@]} -eq 0 ]; then
  roles=(migrator writer reader ops)
fi

# El SQL viaja por stdin, jamás por la línea de comandos: `docker inspect` y
# `ps` muestran los argumentos de cualquier proceso del sistema, y ahí es donde
# se filtran las contraseñas de este tipo de scripts.
sql=$'SET log_statement = \'none\';\nSET log_min_duration_statement = -1;\n'
faltantes=()

for rol in "${roles[@]}"; do
  variable="CDRL_$(echo "$rol" | tr '[:lower:]' '[:upper:]')_PASSWORD"
  valor="${!variable:-}"

  if [ -z "$valor" ]; then
    faltantes+=("$variable")
    continue
  fi

  # Duplicar la comilla simple es el escape de PostgreSQL. Sin esto, una
  # contraseña con apóstrofo rompería la sentencia — o la extendería.
  escapado="${valor//\'/\'\'}"
  sql+="ALTER ROLE cdrl_${rol} PASSWORD '${escapado}';"$'\n'
done

if [ ${#faltantes[@]} -gt 0 ]; then
  echo "Faltan variables de entorno: ${faltantes[*]}" >&2
  echo "Defínelas en tu .env local (que está en .gitignore) y vuelve a intentarlo." >&2
  exit 1
fi

docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f - <<< "$sql"

# Los nombres sí; los valores no.
echo "Contraseña actualizada para: ${roles[*]/#/cdrl_}"
