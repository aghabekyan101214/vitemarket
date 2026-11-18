# Multi-Tenant Showcase Backend — Implementation Plan

FastAPI · SQLAlchemy 2 (async) · Alembic · Pydantic v2 · PostgreSQL 18 · Cloudflare R2 · Docker Compose

Goal: one backend + one DB serving 30 → 100+ statically generated product showcase sites. Adding a site means
**inserting rows (website, domains, theme, content, product assignments). No code changes and no deploy.**

---

## 0. Key decisions (TL;DR)

| Topic | Decision | Why |
|---|---|---|
| Architecture | Modular monolith with one API process and one worker process, both from the same image | Simple, and it scales horizontally by adding API replicas |
| Tenant key | `websites.id` (UUIDv7) + immutable `websites.key` slug | Stable cache keys; key is human-readable in URLs |
| Tenant resolution | `website_domains` table with normalized hostname → website; only `active` domains resolve | Validated mapping, aliases, redirect-to-primary |
| Localization | Shared rows + `*_translations`/per-locale rows; fallback chain `requested → site default` | Products reused across sites/languages |
| Media | **Cloudflare R2** in prod; **SeaweedFS** (S3 API) locally | Same boto3 code path, only env vars differ |
| Image delivery | R2 public custom domain + Cloudflare Image Transformations (`/cdn-cgi/image/...`) | No resize service to run |
| Publishing | Transactional **outbox** (`publish_jobs` table) → worker → signed webhook to Next.js (`revalidateTag`) → optional Cloudflare purge | No Redis. Jobs can't be lost and are debounced per site |
| Queue | Postgres `FOR UPDATE SKIP LOCKED` (+ `LISTEN/NOTIFY` wakeup) | No extra infrastructure |
| HTTP caching | `Cache-Control` + weak ETag derived from `websites.content_version` | Gets a 304 without running content queries; CDN-friendly |
| Auth | Admin users in DB, Argon2id hashes, short-lived JWT, roles + per-website scope | No external IdP needed yet |
| Prod ingress | Cloudflare Tunnel (`cloudflared`) and no open ports | Everything already sits on Cloudflare; no TLS certificates to manage |

### Why SeaweedFS for local R2

- **R2 is S3-compatible.** The backend only does PutObject, HeadObject, DeleteObject and presign, so any S3 server can stand in for it.
- **MinIO is no longer a good default.** The community edition stopped publishing Docker images and binaries (Oct 2025), and the repo is in maintenance mode.
- **SeaweedFS** has an Apache-2.0 license and is actively released (`4.53` is pinned). It runs as a single container. Its identity config supports `anonymous: Read:media`, which mirrors an R2 bucket that is publicly readable via a custom domain. I tested this locally: anonymous GET returns 200, while anonymous PUT and LIST return 403.
- **Rejected alternatives:**
  - Garage: you must initialize the cluster layout by hand.
  - LocalStack: heavier, and S3 is only one of many services it emulates.
  - Wrangler/Miniflare local R2: reachable only through Worker bindings, not the S3 API, so Python can't use it.

**R2 compatibility rules for the storage adapter:**
- `region="auto"`. There are no ACLs: public access is set per bucket (custom domain), never per object.
- Presigned URLs work only on the `*.r2.cloudflarestorage.com` endpoint, not on the custom domain.
- Create the boto3 client with `request_checksum_calculation="when_required"` and `response_checksum_validation="when_required"` so the same client works on both R2 and SeaweedFS.

---

## 1. Project structure

