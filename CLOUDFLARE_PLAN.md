# Cloudflare Setup — End-to-End Plan

Target: **backend on Hetzner** (API + worker + Postgres via `docker-compose.prod.yml`) and **everything else on
Cloudflare**: DNS, TLS, CDN, WAF, R2 images, Image Transformations, the Next.js frontend on Workers (OpenNext),
the Tunnel to the backend, and Access. Built for 100+ country domains in **one Cloudflare account**.

Read first: `PLAN.md` (backend), `FRONTEND_PLAN.md`, `docs/runbooks/deploy.md`, `docs/runbooks/add-website.md`,
`docs/frontend-revalidation.md`, `.env.prod.example`.

---

## 0. Ground rules for the agent

- **Infrastructure as code.** Manage Cloudflare with Terraform (`cloudflare/cloudflare` provider, pinned version) in `infra/cloudflare/`. Deploy the frontend Worker with `wrangler`. Use the dashboard only for things the API can't do, and record each one in §13.
- **Secrets never go into git.** Terraform state holds secrets, so keep it in a private R2 bucket (S3 backend) or Terraform Cloud, never committed. Tokens go into `.env.prod` on the VM, Worker secrets, or CI secrets.
- **Least privilege.** Use one API token per consumer (§2) and no Global API Key.
- **Ask the user** for: the Cloudflare account ID, the infra domain name (written `yourcompany.com` below), the registrar access for each country domain, the Hetzner VM's IPv4 and IPv6 addresses, and the staff email domain or IdP for Access.
- **Things marked _verify_** depend on the current Cloudflare plan or limits, or on the OpenNext version. Check them against the current Cloudflare docs before relying on them, and record the result here.

---

## 1. Target topology

```
                        ┌──────────────────────── Cloudflare account ─────────────────────────┐
 visitor ──► example.de ─► WAF ─► Worker "showcase-frontend" (OpenNext)                        │
             example.fr          │   ├─ R2 "showcase-next-cache"    (ISR page/data cache)       │
             … 100 zones         │   ├─ Durable Objects             (tag cache, revalidation)   │
                                 │   └─ fetch ─► api.yourcompany.com/public/v1  (Access svc token)
 visitor ──► media.yourcompany.com ─► /cdn-cgi/image/… ─► R2 "showcase-media" (public, custom domain)
 backend ──► revalidate.yourcompany.com/api/revalidate ─► WAF (VM IP only) ─► same Worker      │
 staff   ──► api.yourcompany.com/admin, /docs ─► Access (SSO) ─► Tunnel ───────────┐            │
                        └────────────────────────────────────────────────────────── │ ──────────┘
                                                                                     ▼
                                     Hetzner VM (no inbound ports): cloudflared ─► api:8000, worker, db
                                     worker ─► R2 S3 API (uploads), Cloudflare API (purge), webhook ─┘
```

Hostnames on the infra zone `yourcompany.com`:

| Host | Points to | Purpose |
|---|---|---|
| `api.yourcompany.com` | Tunnel → `http://api:8000` | Public API (read by the frontend) + admin API |
| `media.yourcompany.com` | R2 custom domain → `showcase-media` | Images, resized at the edge |
| `revalidate.yourcompany.com` | Worker custom domain → `showcase-frontend` | Webhook receiver only |
| `api-staging.`, `media-staging.`, `*-staging.yourcompany.com` | Staging equivalents (§10) | Staging |

---

## 2. API tokens (create first)

| Token | Permissions | Used by | Stored in |
|---|---|---|---|
| `terraform` | Account: Workers R2, Workers Scripts, Access (apps/policies/service tokens), Cloudflare Tunnel, Account Settings read. Zone (all zones): Zone edit, DNS edit, Zone Settings edit, WAF/Firewall edit, Cache Rules edit, SSL edit, Workers Routes edit | Terraform (operator only) | Operator's password manager |
| `wrangler-ci` | Workers Scripts edit, Workers R2 edit, Durable Objects (per wrangler docs), Workers Routes / custom domains, Account read. _verify_ the exact set for the wrangler version | Frontend CI deploy | CI secret |
| R2 `showcase-media-rw` | R2 **Object Read & Write, bucket `showcase-media` only** | Backend (`STORAGE_ACCESS_KEY_ID/SECRET`) | `.env.prod` |
| R2 `showcase-backups-rw` | R2 Object Read & Write, bucket `showcase-backups` only | `rclone` on the VM | VM `rclone.conf` (mode 600) |
| `backend-purge` | Zone → **Cache Purge** on all zones (only if §8 enables purge) | Backend worker (`CLOUDFLARE_API_TOKEN`) | `.env.prod` |
| Tunnel token | (generated per tunnel) | `cloudflared` (`CLOUDFLARE_TUNNEL_TOKEN`) | `.env.prod` |
| Access service token `frontend-public-api` | (generated) | Frontend Worker → public API | Worker secrets |

