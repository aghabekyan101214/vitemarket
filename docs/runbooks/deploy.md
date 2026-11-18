# Runbook: deploy, rollback, and production hardening

## Deploy

CI (`.github/workflows/ci.yml`) runs on every push to `main`:

1. lint and types;
2. tests (real Postgres and SeaweedFS), plus a migration drift check;
3. builds the production image and pushes `ghcr.io/<owner>/showcase-api:<YYYY.MM.DD>-<sha7>`.

The tag is shown in the run summary. To deploy it:

```bash
cd /srv/showcase
C="docker compose -f docker-compose.prod.yml --env-file .env.prod"
sed -i "s/^API_TAG=.*/API_TAG=2026.11.11-abc1234/" .env.prod
$C pull api
$C up -d          # migrate runs first; api/worker start only if it succeeds
$C ps             # api healthy, migrate exited 0
$C logs --since 5m api worker | grep -i error
```

Sites keep serving throughout: visitors hit static pages on the CDN, not this API. A
restart only briefly delays admin calls and publishing.

## Migrations policy

Migrations run automatically before the new code starts, so every migration must be
**backward compatible with the previous release** (expand, then contract):

* add nullable columns or columns with defaults; add tables and indexes
  (`CREATE INDEX CONCURRENTLY` for large tables);
* rename or drop only in a *later* release, once no deployed code uses the old shape;
* never edit a migration that has run in production.

CI fails if models and migrations drift (`alembic check`). Take an on-demand backup before
any migration that rewrites data (see `backup-restore.md`).

## Rollback

```bash
sed -i "s/^API_TAG=.*/API_TAG=<previous tag>/" .env.prod
$C up -d api worker      # skip migrate: the schema stays (backward compatible by policy)
```

Downgrading the schema (`alembic downgrade -1`) is a last resort; data in new columns is
lost. Restore a backup instead if the data itself is wrong.

## Production hardening checklist

| Control | Where | Status |
| --- | --- | --- |
| TLS, DDoS, WAF | Cloudflare in front; the VM has **no inbound ports** (Tunnel) | compose |
| HSTS, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy` | API middleware (HSTS when `APP_ENV=production`) | app |
| Request body limits | `MAX_REQUEST_BYTES` (JSON, 1 MiB) and `UPLOAD_MAX_BYTES` (+1 MiB for multipart) → 413 | app |
| Login brute force | Per-account lockout (5 failures → 15 min), per-IP limit (`LOGIN_RATE_LIMIT_PER_MINUTE`, per process) and the Cloudflare rule below | app + CF |
| Token revocation | A password change invalidates tokens issued before it; deactivating a user blocks them at once | app |
| Short-lived tokens | `JWT_ACCESS_TTL_MINUTES` (30) | env |
| Least privilege | Roles admin / catalog_manager / site_editor; R2 token scoped to one bucket; CF token = Cache Purge only | app + CF |
| HTML injection | Rich text sanitized server-side (nh3 allowlist) | app |
| Uploads | Re-encoded with Pillow; EXIF/GPS stripped; no SVG; pixel limit | app |
| Audit | Every admin change in `audit_log` with a diff | app |
| Secrets | Validated at startup in production (JWT ≥ 32 chars, webhook/CF secrets present) | app |
| Non-root container | Image runs as `app`; checked by `infra/scripts/smoke-test-image.sh` | image |
| Backups | Nightly + off-box + monthly drill | `backup-restore.md` |

### Cloudflare rate-limiting rule (admin login)

Security → WAF → Rate limiting rules → create:

* **If:** `http.request.uri.path eq "/admin/v1/auth/login" and http.request.method eq "POST"`
* **Rate:** 10 requests per 1 minute, counted by IP
* **Then:** Block for 10 minutes

Optionally put `/admin/*` behind Cloudflare Access (SSO) so only staff can reach it at all.
The public API needs no auth, but should be cached: add a Cache Rule for
`/public/v1/*` to respect origin cache headers.

## Rotating secrets

| Secret | Procedure | Impact |
| --- | --- | --- |
| `JWT_SECRET` | New value in `.env.prod`, `$C up -d api` | Everyone logs in again |
| `REVALIDATE_WEBHOOK_SECRET` | Frontend accepts old + new → change backend → remove old | None if staged |
| `CLOUDFLARE_API_TOKEN`, R2 keys | Create the new token, update the env, `$C up -d api worker`, revoke the old one | None |
| `POSTGRES_PASSWORD` | `ALTER ROLE showcase PASSWORD '…'` → update the env → `$C up -d` | Brief restart |
| Admin password | `PATCH /admin/v1/users/{id}` with `password` | Their existing tokens stop working |