```
website_designs/
├── PLAN.md
├── docker-compose.yml            # local: db, storage(SeaweedFS), storage-init, migrate, api, worker, webhook-sink, adminer(profile)
├── docker-compose.prod.yml       # prod: db, db-backup, migrate, api, worker, cloudflared
├── .env.example / .env.prod.example
├── infra/
│   ├── postgres/init/01-create-test-db.sql
│   └── seaweedfs/s3.json         # local S3 identities (backend rw, anonymous read on "media")
└── backend/
    ├── Dockerfile                # targets: dev, prod
    ├── pyproject.toml / uv.lock  # uv-managed
    ├── alembic.ini
    ├── migrations/               # env.py (async), versions/
    ├── app/
    │   ├── main.py               # create_app(): routers, middleware, exception handlers
    │   ├── worker.py             # publish worker entrypoint
    │   ├── cli.py                # create-admin, seed, enqueue-full-publish
    │   ├── core/
    │   │   ├── config.py         # pydantic-settings Settings (env-based)
    │   │   ├── db.py             # async engine, sessionmaker, get_session dependency
    │   │   ├── logging.py        # structlog JSON/console, request_id + website_id context
    │   │   ├── errors.py         # AppError hierarchy → RFC 9457 problem+json
    │   │   ├── security.py       # argon2 hashing, JWT encode/decode
    │   │   ├── storage.py        # S3Storage (boto3) — R2 / SeaweedFS
    │   │   ├── http_cache.py     # ETag/Cache-Control helpers, If-None-Match → 304
    │   │   ├── locale.py         # BCP-47 normalization + fallback chain
    │   │   └── pagination.py
    │   ├── api/
    │   │   ├── deps.py           # current_admin, require_role, require_site_access
    │   │   ├── public.py         # mounts /public/v1 routers
    │   │   └── admin.py          # mounts /admin/v1 routers
    │   └── modules/              # one folder per bounded context:
    │       ├── auth/             #   models · schemas · repository · service · router
    │       ├── websites/         #   websites, domains, themes, tenant resolution
    │       ├── content/          #   homepage/page content, SEO metadata
    │       ├── catalog/          #   products, translations, images, website_products
    │       ├── media/            #   upload validation + storage keys
    │       └── publishing/       #   outbox, change→affected-pages mapping, webhook, CDN purge
    └── tests/
        ├── conftest.py           # test DB, transactional session per test, httpx AsyncClient
        ├── factories.py
        ├── unit/                 # locale fallback, host normalization, change mapping, signatures
        └── api/                  # public + admin endpoint tests
```

**Layer rules.** Routers own HTTP only: parsing, status codes and headers. Services own business rules and
transactions. Repositories own SQLAlchemy queries. Models are ORM classes and schemas are Pydantic I/O.
Modules call each other only through services, never through another module's repository.

**Dependencies:**
- Runtime: `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `pydantic-settings`, `structlog`, `pyjwt`, `argon2-cffi`, `boto3`, `pillow`, `httpx`, `python-multipart`, `idna`, `nh3` (sanitizes rich text)
- Dev: `pytest`, `pytest-asyncio`, `ruff`, `mypy`, `watchfiles`

---

## 2. Database schema

All tables have `created_at`/`updated_at timestamptz`. IDs use the native `uuidv7()` from PG 18.
Locales are BCP-47 strings normalized to `ll` or `ll-RR` (e.g. `de`, `pt-BR`).

```sql
-- Enums
CREATE TYPE website_status AS ENUM ('draft','active','disabled');
CREATE TYPE domain_status  AS ENUM ('pending','active','disabled');
CREATE TYPE page_type      AS ENUM ('home','products','product');
CREATE TYPE job_status     AS ENUM ('pending','running','succeeded','failed');
CREATE TYPE admin_role     AS ENUM ('admin','catalog_manager','site_editor');

CREATE TABLE websites (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  key              text NOT NULL UNIQUE CHECK (key ~ '^[a-z0-9-]{2,50}$'),  -- immutable
  name             text NOT NULL,
  country_code     char(2) NOT NULL,                 -- ISO 3166-1
  default_locale   text NOT NULL,
  locales          text[] NOT NULL,                  -- must contain default_locale (CHECK)
  status           website_status NOT NULL DEFAULT 'draft',
  auto_publish     boolean NOT NULL DEFAULT true,
  content_version  bigint NOT NULL DEFAULT 1,        -- bumped on ANY change affecting the site → ETag
  published_version bigint NOT NULL DEFAULT 0,
  published_at     timestamptz,
  CHECK (default_locale = ANY(locales))
);

CREATE TABLE website_domains (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  website_id          uuid NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
  hostname            text NOT NULL UNIQUE CHECK (hostname = lower(hostname)), -- punycode, no port/trailing dot
  is_primary          boolean NOT NULL DEFAULT false,
  status              domain_status NOT NULL DEFAULT 'pending',
  redirect_to_primary boolean NOT NULL DEFAULT true,
  cloudflare_zone_id  text,                          -- for CDN purge; many sites = many zones
  verification_token  text NOT NULL,                 -- DNS TXT _showcase-verify.<host>
  verified_at         timestamptz
);
CREATE UNIQUE INDEX uq_domain_primary ON website_domains(website_id) WHERE is_primary;
CREATE INDEX ix_domains_website ON website_domains(website_id);