---

## 3. Infra zone `yourcompany.com`

1. Add the zone. Use the **Pro plan** if you want more than one rate-limiting rule (admin login + webhook) and WAF managed rules. _Verify_ what the Free plan includes. Point the nameservers at Cloudflare.
2. Settings:
   - SSL/TLS **Full (strict)**, Always Use HTTPS, minimum TLS 1.2, TLS 1.3, HTTP/3.
   - DNSSEC on (add the DS record at the registrar).
   - HSTS: start at 6 months, no preload.
3. Disable features that rewrite HTML or responses: **Rocket Loader off, Email Obfuscation off**. They break JSON-LD and SEO markup and don't apply to JSON anyway.
4. Universal SSL covers `yourcompany.com` and `*.yourcompany.com` (one level only). That's why the staging hosts in §10 are named `x-staging.yourcompany.com` and not `x.staging.yourcompany.com`.

---

## 4. R2

| Bucket | Access | Settings |
|---|---|---|
| `showcase-media` | **Public via custom domain `media.yourcompany.com` only.** `r2.dev` public URL **disabled** | No CORS (uploads go through the API). No lifecycle rule: the backend's own cleanup job deletes images (`MEDIA_RETENTION_DAYS`) |
| `showcase-backups` | Private | Lifecycle rule: delete objects older than 180 days, matching the `db-backup` retention |
| `showcase-next-cache` | Private, bound to the Worker | OpenNext incremental cache |
| `showcase-tfstate` | Private | Terraform state (optional, if not using Terraform Cloud) |
| `*-staging` copies | As above | Staging (§10) |

Location hint: EU (`weur`) for media and backups. The media domain is cached at the edge anyway.

**Media caching:** the backend uploads objects with `Cache-Control: public, max-age=31536000, immutable`. Add a
Cache Rule on `media.yourcompany.com`: *eligible for cache, respect origin headers*. _Verify_ `cf-cache-status: HIT` on
a second request.

---

## 5. Image Transformations

1. Enable Transformations on the zone `yourcompany.com`, and allow them **only for images from this zone** (not "any origin").
2. The URL format the frontend loader produces: `https://media.yourcompany.com/cdn-cgi/image/width=640,quality=75,format=auto/<key>`.
3. Cost control: every unique width × image counts as a transformation.
   - The frontend restricts widths to about 6 values (`FRONTEND_PLAN.md` §6.4).
   - Optional: a WAF custom rule blocking `/cdn-cgi/image/` requests whose `width=` isn't in the allowed set. _Verify_ that regex matching is available on the plan.
   - Set a billing notification for Images usage. _Verify_ the free allowance and price per 1,000 transformations.
4. Backend env: `IMAGE_TRANSFORM_PROVIDER=cloudflare`, `STORAGE_PUBLIC_BASE_URL=https://media.yourcompany.com`. Frontend: `NEXT_PUBLIC_IMAGE_PROVIDER=cloudflare` (needed at build time).

---

## 6. Tunnel + Access (backend ingress)

### 6.1 Tunnel
- Create a remotely managed tunnel `showcase-prod`. Public hostname `api.yourcompany.com` → service `http://api:8000`. The `cloudflared` container is already in `docker-compose.prod.yml`.
- The token goes into `.env.prod` as `CLOUDFLARE_TUNNEL_TOKEN`.
- The VM needs **outbound** access to Cloudflare on port 7844 (TCP + UDP). The Hetzner Cloud Firewall allows outbound by default.
- **Inbound: block everything** except SSH from the operator's IP.
- Optional: a second replica of `cloudflared` for resilience.

### 6.2 Access applications

