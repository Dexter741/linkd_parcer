#!/usr/bin/env bash
# Dump the parser database to backups/ (keeps the last 48 dumps).
# Cron, hourly:  0 * * * * cd /path/to/linkd_parcer && sh scripts/backup_db.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
if [ -f .env ]; then set -a; . ./.env; set +a; fi
file="backups/linkd_$(date +%Y%m%d_%H%M).sql.gz"
trap '[ $? -eq 0 ] || rm -f "$file"' EXIT  # never leave a broken dump behind
if [ -n "$(docker compose ps -q db 2>/dev/null)" ]; then
    docker compose exec -T db pg_dump -U "${POSTGRES_USER:-linkd}" "${POSTGRES_DB:-linkd}" | gzip > "$file"
else
    PGPASSWORD="${POSTGRES_PASSWORD:-linkd}" pg_dump -h "${POSTGRES_HOST:-localhost}" -p "${DB_HOST_PORT:-${POSTGRES_PORT:-5432}}" \
        -U "${POSTGRES_USER:-linkd}" "${POSTGRES_DB:-linkd}" | gzip > "$file"
fi
ls -1t backups/linkd_*.sql.gz | tail -n +49 | xargs -r rm --
echo "$file"