CREATE TABLE website_themes (                          -- 1:1 with websites
  website_id    uuid PRIMARY KEY REFERENCES websites(id) ON DELETE CASCADE,
  tokens        jsonb NOT NULL,     -- validated by Pydantic ThemeTokens: colors{}, fonts{heading,body,source}, radius, spacing, layout variants
  logo_key      text, logo_dark_key text, favicon_key text, og_default_image_key text,
  version       int NOT NULL DEFAULT 1                 -- optimistic locking
);

CREATE TABLE website_content (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  website_id  uuid NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
  page        page_type NOT NULL CHECK (page IN ('home','products')),
  locale      text NOT NULL,
  title       text NOT NULL,
  subtitle    text,
  blocks      jsonb NOT NULL DEFAULT '[]',   -- typed sections (hero, features, cta, text…) = Pydantic discriminated union
  version     int NOT NULL DEFAULT 1,
  UNIQUE (website_id, page, locale)
);

CREATE TABLE products (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  sku         text NOT NULL UNIQUE,
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  attributes  jsonb NOT NULL DEFAULT '{}'    -- non-localized specs (dimensions, materials codes…)
);

CREATE TABLE product_translations (
  product_id        uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  locale            text NOT NULL,
  name              text NOT NULL,
  slug              text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  short_description text,
  description       text,                    -- sanitized HTML / markdown
  attributes        jsonb NOT NULL DEFAULT '{}',  -- localized spec labels/values
  PRIMARY KEY (product_id, locale),
  UNIQUE (locale, slug)
);

CREATE TABLE website_products (
  website_id  uuid NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
  product_id  uuid NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
  position    int NOT NULL DEFAULT 0,
  is_visible  boolean NOT NULL DEFAULT true,
  is_featured boolean NOT NULL DEFAULT false,   -- shown on homepage
  PRIMARY KEY (website_id, product_id)
);
CREATE INDEX ix_wp_listing ON website_products(website_id, position, product_id) WHERE is_visible;
CREATE INDEX ix_wp_product ON website_products(product_id);   -- "which sites use this product?"

CREATE TABLE product_images (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  product_id   uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  storage_key  text NOT NULL UNIQUE,       -- products/{product_id}/{uuid}.{ext}  (immutable)
  content_type text NOT NULL, width int NOT NULL, height int NOT NULL, bytes int NOT NULL,
  position     int NOT NULL DEFAULT 0,
  alt          jsonb NOT NULL DEFAULT '{}' -- {"de": "...", "en": "..."}
);
CREATE INDEX ix_images_product ON product_images(product_id, position);

CREATE TABLE seo_metadata (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  website_id      uuid NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
  locale          text NOT NULL,
  page            page_type NOT NULL,
  product_id      uuid REFERENCES products(id) ON DELETE CASCADE,  -- only for page='product'
  title           text, description text,
  canonical_url   text,                    -- null = derived from primary domain + path
  og_title text, og_description text, og_image_key text,
  robots          text NOT NULL DEFAULT 'index,follow',
  structured_data jsonb,
  CHECK ((page = 'product') = (product_id IS NOT NULL)),
  UNIQUE NULLS NOT DISTINCT (website_id, locale, page, product_id)
);

-- Supporting tables
CREATE TABLE admin_users (id uuid PK, email citext UNIQUE, password_hash text, role admin_role,
                          is_active bool, failed_logins int, locked_until timestamptz, last_login_at timestamptz);
CREATE TABLE admin_user_websites (admin_user_id uuid FK, website_id uuid FK, PRIMARY KEY (admin_user_id, website_id));

CREATE TABLE publish_jobs (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  website_id  uuid NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
  status      job_status NOT NULL DEFAULT 'pending',
  tags        text[] NOT NULL DEFAULT '{}',
  paths       text[] NOT NULL DEFAULT '{}',
  full_site   boolean NOT NULL DEFAULT false,
  content_version bigint NOT NULL,
  attempts    int NOT NULL DEFAULT 0,
  run_after   timestamptz NOT NULL DEFAULT now(),
  last_error  text, started_at timestamptz, finished_at timestamptz
);
CREATE UNIQUE INDEX uq_job_pending_per_site ON publish_jobs(website_id) WHERE status = 'pending';  -- debounce/merge
CREATE INDEX ix_jobs_due ON publish_jobs(run_after) WHERE status = 'pending';

CREATE TABLE audit_log (id bigserial PK, admin_user_id uuid, action text, entity text, entity_id text,
                        website_id uuid, diff jsonb, created_at timestamptz DEFAULT now());