| App | Path | Policy |
|---|---|---|
| `showcase-admin` | `api.yourcompany.com/admin/*`, `/docs`, `/openapi.json` | Allow: staff emails or IdP group. Plus a **Service Auth** policy with a separate service token for automation (runbook curl scripts, CI) |
| `showcase-public-api` | `api.yourcompany.com/public/*` | **Service Auth** only: service token `frontend-public-api` |
| `showcase-health` | `api.yourcompany.com/health/*` | Service Auth (monitoring) or bypass. Decide with the user |

- The frontend sends `CF-Access-Client-Id` and `CF-Access-Client-Secret` on every API call. This is a frontend code change, listed in §12.
- If an admin UI is later hosted on another origin, configure Access CORS for that app, and keep `CORS_ALLOW_ORIGINS` in the backend in sync.

### 6.3 Caching and WAF on `api.yourcompany.com`
- Cache Rule: `starts_with(http.request.uri.path, "/public/v1/")` → eligible for cache, respect origin `Cache-Control` (`s-maxage=300` content, `60` routing), and keep the query string in the cache key. **Bypass cache for `/admin/*`.**
- _Verify_ that responses on Access-protected paths are still served from cache (`cf-cache-status: HIT` with the service-token headers). If not, drop the `showcase-public-api` Access app and rely on WAF + rate limit instead. The data is public anyway.
- WAF rate limit (already in `deploy.md`): `POST /admin/v1/auth/login`, 10 per minute per IP → block for 10 minutes.
- WAF managed rules on (Pro).

---

## 7. Frontend Worker (OpenNext)

### 7.1 Proof of concept (do this before §9)
Deploy the frontend to a `*.workers.dev` URL plus two test hostnames, and confirm:
- the adapter supports the pinned Next.js version, including the host-routing middleware/proxy (`FRONTEND_PLAN.md` §3);
- `revalidateTag` from `/api/revalidate` really refreshes a page across requests (tag cache working);
- the bundle size is within the Workers limit;
- the `next/image` custom loader works.

**If any of these fails, stop and report.** The fallback is Next.js standalone in Docker on the Hetzner VM behind the same Tunnel, which changes §7–§9.

### 7.2 Worker configuration (`wrangler.jsonc` in the frontend repo)
- Name `showcase-frontend` (and `showcase-frontend-staging`). `compatibility_flags: ["nodejs_compat"]`, with a pinned `compatibility_date`.
- Bindings, following the OpenNext Cloudflare docs for the pinned adapter version:
  - R2 incremental cache → `showcase-next-cache`;
  - tag cache (Durable Object-based, recommended for on-demand `revalidateTag`; _verify_ the option name);
  - revalidation queue (Durable Object);
  - optional regional cache.
- Vars: `SHOWCASE_API_URL=https://api.yourcompany.com/public/v1`, `APP_ENV=production`, `HOST_MAP_TTL_SECONDS=60`, `TRUST_FORWARDED_HOST=false`.
- Secrets (`wrangler secret put`): `REVALIDATE_WEBHOOK_SECRETS` (comma-separated list, for rotation), `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`.
- Observability: Workers Logs on, with head sampling as needed.

### 7.3 Hostnames
- `revalidate.yourcompany.com` → Worker **custom domain**.
- Each site's apex and `www` → Worker **custom domains** (§9). Cloudflare creates the DNS records and certificates.
- _Verify_ the custom-domain limit per Worker/account for ~200 hostnames (100 sites × apex + www). If it's too low, the fallback per zone is a proxied placeholder DNS record (`AAAA @ 100::`) plus a **Worker route** `example.de/*`.

### 7.4 Webhook endpoint protection (zone `yourcompany.com`)
- WAF custom rule: `http.host eq "revalidate.yourcompany.com" and not ip.src in {<VM IPv4> <VM IPv6>/64}` → **Block**. Include IPv6: the VM may connect to Cloudflare over IPv6.
- Rate limit: 60 requests per minute on `revalidate.yourcompany.com` → block. Requires a second rate-limit rule (Pro).
- The frontend code must also return 404 for `/api/revalidate` on every host except `revalidate.yourcompany.com` (§12).

---

## 8. Backend production env (`.env.prod` on the VM)

