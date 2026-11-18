#!/usr/bin/env bash
# Restore a backup into a throwaway Postgres container and sanity-check it.
# Proves the backup is usable without touching production. Run monthly (see
# docs/runbooks/backup-restore.md).
#   infra/scripts/restore-drill.sh backups/last/showcase-latest.sql.gz
set -euo pipefail
dump="${1:?usage: $0 <backup.sql.gz>}"
[ -s "$dump" ] || { echo "no such backup: $dump" >&2; exit 1; }
name="showcase-restore-drill-$$"
image="postgres:18.6-alpine"

echo "==> starting $image"
docker run -d --name "$name" -e POSTGRES_USER=showcase -e POSTGRES_PASSWORD=drill \
  -e POSTGRES_DB=showcase "$image" >/dev/null
trap 'docker rm -f "$name" >/dev/null' EXIT
for _ in $(seq 1 30); do
  docker exec "$name" pg_isready -U showcase -d showcase >/dev/null 2>&1 && break
  sleep 1
done
sleep 2  # the image restarts Postgres once after init

echo "==> restoring $dump ($(du -h "$dump" | cut -f1))"
start=$(date +%s)
# Backups use --schema=public, which includes CREATE SCHEMA public: drop the empty default.
docker exec "$name" psql -q -U showcase -d showcase -c "DROP SCHEMA public CASCADE" >/dev/null
gunzip -c "$dump" | docker exec -i "$name" psql -q -v ON_ERROR_STOP=1 -U showcase -d showcase >/dev/null
echo "    restored in $(( $(date +%s) - start ))s"

echo "==> sanity checks"
docker exec -i "$name" psql -U showcase -d showcase -v ON_ERROR_STOP=1 -At <<'SQL'
SELECT 'alembic revision: ' || version_num FROM alembic_version;
SELECT 'websites:         ' || count(*) || ' (' || count(*) FILTER (WHERE status = 'active') || ' active)' FROM websites;
SELECT 'domains:          ' || count(*) FROM website_domains;
SELECT 'products:         ' || count(*) FROM products;
SELECT 'translations:     ' || count(*) FROM product_translations;
SELECT 'product images:   ' || count(*) FROM product_images;
SELECT 'publish jobs:     ' || count(*) || ' (' || count(*) FILTER (WHERE status = 'pending') || ' pending)' FROM publish_jobs;
SELECT 'admin users:      ' || count(*) FROM admin_users;
SELECT 'newest change:    ' || coalesce(max(updated_at)::text, '-') FROM websites;
SQL
echo "==> restore drill passed"