```

**Integrity rules enforced in services:**
- A product can be made visible on a site only if it has a translation for that site's `default_locale`.
- Content, SEO and translation locales must be in `websites.locales`.
- Exactly one primary domain per site, and it must be `active` before the site can become `active`.

---

## 3. Multi-tenant domain resolution

1. **Normalize the host.** Lowercase it, strip the port and any trailing dot, and convert IDN to punycode (`idna`). Reject anything that isn't a valid hostname.
2. **Look it up:**
   `SELECT d.website_id, d.is_primary, d.redirect_to_primary, w.key, w.status, w.default_locale, w.locales, p.hostname AS primary_host FROM website_domains d JOIN websites w … LEFT JOIN website_domains p ON p.website_id = w.id AND p.is_primary WHERE d.hostname = $1 AND d.status = 'active'`
   This is a single indexed query.
3. **Return a result:**
   - No row, or the website isn't `active` → 404.
   - Alias with `redirect_to_primary` → `{redirect: "https://<primary_host>"}`. The Next.js middleware issues the 301.
4. **Validate the domain.** A new domain starts as `pending`. `POST /admin/v1/domains/{id}/verify` checks the DNS TXT record `_showcase-verify.<host>` against `verification_token` and then sets the domain `active`. Admins can force-activate.
5. **How the frontend uses it.** The Next.js middleware reads `Host`, resolves it through a small map fetched at build time (`GET /public/v1/sites`, cached and revalidated via the `domains` tag), and rewrites to `/_sites/[siteKey]/[locale]/...`. Visitors never trigger a backend call. `/public/v1/resolve` exists for on-demand/fallback use and for tooling.
6. **Isolation.**
   - Every public query is parameterized by `website_id`, which comes from the resolved site and never from client-supplied IDs.
   - Repositories for site-scoped tables require `website_id` as a mandatory argument.
   - Admin routes check `require_site_access(website_id)`.
   - Tests assert that site A can never read site B's products, content or SEO.

---

## 4. Localization

- **Shared vs translated data.** Shared, non-translatable data lives in `products`. Translatable data lives in `product_translations`, `website_content(locale)`, `seo_metadata(locale)` and `product_images.alt[locale]`.
- **Fallback chain** (`core/locale.py`): requested locale → website `default_locale`. If neither exists, the item is omitted from public listings and not considered visible. It never silently shows another language. For SEO, missing fields fall back to the content title/description and then to site defaults.
- **Product reuse.** A product is reused across sites by assigning it in `website_products`. Each site picks the translation for its locale, so a German site and an Austrian site share one `de` translation.
- **hreflang.** Generated from `websites.locales` and the primary domain. It is returned in SEO responses and sitemap entries.

---

## 5. REST API

OpenAPI is served at `/docs` and `/openapi.json`, with tags per module. Errors use `application/problem+json`.
`{site}` = `websites.key`.

### Public (`/public/v1`), read-only and CDN-cacheable

| Method & path | Returns |
|---|---|
| `GET /resolve?host=` | `{website_key, website_id, locale, locales, primary_host, redirect?}` |
| `GET /sites` | All active sites with hosts and locales, used for `generateStaticParams` and the middleware host map |
| `GET /sites/{site}/config?locale=` | Site info, theme tokens, asset URLs, locales, nav |
| `GET /sites/{site}/home?locale=` | Home content blocks + featured products (summary) |
| `GET /sites/{site}/products?locale=&page=&page_size=` | Paginated visible products (summary + primary image), `page_size ≤ 100` |
| `GET /sites/{site}/products/{slug}?locale=` | Product detail: translation, attributes, all images |
| `GET /sites/{site}/seo?locale=&page=home\|products\|product&slug=` | Resolved SEO: title, description, canonical, og, robots, hreflang, JSON-LD |
| `GET /sites/{site}/bundle?locale=` | **Everything needed to build the site in one call**: config + home + SEO for all pages + all visible products (summaries) + slugs |
| `GET /sites/{site}/sitemap?locale=` | `[{path, lastmod, alternates}]` |

Headers: `Cache-Control: public, max-age=60, s-maxage=300, stale-while-revalidate=86400`, `ETag`, `Vary: Accept-Encoding`.

`/sites` and `/resolve` use a shorter policy (`max-age=30, s-maxage=60`) and a body-hash ETag. Public requests run in a read-only `REPEATABLE READ` transaction with a 5 s statement timeout, so every query in one response sees the same snapshot as the `content_version` in its ETag.

**URL scheme** (`modules/public/urls.py`): the default locale has no prefix (`/`, `/products`, `/products/{slug}`); other locales are prefixed (`/en`, `/en/products/{slug}`). Locale-neutral internal links in content blocks (`/products`) are prefixed by the API. hreflang values combine language and site country (`de-DE`, `en-DE`), plus `x-default`. A product shown in a fallback language canonicalizes to its original-language URL and is not listed under the fallback locale in the sitemap or hreflang.

### Admin (`/admin/v1`), Bearer JWT

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/login`, `GET /auth/me` |
| Websites | `GET /websites`, `POST /websites`, `GET/PATCH /websites/{id}`, `POST /websites/{id}/disable`, `POST /websites/{id}/enable`, `POST /websites/{id}/publish`, `GET /websites/{id}/publish-jobs` |
| Domains | `GET/POST /websites/{id}/domains`, `PATCH/DELETE /domains/{domain_id}`, `POST /domains/{domain_id}/verify`, `POST /domains/{domain_id}/make-primary` |
| Theme | `GET/PUT /websites/{id}/theme` (If-Match), `POST /websites/{id}/theme/assets` (multipart: `kind=logo\|logo_dark\|favicon\|og`) |
| Content | `GET /websites/{id}/content`, `GET/PUT /websites/{id}/content/{page}/{locale}` (If-Match) |
| SEO | `GET /websites/{id}/seo?locale=`, `PUT /websites/{id}/seo/{page}/{locale}` (+ `?product_id=`), `DELETE …` |
| Products | `GET /products?q=&status=&website_id=&page=`, `POST /products`, `GET/PATCH /products/{id}`, `POST /products/{id}/archive` |
| Translations | `GET /products/{id}/translations`, `PUT/DELETE /products/{id}/translations/{locale}` |
| Images | `POST /products/{id}/images` (multipart), `PATCH /images/{id}` (alt), `PUT /products/{id}/images/order`, `DELETE /images/{id}` |
| Site products | `GET /websites/{id}/products`, `PUT /websites/{id}/products/{product_id}` (assign / visibility / featured / position), `DELETE …`, `PUT /websites/{id}/products/order` (bulk reorder), `POST /websites/{id}/products/bulk-assign` |
| Users | `GET/POST /users`, `PATCH /users/{id}`, `PUT /users/{id}/websites` (admin only) |