| Var | Value |
|---|---|
| `STORAGE_ENDPOINT_URL` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` (EU jurisdiction buckets: `https://<ACCOUNT_ID>.eu.r2.cloudflarestorage.com`) |
| `STORAGE_REGION` | `auto` |
| `STORAGE_ACCESS_KEY_ID` / `STORAGE_SECRET_ACCESS_KEY` | R2 token `showcase-media-rw` |
| `STORAGE_BUCKET` | `showcase-media` |
| `STORAGE_PUBLIC_BASE_URL` | `https://media.yourcompany.com` |
| `IMAGE_TRANSFORM_PROVIDER` | `cloudflare` |
| `REVALIDATE_WEBHOOK_URL` | `https://revalidate.yourcompany.com/api/revalidate` |
| `REVALIDATE_WEBHOOK_SECRET` | `openssl rand -hex 32`, also in the Worker's `REVALIDATE_WEBHOOK_SECRETS` |
| `CDN_PURGE_PROVIDER` | See below |
| `CLOUDFLARE_API_TOKEN` | `backend-purge` (only if purge is enabled) |
| `CLOUDFLARE_TUNNEL_TOKEN` | Tunnel token |
| `CORS_ALLOW_ORIGINS` | Admin UI origin, if any |

**CDN purge decision.** With the frontend on Workers, HTML isn't stored in the zone's CDN cache: OpenNext
caches it in R2 and Durable Objects, and the webhook invalidates that. So start with **`CDN_PURGE_PROVIDER=none`**.
Enable `cloudflare` only if an HTML Cache Rule or the OpenNext regional cache is added in front of the Worker. In that
case _verify_ that purge by hostname is available on the zones' plan and check the purge rate limits. Either way, fill
in `cloudflare_zone_id` on every domain record, so purge can be switched on later without data work.

---

## 9. Per-country site zone (repeat for each domain; first for the two launch sites)

Turn this into a Terraform module `site_zone` looped over `var.sites` (`{ domain, aliases[], website_key }`).
The list must match the domains registered in the backend.

1. **Zone:** add `example.de` on the Free plan. Output the assigned nameservers. The **user** changes the NS at the registrar. Wait for the zone to become `active`. _Verify_ any limit on adding many zones quickly; add in batches if needed.
2. **Zone settings** (same as §3):
   - Always Use HTTPS, minimum TLS 1.2, TLS 1.3, HTTP/3, DNSSEC.
   - Rocket Loader off, Email Obfuscation off.
   - HSTS 6 months, once HTTPS is confirmed.
   - Bot Fight Mode: leave off, or confirm verified bots (Googlebot, Bingbot) are never challenged. Crawlability is the top priority.
   - AI-crawler blocking: the user's choice.
3. **Backend domain verification:** create the TXT record `_showcase-verify.example.de` = the value from `POST /admin/v1/websites/{id}/domains`. Then `POST /domains/{id}/verify`.
4. **Frontend:** attach `example.de` and `www.example.de` as Worker custom domains (§7.3). Don't add A/AAAA/CNAME records for them by hand. Keep any existing MX/TXT mail records.
5. **Backend data:** set `cloudflare_zone_id` on each domain record, then enable the site (`add-website.md` §6).
6. **Search Console:** the user adds the domain property; the agent can create the verification TXT record. Submit `https://example.de/sitemap.xml`.

Aliases like `www` are redirected to the primary host by the frontend middleware (data-driven,
`redirect_to_primary`). Don't add Cloudflare Redirect Rules for them, so there's a single source of truth.

---

## 10. Staging

- A separate backend on the same VM (second compose project, own DB and `.env.staging`) or on a second small VM.
- Its own tunnel hostname `api-staging.yourcompany.com`, bucket `showcase-media-staging` → `media-staging.yourcompany.com`, Worker `showcase-frontend-staging`, and its own webhook secret and service token.
- Site hosts: `<website-key>-staging.yourcompany.com`, covered by Universal SSL.
  - Use a single Worker route `*-staging.yourcompany.com/*` with a proxied wildcard DNS record, so staging needs no per-site Cloudflare work. _Verify_ that wildcard routes accept that pattern; otherwise use `*.yourcompany.com/*` with a hostname check in the Worker.
  - Register these hosts as active domains in the **staging** backend.
- `APP_ENV=staging` → the frontend emits `noindex` and disallows everything in `robots.txt`. Also protect the staging hosts with Access (staff only) so crawlers never see them.

