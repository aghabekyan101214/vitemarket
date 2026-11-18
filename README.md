# Showcase backend

A multi-tenant FastAPI + PostgreSQL backend for many statically generated product showcase sites
(30–100+), each with its own domains, theme, locales, content, SEO and product selection.
A shared product catalog with per-locale translations feeds every site. Images live in
Cloudflare R2. Every edit triggers a signed revalidation webhook to the frontend and a
Cloudflare cache purge.

* Architecture and decisions: [`PLAN.md`](PLAN.md)
* Add a website (no code changes): [`docs/runbooks/add-website.md`](docs/runbooks/add-website.md)
* Deploy, rollback, hardening: [`docs/runbooks/deploy.md`](docs/runbooks/deploy.md)
* Backups and restore: [`docs/runbooks/backup-restore.md`](docs/runbooks/backup-restore.md)
* Frontend revalidation endpoint: [`docs/frontend-revalidation.md`](docs/frontend-revalidation.md)

## Quick start (local)

Requirements: Docker with Compose v2.

```bash
docker compose up -d                                    # db, SeaweedFS (S3), migrate, api, worker, webhook-sink
docker compose exec api python -m app.cli seed          # 2 sites, 10 products, images, admin@example.com / admin-password-123
open http://localhost:8000/docs                         # Swagger UI (Authorize → paste a token from /admin/v1/auth/login)
```

| URL | What |
| --- | --- |
| http://localhost:8000/docs | OpenAPI / Swagger UI |
| http://localhost:8000/public/v1/sites | Public API: all active sites and their hosts |
| http://localhost:8000/public/v1/sites/nordic-home-de/bundle?locale=en | Everything a site build needs, in one call |
| http://localhost:8333/media/… | Uploaded images (SeaweedFS stands in for R2) |
| `docker compose logs -f webhook-sink` | Revalidation webhooks the worker sends |
| `docker compose --profile tools up -d adminer` → http://localhost:8082 | DB UI |

## Everyday commands

```bash
# tests (real Postgres, separate showcase_test DB; each test rolls back)
docker compose exec api pytest -q

# lint, format, types (the same checks CI runs)
docker compose exec api sh -c "ruff format app tests migrations && ruff check app tests migrations && mypy app tests"

# migrations
docker compose exec api alembic revision --autogenerate -m "describe change"
docker compose exec api alembic upgrade head
docker compose exec api alembic check                   # models and migrations in sync?

# CLI
docker compose exec api python -m app.cli create-admin --email you@example.com --role admin
docker compose exec api python -m app.cli publish-all   # full republish of every site
docker compose exec api python -m app.cli media-gc      # purge deleted images past retention
docker compose exec api python -m app.cli openapi > openapi.json

# after changing dependencies (pyproject.toml), rebuild all three images
docker compose exec api uv lock && docker compose build api worker migrate && docker compose up -d
```

## Layout

```
.github/workflows/ci.yml        lint · types · tests · migration drift · prod image → GHCR
docker-compose.yml              local stack
docker-compose.prod.yml         production: db, db-backup, migrate, api, worker, cloudflared
.env.example / .env.prod.example
docs/                           runbooks, frontend integration
infra/scripts/                  restore-drill.sh, smoke-test-image.sh
backend/
  app/
    main.py                     app factory: middleware, routers, error handlers
    worker.py                   publish worker (webhook + CDN purge) and media GC
    cli.py                      seed, create-admin, publish-all, media-gc, openapi
    core/                       config, db, logging, errors, security, storage, caching, middleware
    api/                        HTTP layer: public.py, admin/*
    modules/                    domain logic: websites, content, catalog, media, auth, audit, publishing, public
  migrations/                   Alembic (async)
  tests/                        unit/, api/ (public), admin/, db/
```

## APIs at a glance

* **`/public/v1`** (no auth, read-only, CDN-cacheable): host resolution, site config,
  home, product listing and detail, SEO, sitemap, and a one-call `bundle`. Responses carry
  weak ETags derived from the site's `content_version` (304 on revalidation).
* **`/admin/v1`** (Bearer JWT; roles `admin`, `catalog_manager`, `site_editor`): websites,
  domains with DNS verification, themes, content blocks, SEO, products, translations,
  per-site assignments, images, users, the audit log and publish jobs. Optimistic locking with
  `If-Match` on theme and content.

Errors are RFC 9457 `application/problem+json` with a stable `code`.