**Authorization:**

| Role | Can do |
|---|---|
| `admin` | Everything |
| `catalog_manager` | Products, translations and images (global), plus read access to all sites |
| `site_editor` | Content, SEO, theme, domains (no verify) and product assignment, only for sites in `admin_user_websites` |

**Mutations:** every mutation writes `audit_log` and calls `publishing.mark_changed(...)` in the same transaction.

---

## 6. Media (images, logos)

**Upload flow:**
1. The admin client sends a multipart upload to the API. The API validates size (`UPLOAD_MAX_BYTES`) and opens the file with Pillow (`verify()`; allowed formats: JPEG, PNG, WebP, AVIF). It records width and height and strips EXIF/GPS.
2. The API calls `PutObject` to R2 with key `products/{product_id}/{uuidv7}.{ext}` and `Cache-Control: public, max-age=31536000, immutable`. Keys are never overwritten, so URLs can be cached forever.
3. The DB row is inserted. If the DB step fails, a compensating `DeleteObject` runs.

Uploads go through the API rather than browser-to-R2 presigned PUTs. This keeps validation server-side and avoids bucket CORS. Admin upload volume is tiny. Presigned uploads can be added later in `core/storage.py` with no schema change.

**Delivery:**
- The API returns `{key, url, width, height, alt}`, where `url = STORAGE_PUBLIC_BASE_URL/key`.
- The Next.js custom image loader builds `https://media.example.com/cdn-cgi/image/width=800,quality=80,format=auto/<key>` when the provider is `cloudflare`. Locally (`none`) it uses the raw SeaweedFS URL.
- Requires: R2 bucket with a custom domain on a Cloudflare zone, Image Transformations enabled on that zone, and allowed widths restricted in the loader to cap unique transformations.

**Deletion:** remove the DB row immediately, but delete objects only from a daily GC sweep, 7 days later (`deleted_media` table / soft-delete). Statically generated pages that are still cached never point at a missing image.

**SVG logos:** not allowed in v1, because they are an XSS risk. Revisit with sanitization if needed. The media domain is cookie-less and separate from the admin origin either way.