---

## 11. Monitoring and notifications

Set Cloudflare notifications by email (and Slack if available) for:
- **Tunnel health:** `showcase-prod` down or degraded.
- **Worker errors:** spike in `showcase-frontend` error rate.
- **Usage:** R2 storage and operations thresholds; Images transformation usage; billing usage.
- **Security:** WAF events spike on `revalidate.` and on the login rule.
- **Certificates:** SSL/TLS certificate events on the site zones.

Outside Cloudflare (backend): watch for publish jobs in `failed` state (`GET /admin/v1/websites/{id}/publish-jobs`), for example with a small cron on the VM that alerts. Add it to `deploy.md`.

---

## 12. Code changes outside Cloudflare (for the respective agents)

| Repo | Change |
|---|---|
| Backend | In production, require `REVALIDATE_WEBHOOK_URL` to start with `https://` (`app/core/config.py` validator) |
| Backend | `docs/runbooks/add-website.md` §0/§3: replace "CNAME → frontend host" with "attach Worker custom domains (`terraform apply` for the new site entry)" and "set `cloudflare_zone_id`" |
| Backend | `deploy.md`: link to this plan; add the backup sync to R2 (`rclone sync ./backups r2:showcase-backups`, daily cron) |
| Frontend | Send the `CF-Access-Client-Id` / `CF-Access-Client-Secret` headers on every API fetch |
| Frontend | Accept `REVALIDATE_WEBHOOK_SECRETS` (a list); serve `/api/revalidate` only on `revalidate.yourcompany.com`, 404 elsewhere |
| Frontend | `wrangler.jsonc` + OpenNext config + CI deploy job (`wrangler deploy`, staging on every push to main, production on tag) |

---

## 13. Execution order and done criteria

| # | Step | Done when |
|---|---|---|
| 1 | Tokens (§2), Terraform skeleton + remote state | `terraform plan` runs clean |
| 2 | Infra zone (§3) | Zone active, settings applied |
| 3 | R2 buckets + media custom domain (§4) | `curl -I https://media.yourcompany.com/<test-key>` → 200, `cache-control: …immutable` |
| 4 | Image Transformations (§5) | `/cdn-cgi/image/width=640,format=auto/<key>` → 200, `content-type: image/avif` or `webp` |
| 5 | Tunnel + Access + API cache (§6) | Public API without the service token → 403; with it → 200 and a cache HIT on repeat; `/admin` needs SSO |
| 6 | Backend env (§8) + deploy on Hetzner | `/health/ready` OK via Tunnel; an image upload in the admin appears on `media.` |
| 7 | Frontend proof of concept (§7.1) | All four checks pass, or the fallback is decided with the user |
| 8 | Worker + `revalidate.` + WAF (§7.2–7.4) | Webhook from the VM → 200; from any other IP → 403; bad signature → 401 |
| 9 | Two launch site zones (§9) | Both domains serve their own site over HTTPS; `www` → 308 to apex |
| 10 | Staging (§10) | Staging sites reachable for staff only, `noindex` |
| 11 | Monitoring (§11) | Test notification received |
| 12 | **End-to-end check** | See below |

**End-to-end acceptance:**
1. `https://<de-domain>/` and `https://<fr-domain>/` render their own theme, language and products. `view-source` shows full HTML, canonical, hreflang (DE only) and JSON-LD.
2. `https://<de-domain>/sitemap.xml` and `/robots.txt` are correct; staging `robots.txt` disallows everything.
3. Rename a product shown on both sites through the admin API. Within about 1 minute (30 s debounce + webhook), both sites show the new name. The publish job is `succeeded`.
4. Images load from `media.yourcompany.com/cdn-cgi/image/…` in AVIF/WebP, with a cache HIT on reload.
5. Stop the backend (`docker compose stop api`). Both sites keep serving pages and images. Start it again.
6. Lighthouse (mobile) ≥ 90 on home and products pages for both sites.
7. Restore drill: the latest dump in `showcase-backups` restores with `infra/scripts/restore-drill.sh`.

Write the results, every _verify_ outcome, and any dashboard-only steps into `docs/runbooks/cloudflare.md`
when finished. That becomes the operational runbook. This plan file can then be deleted.
