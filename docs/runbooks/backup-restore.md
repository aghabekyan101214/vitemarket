# Runbook: backups and restore

## What needs backing up

| Data | Where | Backup |
| --- | --- | --- |
| Everything editorial: sites, domains, themes, content, SEO, products, translations, users, audit log, publish queue | Postgres (`pgdata` volume) | Nightly `pg_dump` by the `db-backup` service, synced off the box |
| Images | Cloudflare R2 bucket (`STORAGE_BUCKET`) | R2 is replicated storage. Object keys are immutable and deletes are delayed `MEDIA_RETENTION_DAYS` (7). Optional copy to a second bucket, below. |
| Secrets | `.env.prod` | Password manager or secret store. **Not** in the backups. |
| Generated sites | Frontend hosting / CDN | Not backed up: rebuilt from the DB by a full publish |

Targets: **RPO ≤ 24 h** (nightly dump; tighten by changing `SCHEDULE`). **RTO about 30 min**
for a full VM loss (dump restore takes seconds to minutes at this data size).

## Nightly dumps

`docker-compose.prod.yml` → `db-backup` ([prodrigestivill/postgres-backup-local](https://github.com/prodrigestivill/docker-postgres-backup-local)):

* `pg_dump -Z9 --schema=public` every day into `./backups` next to the compose file;
* rotation: 7 daily, 4 weekly, 6 monthly;
* layout: `backups/{daily,weekly,monthly}/showcase-YYYYMMDD*.sql.gz`, plus
  `backups/last/showcase-latest.sql.gz`.

Take an on-demand backup, e.g. before a risky migration:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec db-backup /backup.sh
ls -lh backups/last/
```

## Off-box copy (required)

A backup on the same disk as the database is not a backup. Sync `./backups` to a
**separate** R2 bucket (not the media bucket), with an API token that is scoped to that
bucket only and has no delete permission:

```ini
# ~/.config/rclone/rclone.conf on the VM
[r2backup]
type = s3
provider = Cloudflare
access_key_id = …
secret_access_key = …
endpoint = https://<ACCOUNT_ID>.r2.cloudflarestorage.com
no_check_bucket = true
```

```cron
# crontab -e  (runs after the nightly dump; "copy" never deletes remote files;
# --copy-links follows the *-latest.sql.gz symlinks)
30 3 * * * rclone copy /srv/showcase/backups r2backup:showcase-db-backups --include "*.sql.gz" --copy-links --log-file /var/log/rclone-backup.log
```

Set an **R2 lifecycle rule** on `showcase-db-backups` (e.g. delete objects after 180 days),
so retention lives in the bucket and the VM never needs delete rights. Even if the VM
is compromised, older backups cannot be destroyed from it.

Optionally, protect images against accidental mass deletion (bugs, leaked keys) with a
weekly copy of the media bucket:

```cron
0 4 * * 0 rclone copy r2media:showcase-media r2backup:showcase-media-copy --log-file /var/log/rclone-media.log
```

## Monitoring

* `db-backup` exposes a healthcheck on :8080; `docker ps` shows it unhealthy if the last
  run failed.
* Alert when the newest object in `showcase-db-backups` is older than 26 h (e.g. a
  scheduled check with `rclone lsl … | sort | tail -1`, or an R2 event notification).

## Restore drill (monthly)

Proves the dumps are actually usable, without touching production:

```bash
rclone copy r2backup:showcase-db-backups/last/showcase-latest.sql.gz /tmp/drill/
infra/scripts/restore-drill.sh /tmp/drill/showcase-latest.sql.gz
```

The script restores into a throwaway `postgres:18.6-alpine` container and prints row
counts, the Alembic revision and the newest change. It fails on any SQL error. Record
the date and duration.

## Restore production

Pick the scenario first. **Never** restore over the live database before confirming
the backup is the right one: run the drill script on it first.

### A. Bad data, database fine (e.g. a site deleted or overwritten by mistake)

Prefer a surgical fix. The audit log (`GET /admin/v1/audit-log`) has the before/after
diff of every admin change; re-apply the old values through the API. That also triggers
the publish webhook. For larger damage, restore the dump into a side database and copy
the affected rows:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec db \
  createdb -U showcase showcase_restore
docker compose -f docker-compose.prod.yml --env-file .env.prod exec db \
  psql -U showcase -d showcase_restore -c "DROP SCHEMA public CASCADE"
gunzip -c backups/daily/showcase-YYYYMMDD.sql.gz | \
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T db \
  psql -v ON_ERROR_STOP=1 -U showcase -d showcase_restore
# inspect, then copy rows across with INSERT … SELECT via dblink, or by hand; drop showcase_restore after
```

### B. Database lost or corrupt: full restore

```bash
cd /srv/showcase
C="docker compose -f docker-compose.prod.yml --env-file .env.prod"
DUMP=backups/last/showcase-latest.sql.gz      # or fetch it from r2backup first

# 1. Stop writers (the public API also goes down; sites stay up from the CDN and static hosting)
$C stop api worker cloudflared

# 2. Recreate an empty database
$C exec db dropdb -U showcase --if-exists showcase
$C exec db createdb -U showcase showcase
$C exec db psql -U showcase -d showcase -c "DROP SCHEMA public CASCADE"   # the dump creates it

# 3. Load the dump (ON_ERROR_STOP: all or nothing)
gunzip -c "$DUMP" | $C exec -T db psql -v ON_ERROR_STOP=1 -U showcase -d showcase

# 4. Bring the app back; migrate runs first and upgrades an older dump to the current schema
$C up -d

# 5. Republish every site so the frontend matches the restored data
$C exec api python -m app.cli publish-all
```

Alternatively, for step 5 call `POST /admin/v1/websites/{id}/publish` for each site.

### C. Whole VM lost

1. Provision a VM; install Docker; restore `/srv/showcase` (compose file, `.env.prod` from
   the secret store).
2. `$C up -d db` and wait for it to be healthy, then follow **B** from step 2, using the
   newest dump from `r2backup`.
3. `cloudflared` reconnects with the same tunnel token, so no DNS changes are needed.

### Images after a restore

The DB references images by immutable key. A dump from *N* days ago may reference images
deleted since. They survive in R2 for `MEDIA_RETENTION_DAYS` (7) after deletion, so
restores up to 7 days back are complete. For older dumps, copy any missing keys back from
`showcase-media-copy`:

```bash
rclone copy r2backup:showcase-media-copy r2media:showcase-media --ignore-existing
```

The other way round is harmless: images uploaded after the dump are unreferenced, and
cost a few bytes.

## Secrets

If `JWT_SECRET` is lost, users log in again (generate a new one). If
`REVALIDATE_WEBHOOK_SECRET` is lost, set a new one on both the backend and the frontend.
Neither affects the data.