---

## 7. Static publishing and cache invalidation

**Frontend contract.** This plan assumes Next.js with ISR/on-demand revalidation: OpenNext on Cloudflare Workers, Vercel, or self-hosted. Every frontend `fetch` to this API carries tags:

| Tag | Covers |
|---|---|
| `site:{id}` | Everything for a site (config/theme/domains) |
| `site:{id}:home:{locale}` | Homepage |
| `site:{id}:products:{locale}` | Product listing |
| `site:{id}:product:{product_id}` | Product page |
| `sites` | The domain/site map |

**Change → affected pages** (`publishing/impact.py`, pure function, unit-tested):

| Change | Affected websites | Tags / paths |
|---|---|---|
| Website settings, theme, locales | that site | `site:{id}` (full) |
| Domain added/removed/primary changed | that site | `site:{id}` + `sites` |
| Home content (locale) | that site | `home:{locale}` |
| SEO row | that site | the page's tag |
| Product core/translation/image | **every site where it's assigned** (`ix_wp_product`) | `product:{pid}` + `products:{locale}` (+ `home` if featured) |
| Assignment/visibility/order | that site | `products:*`, `product:{pid}`, `home` if featured |

**Outbox flow:**
1. **Mark the change.** `mark_changed(website_ids, tags, paths)` runs inside the admin's DB transaction. It bumps `websites.content_version`, which invalidates the API ETag immediately. If `auto_publish`, it then **upserts** the site's single `pending` job: `INSERT … ON CONFLICT (website_id) WHERE status='pending' DO UPDATE SET tags = array(SELECT DISTINCT unnest(old||new)), run_after = now() + debounce`. Ten quick edits therefore produce one job. If the transaction rolls back, no job exists.
2. **Publish manually when needed.** Sites with `auto_publish=false` accumulate changes, and the editor clicks Publish (`POST /websites/{id}/publish`), which creates the job.
3. **Worker** (`python -m app.worker`):
   - Polls the queue and sleeps until the next job's `run_after` (at most 5 s). Every job is debounced, so `LISTEN/NOTIFY` would add a connection without making publishing faster.
   - Claims due jobs with `UPDATE … SET status='running' WHERE id IN (SELECT id … WHERE status='pending' AND run_after<=now() ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 10) RETURNING *`.
   - **Webhook:** `POST REVALIDATE_WEBHOOK_URL` with body `{job_id, website_id, website_key, hosts, locales, tags, paths, full_site, content_version}`. Headers: `X-Showcase-Timestamp` and `X-Showcase-Signature: v1=HMAC_SHA256(secret, timestamp + "." + body)`. Next.js verifies the signature (5-minute tolerance) and calls `revalidateTag` for each tag. `job_id` is the idempotency key.
   - **CDN purge** (if `CDN_PURGE_PROVIDER=cloudflare` and HTML is edge-cached): for each affected host, call the Cloudflare purge API on that domain's `cloudflare_zone_id`. It purges by URL list, or by hostname when `full_site` or more than 100 paths.
   - **Success:** `succeeded`, and `websites.published_version = job.content_version`, `published_at = now()`.
   - **Failure:** `attempts += 1`, `run_after = now() + backoff(2^n, max 15 min)`. After 8 attempts the job is `failed` and visible in the admin.
   - Stuck `running` jobs (worker crash) are reset by a timeout check.

Because `published_version < content_version` is visible per site, the admin UI can show "unpublished changes".

**Static-only frontends.** If a site is ever deployed as a pure `output: export` build (e.g. Cloudflare Pages), add a `websites.deploy_hook_url`. The worker then calls that hook instead (full-site rebuild for just that site). The rest is unchanged.

**Visitor traffic.** Visitors never hit the backend. Pages are served from the Next.js cache and the CDN. The API sees build and revalidation traffic only: roughly (sites × pages) requests per publish.

---

## 8. Performance

- **Avoid N+1.** Listings run one query: `website_products ⋈ products ⋈ product_translations (locale IN (req, default))`, with `DISTINCT ON` preferring the requested locale. Images load with a second `selectinload`/`IN` query, giving 2 queries per page regardless of size. The bundle endpoint builds from ~5 set-based queries.
- **Pagination.** `page/page_size` with `count(*) OVER ()`. Per-site product counts are small (<1–2k), so offset is fine. The `(website_id, position, product_id)` index makes it index-ordered. The API contract allows switching to a cursor later.
- **ETag.**
  1. One indexed lookup fetches `(id, status, content_version)` by key.
  2. The ETag is `W/"{site_id}:{content_version}:{endpoint}:{hash(query params)}"`.
  3. On an `If-None-Match` match, return 304 and skip the content queries entirely.
  4. Otherwise run them and serialize with `orjson`.
- **Connection pool.** Async SQLAlchemy pool sized via env (`DB_POOL_SIZE`). `pool_pre_ping`. Statement timeout of 5 s for public routes.
- **Future caching.** Public read paths go through a `SiteReadService` with pure inputs (site, version, locale, params). An in-process or Redis cache can later be keyed on `content_version` with no invalidation logic, because the version bump is the invalidation.
- **DB settings.** `pg_stat_statements` is enabled in prod to find slow queries, and `log_min_duration_statement=500ms`.

---

## 9. Docker environments

### Local (`docker-compose.yml`)

| Service | Purpose | Host port |
|---|---|---|
| `db` | Postgres 18.6 (+ `showcase_test` DB via init script) | `${DB_HOST_PORT:-5432}` |
| `storage` | SeaweedFS 4.53, S3 API (R2 stand-in) | 8333 (S3 + public URLs), 9333 (UI) |
| `storage-init` | One-shot: create `media` bucket (idempotent) | — |
| `migrate` | One-shot `alembic upgrade head` | — |
| `api` | uvicorn `--reload`, source bind-mounted | 8000 (`/docs`) |
| `worker` | Publish worker with auto-restart on code change | — |
| `webhook-sink` | Echo server standing in for Next.js `/api/revalidate` (`docker compose logs -f webhook-sink`) | 8081 |
| `adminer` | DB UI (`--profile tools`) | 8082 |

The infrastructure part (db, storage, storage-init, bucket permissions, test DB) has been started and checked.
`api`, `migrate` and `worker` need the Phase 1 scaffold (`pyproject.toml`, `uv.lock`, `app/`) before they can build.

### Production (`docker-compose.prod.yml` + `.env.prod`)

| Service | Notes |
|---|---|
| `db` | Postgres 18.6 tuned for a 4 GB VM, no host port. *Alternative:* managed Postgres. Delete the service and point `DATABASE_URL` at it |
| `db-backup` | Daily `pg_dump`, keeps 7 daily, 4 weekly and 6 monthly backups in `./backups`. **Also** sync `./backups` off-box (e.g. a cron'd `rclone sync` to a private R2 bucket) and test restores |
| `migrate` | Runs before `api`/`worker` start; deploy = `pull && up -d` |
| `api` | Prod image target: non-root, 4 uvicorn workers, healthcheck, `--proxy-headers` (real IP from `CF-Connecting-IP`) |
| `worker` | Same image, `python -m app.worker`. Safe to run more than one (SKIP LOCKED) |
| `cloudflared` | Cloudflare Tunnel → `http://api:8000`. Put Cloudflare Access/WAF rules on `/admin/*` and a rate limit on `/admin/v1/auth/login` |

**Not in prod compose:**
- Object storage: R2 is managed.
- Next.js: hosted separately.
- Redis: not needed, as required.

**Image versioning:** CI builds `backend/Dockerfile --target prod`, pushes `API_IMAGE:API_TAG` (date-sha), and the VM pulls it.

---

## 10. Cross-cutting

- **Config.** `pydantic-settings` `Settings` loads env vars only, and the app fails fast at startup if a required variable is missing. `APP_ENV=local|test|production` toggles docs exposure and log format.
- **Logging.** `structlog` writes JSON in prod and console output locally. Middleware binds `request_id` (from `CF-Ray` or a generated one), method, path, status and duration. `website_id` is bound when resolved. Secrets are never logged.
- **Errors.** `AppError(code, status, detail)` subclasses (`NotFound`, `Conflict`, `Forbidden`, `ValidationFailed`, `PreconditionFailed`) map to problem+json. Unhandled errors return 500 with a `request_id` and are logged with a stack trace. DB `IntegrityError` maps to 409 with the constraint name.
- **Security.**
  - Passwords: Argon2id hashes, with lockout after 5 failed logins.
  - Tokens: JWT (HS256, 30 min). Refresh tokens are deferred until an admin UI needs them.
  - CORS is limited to the admin origin. Public routes are GET-only.
  - Rich text is sanitized with `nh3`, and request body sizes are capped.
- **Health.** `/health/live` (process) and `/health/ready` (DB `SELECT 1`).

---

## 11. Testing

- **Real Postgres.** Tests run against the `showcase_test` DB (created by the init script). Alembic migrates once per session, and each test runs inside a transaction that is rolled back.
- **HTTP layer.** `httpx.AsyncClient(transport=ASGITransport(app))`.
- **Storage.** A `FakeStorage` handles unit and API tests. One integration test runs against SeaweedFS (`-m integration`).
- **Webhook and Cloudflare calls.** Mocked with `httpx.MockTransport`.
- **Core cases:**
  - Host normalization (case, port, IDN, trailing dot); alias redirect; pending or disabled domain → 404; one primary per site.
  - Tenant isolation: site A never sees site B's data.
  - Locale fallback and the rule that a product can't be made visible without a default-locale translation.
  - Product listing: ordering, visibility, pagination, query count (assert ≤ 3 queries via a SQLAlchemy event counter).
  - ETag: 304 on match, and a new ETag after any change, including a shared product edited via another site.
  - Impact mapping: a product edit enqueues jobs for exactly the sites that use it. Debounce merges into one pending job.
  - Worker: success sets `published_version`, failure backs off, the HMAC signature verifies.
  - Auth: role matrix and site scoping for `site_editor`.
  - Image upload: rejects non-images and oversized files, stores dimensions.
- **Commands.** `docker compose run --rm api pytest` runs the tests. Ruff and mypy run in CI.

---

## 12. Implementation phases

| Phase | Deliverable | Done when |
|---|---|---|
| 0 ✅ | Compose (local + prod), Dockerfile, SeaweedFS/R2 setup, env templates | Storage + DB start; bucket permissions verified |
| 1 ✅ | Skeleton: `pyproject`/`uv.lock`, settings, logging, errors, async DB, health, Alembic async env | `docker compose up` → `/docs` + `/health/ready` green |
| 2 ✅ | Models + initial migration + `cli seed` (2 sites, 3 locales, 10 products) + `cli create-admin` | Migration up/down clean; seed is idempotent |
| 3 ✅ | Tenant resolution + public read APIs + ETag/Cache-Control + bundle/sitemap | Public API tests incl. isolation + query count |
| 4 ✅ | Auth/roles + admin CRUD (websites, domains, theme, content, SEO, products, translations, assignments) + audit log | Admin API tests + role matrix |
| 5 ✅ | Media upload/validation/storage + GC sweep | Upload works against SeaweedFS; URLs served |
| 6 ✅ | Publishing: impact mapping, outbox/debounce, worker, signed webhook, CF purge | Edits show up as signed webhook calls in `webhook-sink` |
| 7 ✅ | Hardening: OpenAPI examples, CI (lint, type, tests, image build), backup/restore runbook, onboarding runbook "add a new website" | New site live with zero code changes |

**Phase 7 notes.**
- Hardening:
  - security headers, plus HSTS in production;
  - request body limits (413);
  - per-IP login rate limit (429 with `Retry-After`), on top of the per-account lockout and a Cloudflare WAF rule;
  - a password change revokes older tokens (`admin_users.password_changed_at`).
- CI (`.github/workflows/ci.yml`):
  - `uv lock --check`, ruff, mypy;
  - tests on the compose stack;
  - `alembic check` drift detection;
  - prod image smoke test (non-root, `/health/live`);
  - push to GHCR on `main` as `YYYY.MM.DD-sha7`.
- Runbooks in `docs/runbooks/`: `add-website.md`, `backup-restore.md` (with `infra/scripts/restore-drill.sh`), `deploy.md`.
- Acceptance: `tests/admin/test_onboarding_e2e.py` takes a site from creation to a live public bundle and sitemap using only API calls.
- New CLI commands: `publish-all` (after a restore) and `openapi` (spec dump).

---

## 13. Open questions (they change details, not the architecture)

1. **Next.js hosting:** OpenNext on Cloudflare Workers, Vercel, or self-hosted? This decides the webhook receiver and whether the Cloudflare HTML purge is needed.
2. **Admin UI:** is it in scope? Who builds it? This sets the CORS origin and whether refresh tokens and presigned uploads are worth adding.
3. **Locales:** one locale per site (typical: country site = one language) or multiple per site? The schema supports both. This affects URL structure (`/de/...` prefixes).
4. **Domains:** does each site live on its own Cloudflare zone? This decides whether `cloudflare_zone_id` is filled per domain or is one shared zone with subdomains.
