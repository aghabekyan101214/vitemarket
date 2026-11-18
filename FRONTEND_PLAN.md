# Multi-Country Showcase Frontend — Implementation Plan

Next.js (App Router) · React Server Components · TypeScript (strict) · Tailwind CSS v4 · ISR with on-demand tag revalidation

Goal: one Next.js codebase serving every showcase site (2 now, 30–100+ later). The incoming host picks the
site. All content, theme tokens, SEO and products come from the existing backend (`/public/v1`, see
`PLAN.md`). **A new site that reuses an existing design needs no frontend code change and no redeploy.**

This plan is written against the backend as it exists in this repo. Where the FE brief and the backend
disagree, the backend contract wins and the difference is listed in §1.

---

## 0. Key decisions (TL;DR)

| Topic | Decision | Why |
|---|---|---|
| Framework | Next.js 16 App Router, RSC by default, pin exact version | Static HTML, built-in ISR and tag revalidation |
| Routing | `proxy.ts` (`middleware.ts` on Next ≤15) maps host → site and rewrites `/products` → `/{siteKey}/{locale}/products` | One app, many domains. A site can't reach another site's pages |
| Route tree | `app/[site]/[locale]/…` (**not** `_sites`, because `_`-prefixed folders are private in the App Router) | Root layout per site/locale, so `<html lang>` is always right |
| Host map | Runtime fetch of `GET /public/v1/sites`, kept in memory with a 60 s TTL and stale fallback | New domains go live without a rebuild |
| Data | Typed client over `/public/v1/sites/{key}/{config,home,products,seo,sitemap}`; every fetch tagged per `docs/frontend-revalidation.md` | Granular, per-site, per-locale cache invalidation |
| Caching | Pages fully static; `revalidate` on-demand via signed webhook `POST /api/revalidate`, 24 h time-based safety net | Edits show up in seconds. Visitors never hit the backend |
| Types | Generated from the backend OpenAPI spec (`openapi-typescript`, dev-only), committed, drift-checked in CI | One source of truth, no hand-written DTOs |
| Theme | Backend tokens → CSS variables in a server `<style>` tag → Tailwind `@theme` utilities | Colors, radius and fonts per site without a rebuild |
| Design variants | Backend enums (`header_style`, `product_card_style`) + a frontend **design registry** (component overrides per design) | Sites can differ in layout and structure, not only in color |
| Home composition | Backend `blocks[]` (hero, text, features, featured_products, cta) rendered by a block registry, in backend order | Each site composes its home page from data |
| Fonts | `next/font/google` registry of the families in use. A backend family missing from the registry falls back to a system stack and logs a warning | Self-hosted, no layout shift. A new font is a one-line change |
| Images | `next/image` with a custom `loaderFile`: Cloudflare `/cdn-cgi/image/…` in production, raw URL locally | No Next image optimizer to run, cacheable at the edge |
| UI strings | Small typed dictionaries per language for interface text (`Skip to content`, empty state, …). All content comes from the backend | The backend has no UI-strings endpoint (§1) |
| Mock adapter | `ApiClient` interface with `http` and `mock` implementations. Fixtures are recorded from the seeded backend | FE dev and tests work without Docker, and fixtures can't drift from the real contract |
| Tests | Vitest (unit + component) + Playwright smoke against mock mode on `*.localhost` hosts | Covers domain resolution, theme selection and rendering, as required |

---

## 1. Backend contract vs FE brief: gaps and decisions

| # | FE brief says | Backend reality | Decision for v1 |
|---|---|---|---|
| 1 | `/api/websites/{website_id}/…` | `/public/v1/sites/{key}/…`, addressed by **`key`**. `id` is used only in cache tags | Use the real paths. The client takes `SiteRef {id, key}` |
| 2 | Product categories (highlights + filtering) | **No categories** in the schema or API | Don't build them. The design registry leaves a slot. If the designs need categories, the backend adds `categories` + `product.category_ids` first |
| 3 | No product detail pages | Backend serves `/products/{slug}`, the product SEO, and **lists product URLs in the sitemap**; `ProductSummary.path` points there | No detail route. Product cards don't link anywhere. **The sitemap route filters to home and products paths** so no 404 URLs are submitted. Adding the route later is one page, because the data, SEO, tags and hreflang already exist |
| 4 | "Company introduction" section | There's no dedicated block. `text` (sanitized HTML) and `features` exist | Company intro = `text` block (+ optional `features`). The design decides how a `text` block looks |
| 5 | Hero images, descriptive alt | `HeroBlock.image_key` has **no alt and no dimensions** | Build the URL from `config.media.base_url` + key. Render in a fixed-aspect container (`fill` + `sizes`, no CLS) with `alt=""` (decorative). Ask the backend for `image_alt` (localized) |
| 6 | No hardcoded UI strings | Backend provides content, nav labels, SEO; no interface strings or footer content | FE dictionaries per **language** (`de`, `en`, `fr`) for interface text only, with `en` as fallback. Footer text: site name + © year + nav. **Legal pages (DE Impressum/Datenschutz) are needed for DE but are not in the backend or in scope.** Flag to the product owner |
| 7 | Per-site designs | `ThemeTokens` has `header_style` and `product_card_style`, but **no design/template field** (`extra="forbid"`, so the FE can't add one) | FE `config/designs.ts`: `siteKey → designId`, default `base`. Ask the backend to add `tokens.design: str` so this moves to data |
| 8 | Fonts from backend | Arbitrary family names at runtime. `next/font` needs them at build time | Font registry (§6.3). An unknown family → system stack + build warning |
| 9 | Locale-specific `hreflang` | Backend emits hreflang **within one site** (`de-DE`, `en-DE`, `x-default`), never across sites (DE ↔ AT) | Use as is. This is correct: no hreflang between pages that aren't real equivalents. Cross-country alternates would need a backend "site group" concept later |
| 10 | Staging domains | Domains must be `active` to resolve. Seed has a `pending` staging host | Staging runs against a **staging backend** with its own active staging domains. Additionally, `APP_ENV != production` allows env host aliases (§3.4). Staging is always `noindex` |
| 11 | Category filtering on `/products` | Pagination only (`page`, `page_size ≤ 100`) | No filter UI. Render all visible products on one static page (loop pages of 100). Add `/products/page/[n]` only if a site goes over ~96 products |
| 12 | `features[].icon` | Free string, max 50 chars | FE maps a small fixed set of names to inline SVGs. Unknown → no icon |

**No Figma designs are in this repo.** Sections 6–7 define the variant mechanism. The concrete component
variants (Design A / Design B) are filled in once the designs are shared. Map every design section to a
backend block type before building it. A section with no block type is a backend change, not a hardcoded FE section.

---

## 2. Project structure

```
src/
  proxy.ts                         host → site → rewrite (middleware.ts on Next ≤15)
  app/
    [site]/
      [locale]/
        layout.tsx                 root layout: <html lang>, theme <style>, fonts, Header/Footer
        page.tsx                   home
        products/page.tsx          products listing
        not-found.tsx, error.tsx   localized states
        loading.tsx                skeleton (only seen on a first, uncached render)
      sitemap.xml/route.ts         per-host sitemap (filtered, see §1 #3)
      robots.txt/route.ts          per-host robots
    api/revalidate/route.ts        signed webhook → revalidateTag
    global-not-found.tsx           unmatched route outside a site (Next ≥15.4)
  components/
    common/        Container, SectionHeading, SkipLink, JsonLd, RichText, Icon
    layouts/       Header (+ variants: minimal | centered | split), Footer, Nav, LanguageSwitcher
    sections/      HeroSection, TextSection, FeaturesSection, FeaturedProducts, CtaSection, BlockRenderer
    products/      ProductCard (+ plain | bordered | elevated), ProductGrid, EmptyState
  designs/
    registry.ts    designId → DesignModule (partial component overrides on top of base)
    base/          default implementations (= components/*)
    design-a/      overrides from Figma A (e.g. its own Hero, Header)
    design-b/      overrides from Figma B
  config/
    designs.ts     siteKey → designId (until the backend has tokens.design)
    fonts.ts       next/font registry: family name → font object
    env.ts         typed, validated env (server-only)
  lib/
    api/
      client.ts    ApiClient interface + getApiClient() (http | mock)
      http.ts      fetch wrapper: base URL, tags, timeout, problem+json errors
      mock.ts      reads fixtures
      tags.ts      tag builders (single place, mirrors backend impact.py)
      fixtures/    JSON recorded from the seeded backend
    site/
      host-map.ts  fetch /sites, TTL cache, normalizeHost, lookup
      routing.ts   pathname → {locale, rest}; locale prefix rules (mirror of backend urls.py)
      context.ts   getSite(params) — React.cache'd: SiteRef + config + design
    seo/
      metadata.ts  Seo → Next Metadata
      sitemap.ts   entries → XML (with xhtml:link alternates)
    theme/
      css-vars.ts  ThemeTokens → CSS custom properties (validated)
      contrast.ts  WCAG contrast check (dev warning + test)
    i18n/
      messages/{en,de,fr}.ts   interface strings only
      t.ts                     getMessages(locale) with language fallback
    images/
      loader.ts    next/image loaderFile
  types/
    api.generated.ts   openapi-typescript output (do not edit)
    api.ts             friendly aliases: SiteConfig, HomePage, ProductSummary, ContentBlock …
tests/
  unit/  e2e/
```

Separation rules:
- `lib/api` is the only code that knows URLs and tags.
- `components/*` receive plain props, never fetch, and never check the site or country.
- `designs/*` only override components.
- `config/*` is the only place that names site keys.

---

## 3. Multi-domain routing

### 3.1 Host map (`lib/site/host-map.ts`)
- `GET {SHOWCASE_API_URL}/sites` → `Map<hostname, {siteKey, siteId, defaultLocale, locales, primaryHost, redirectToPrimary, isPrimary}>`.
- Module-level cache: 60 s TTL (the backend sends `s-maxage=60`). When the TTL expires, refresh in the background and keep serving the old map. If a refresh fails, keep the last good map and log. If there is no map at all (cold start + backend down) → 503 with `Retry-After`.
- `normalizeHost()` matches the backend's `core/hostnames.py`: trim, strip port, strip trailing dot, lowercase, punycode (`URL` / `domainToASCII`). Reject IPs and invalid hosts.

### 3.2 Proxy flow (every request except `/_next/*`, `/api/revalidate`, static assets)
1. **Host.** Read `Host`. Read `X-Forwarded-Host` only when `TRUST_FORWARDED_HOST=true` (set only behind a known proxy). Normalize.
2. **Look up.** **Exact match only.** No match → plain 404 response. Never fall back to a default site: that would serve one country's content on an unknown or hostile domain.
3. **Alias redirect.** If the host is an alias with `redirect_to_primary` → 308 to `https://{primaryHost}{path}{search}`.
4. **Locale.** First path segment ∈ `locales` and ≠ default → that locale, strip the prefix. `/{defaultLocale}/…` → 308 to the unprefixed path, to avoid duplicate URLs. Otherwise → default locale.
5. **Rewrite** to `/{siteKey}/{locale}{rest}`, and set request header `x-site-host` (used for canonical checks and robots).
6. Isolation comes from the rewrite itself: a request for `/nordic-home-fr/fr/products` on the DE host becomes `/nordic-home-de/de/nordic-home-fr/fr/products` → 404. The `[site]` segment can never be set from outside.

### 3.3 Server side
`getSite(params)` (wrapped in `React.cache`) validates `params.site`/`params.locale` against the host map once more, fetches `/config?locale=`, and resolves the design. Every API call takes the resolved `SiteRef`, never a raw param. A backend 404 (`site disabled`, `locale_not_supported`) → `notFound()`.

### 3.4 Local and staging
- Local: the seed's `nordic-home-de.localhost` / `nordic-home-fr.localhost` resolve to 127.0.0.1 in browsers out of the box → `http://nordic-home-de.localhost:3000`.
- `DEV_HOST_ALIASES="localhost=nordic-home-de,staging-de.example.dev=nordic-home-de"`: honored only when `APP_ENV !== "production"`. It is checked after the real map.
- Non-production always sends `X-Robots-Tag: noindex` and a disallow-all `robots.txt`.

---

## 4. Backend API integration

### 4.1 Endpoints used

| Where | Endpoint | Tags |
|---|---|---|
| proxy | `GET /sites` | (in-memory TTL; `sites` tag if fetched in RSC) |
| layout | `GET /sites/{key}/config?locale=` | `site:{id}` |
| home | `GET /sites/{key}/home?locale=` | `site:{id}`, `site:{id}:home:{locale}` |
| home meta | `GET /sites/{key}/seo?page=home&locale=` | `site:{id}`, `site:{id}:home:{locale}` |
| products | `GET /sites/{key}/products?locale=&page=&page_size=100` (loop until `total_pages`) | `site:{id}`, `site:{id}:products:{locale}` |
| products meta | `GET /sites/{key}/seo?page=products&locale=` | `site:{id}`, `site:{id}:products:{locale}` |
| sitemap | `GET /sites/{key}/sitemap` | `site:{id}` + every `:home:*` / `:products:*` tag of the site's locales |

Featured products are product data. A product edit sends `site:{id}:product:{pid}`, plus `products:*`, plus `home:*`
if the product is featured (backend `impact.py`). So the tags above are enough. `bundle` is not used: it pulls every
product SEO record, which pages without product detail don't need. It stays available for a future SSG export mode.

### 4.2 Client (`lib/api/http.ts`)
- `server-only` import guard. `SHOWCASE_API_URL` is a server env var and never reaches the browser.
- `fetch(url, { next: { tags, revalidate: 86400 }, signal: AbortSignal.timeout(8000) })`.
- Non-2xx → parse `application/problem+json` into `ApiError {status, code, detail}`. 404 → `NotFoundError` (pages call `notFound()`). Anything else → **throw**, so ISR keeps serving the last good page instead of caching an error page.
- Next request memoization dedupes identical calls in `generateMetadata` and the page.
- Pure tag builders in `lib/api/tags.ts`, unit-tested against the table in `docs/frontend-revalidation.md`.

### 4.3 Mock adapter
`ApiClient` interface: `sites()`, `config()`, `home()`, `products()`, `seo()`, `sitemap()`. `SHOWCASE_API_MODE=mock`
serves JSON fixtures recorded with a script (`scripts/record-fixtures.ts`) from the seeded local backend (2 sites,
3 locales). UI code imports only the interface, so switching adapters needs no component changes.

### 4.4 Revalidation webhook (`app/api/revalidate/route.ts`)
- Implement it exactly as in `docs/frontend-revalidation.md`: raw-body HMAC-SHA256, 300 s tolerance, `timingSafeEqual`, `full_site` → also revalidate `site:{id}`. Respond 2xx only after revalidating.
- On Next 16, `revalidateTag` takes a cache-life profile as its second argument. Use `'max'` (stale-while-revalidate), or immediate expiry if editors must see changes on the very next request. Check the signature against the pinned version.
- `sites` tag in the payload → also drop the in-memory host map (`hostMap.invalidate()`), so domain changes apply right away on that instance.
- Local: point the backend's `REVALIDATE_WEBHOOK_URL` at `http://host.docker.internal:3000/api/revalidate` instead of `webhook-sink`.
- **Hosting note:** `revalidateTag` must reach a **shared** cache. Vercel and OpenNext-on-Cloudflare provide one. A self-hosted multi-instance setup needs a shared `cacheHandler`. This is backend open question #1 (`PLAN.md` §13). Decide it before building §4.4.

### 4.5 States
| State | Handling |
|---|---|
| Loading | `loading.tsx` skeleton with the same grid geometry (no CLS). Rarely seen, because pages are static |
| Empty | Products: localized `EmptyState`. Home without a `featured_products` block or with none featured: section omitted |
| Error at revalidation | Throw → the previous static page stays live. Log `{siteKey, endpoint, status, code}` |
| Error on a cold render | `error.tsx` (localized, inside the site layout so branding stays) |
| Unknown block type | Skip and log. Forward-compatible with new backend block types |

---

## 5. Pages

### 5.1 Home (`[site]/[locale]/page.tsx`)
- `home.blocks` rendered in order by `BlockRenderer`, using the design registry's components.
- Heading hierarchy: the **first `hero.heading` is the `<h1>`**. If there's no hero, `home.title` is rendered as the `<h1>` (visually hidden if the design has no visible title). Block headings are `<h2>`, card titles `<h3>`.
- Hero image: `preload`/`priority`, `fetchPriority="high"`, `sizes="100vw"`, fixed aspect ratio per design. Other images lazy-load (the default).
- `featured_products` block → `home.featured_products` (already limited by the API) → `ProductGrid`.
- `text` block body is HTML already sanitized by the backend (`nh3`), rendered through `RichText` (`dangerouslySetInnerHTML`, prose styles). The backend sanitizer is the trust boundary.
- CTA links are already locale-prefixed by the backend. Internal links use `<Link>`, external ones `<a rel="noopener">`.

### 5.2 Products (`[site]/[locale]/products/page.tsx`)
- `<h1>` = `products.content.title`, intro = `content.subtitle`, then `content.blocks` (e.g. a `text` intro).
- `ProductGrid`: responsive 1 / 2 / 3 / 4 columns. `ProductCard` = `<article>` with image (`alt` from the API, which falls back to the product name), `<h3>` name, short description. **No link** (§1 #3).
- The first row of images loads eagerly (LCP candidate). The rest lazy-load. `sizes` matches the column breakpoints.

### 5.3 Static generation
- `generateStaticParams` returns `[]` with `dynamicParams = true`: each site/locale page renders on its first request, then stays cached until a tag revalidation. The build never depends on backend data, and new sites need no build.
- Optional warm-up after a deploy: a script requests every `primary_host` × locale × page from `/sites`.

---

## 6. Theming and design variants

### 6.1 Tokens → CSS variables
`ThemeStyle` (server component in the layout) writes
`<style>:root{--site-primary:#1F3A5F;…;--site-radius:.25rem;--font-heading:…}</style>`.
- Values are re-validated on the FE (`^#[0-9a-fA-F]{3,6}$`, radius enum). Invalid → base default. Nothing from the API goes into CSS without validation.
- `globals.css` (Tailwind v4): `@theme inline { --color-primary: var(--site-primary); --color-primary-foreground: …; --radius-site: var(--site-radius); --font-heading: var(--font-heading); }`. Components use `bg-primary text-primary-foreground rounded-site font-heading`. No per-site classes.
- `radius` enum → `none 0 | sm .25rem | md .5rem | lg 1rem | full 9999px`.
- `lib/theme/contrast.ts`: computes WCAG ratios for `foreground/background`, `primary_foreground/primary` and `muted/background`. Logs a dev warning below 4.5:1, and a test asserts the seeded themes pass. AA ultimately depends on the colors editors choose.

### 6.2 Variants (structure, not only color)
Three levels, from cheap to expensive:
1. **Token enums (data):** `header_style: minimal|centered|split` → `HEADER_VARIANTS[style]`; `product_card_style: plain|bordered|elevated` → card class map. Resolved once, in a lookup table, not in `if (country)` checks.
2. **Composition (data):** block order and choice per site and locale (`home.blocks`).
3. **Design module (code):** `designs/registry.ts`:
   ```ts
   type DesignModule = Partial<{
     Header: ComponentType<HeaderProps>; Footer: …; ProductCard: …;
     blocks: Partial<{ [K in ContentBlock['type']]: ComponentType<BlockProps<K>> }>;
   }>;
   getDesign(id) = { ...base, ...designs[id], blocks: { ...base.blocks, ...designs[id].blocks } }
   ```
   `config/designs.ts` maps `nordic-home-de → design-a`, `nordic-home-fr → design-b`, and everything else → `base`.

Server components can't use React context, so the resolved `design` and `messages` come from `getSite()`
(`React.cache`) or are passed as props one level down. The component tree is shallow (layout → section → card).

### 6.3 Fonts (`config/fonts.ts`)
```ts
const fraunces = Fraunces({ subsets: ['latin'], variable: '--ff-fraunces', display: 'swap' }); // …Inter, Playfair_Display, Source_Sans_3
export const FONTS: Record<string, NextFont> = { 'Fraunces': fraunces, 'Inter': inter, … };
```
The layout adds the `variable` class names for the site's heading and body families and sets `--font-heading` and `--font-body`.
`source: "system"` → system stack. Unknown family → system stack + `console.warn`. A new Google font is one line
plus a deploy, which is acceptable for a design-level change. Subsets: `latin` + `latin-ext` (de/fr), extended per new language.

### 6.4 Images (`lib/images/loader.ts`, `next.config.ts → images.loaderFile`)
- `NEXT_PUBLIC_IMAGE_PROVIDER=cloudflare`: `${origin}/cdn-cgi/image/width=${w},quality=${q ?? 75},format=auto${pathname}`.
- `none` (local): return the raw URL (with `?w=` so Next doesn't warn).
- Restrict `deviceSizes`/`imageSizes` to ~6 widths (e.g. 320, 480, 640, 960, 1280, 1920) to cap the number of unique transformations.
- A startup assertion compares the env value with `config.media.transform_provider` and logs on mismatch.
- Product images always pass `width`/`height` from the API (no CLS). The hero uses `fill` in a fixed-ratio box.

---

## 7. SEO

| Item | Source | Implementation |
|---|---|---|
| Title, description | `/seo` | `generateMetadata` → `title`, `description` (already "X \| Site", so set `title.absolute`) |
| Canonical | `seo.canonical` (always the primary domain; fallback-locale content canonicalizes to the original) | `alternates.canonical` |
| hreflang | `seo.alternates[]` (incl. `x-default`) | `alternates.languages` = `{[hreflang]: href}` |
| Open Graph | `seo.og` | `openGraph` {title, description, url, siteName, locale, type, images} |
| Robots | `seo.robots` | `robots`, overridden to `noindex` when `APP_ENV !== production` |
| JSON-LD | `seo.json_ld[]` (`WebSite`, `CollectionPage`) | `<JsonLd>`: `JSON.stringify(x).replace(/</g, '\\u003c')` in `<script type="application/ld+json">` |
| Organization | `config.site` + logo | Optional extra `Organization` JSON-LD on home |
| `metadataBase` | `config.site.base_url` | Set in the layout's `generateMetadata` |
| `<html lang>` | locale + country | `hreflang` form (`de-DE`, `en-DE`, `fr-FR`), the same rule as backend `urls.hreflang` |
| Favicon | `theme.favicon` | `icons` metadata |
| sitemap.xml | `/sitemap` | Route handler. **Keep only home and products paths**, and filter their `alternates` the same way. Emit `<xhtml:link rel="alternate" hreflang>` and `lastmod` |
| robots.txt | host map | Primary host in production: `Allow: /` + `Sitemap: https://{primary}/sitemap.xml`. Any other host or environment: `Disallow: /` |

Duplicate content across countries: one canonical host per site (aliases 308 to it), hreflang scoped to the
site's own locales with the country suffix (`de-DE` vs a future `de-AT`), and no cross-site alternates.
Every page is complete HTML from the server: no client-side data fetching.

---

## 8. Internationalization

- The locale comes from the URL (default locale unprefixed, others `/{locale}`), mirroring backend `urls.py`. `lib/site/routing.ts` is the only FE copy of that rule, and it is unit-tested against the same cases.
- Content (titles, blocks, product names, alt text, nav labels) is backend-translated. The API already falls back to the default locale, and canonicalizes fallback product pages.
- Interface strings: `messages/{lang}.ts` typed against `en.ts` (`satisfies Messages`). Lookup order: `pt-BR` → `pt` → `en`.
- Formatting: `Intl.DateTimeFormat`/`NumberFormat`/`PluralRules` with the `de-DE`-style tag (e.g. "8 Produkte").
- Language switcher: rendered only when `config.locales.length > 1` (DE site yes, FR site no). It links to the **same page** in the other locale (computed with `routing.ts`), not just to home. `hrefLang` and `lang` are set on each link.

---

## 9. Performance (Core Web Vitals)

- Pages ship almost no client JS. The only possible client component is a mobile nav toggle: a `<details>`-based menu needs no JS at all, so prefer that.
- LCP: hero / first-row images preloaded with a correct `sizes`. Fonts are self-hosted through `next/font` with `display: swap` and fallback metrics.
- CLS: every image has dimensions or a fixed-ratio box, the font fallback is metric-adjusted, and skeletons match the final layout.
- Cache: static HTML from the Next cache/CDN. API responses are cached in the Data Cache by tag. Media is immutable at the edge.
- Budget: < 100 kB JS per page (gz). Lighthouse CI on both sites' home and products pages in mobile mode, failing below 90.

---

## 10. Accessibility (WCAG 2.2 AA)

Skip link, landmarks (`header`/`nav`/`main`/`footer`), one `<h1>` per page, focus-visible styles in theme colors,
24×24 px minimum target size (2.5.8), `alt` from the API, decorative hero `alt=""` (until backend #5), and
`prefers-reduced-motion` respected. `aria-current="page"` on nav. Contrast is checked by `contrast.ts`.
axe checks run in the Playwright suite.

---

## 11. Security

- Secrets (`SHOWCASE_API_URL` if private, `REVALIDATE_WEBHOOK_SECRET`) are server-only, guarded by `server-only`. Only `NEXT_PUBLIC_IMAGE_PROVIDER` is public.
- Host header: exact match against the backend map. Forwarded headers are opt-in.
- API data that goes into CSS or JSON-LD is validated or escaped. Rich text is backend-sanitized.
- Headers via `next.config`: `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, `frame-ancestors 'none'`. HSTS sits at the CDN.

---

## 12. Environment variables

| Var | Scope | Example |
|---|---|---|
| `SHOWCASE_API_URL` | server | `http://localhost:8000/public/v1` |
| `SHOWCASE_API_MODE` | server | `http` \| `mock` |
| `REVALIDATE_WEBHOOK_SECRET` | server | shared with the backend (`local-webhook-secret` locally) |
| `APP_ENV` | server | `local` \| `staging` \| `production` |
| `DEV_HOST_ALIASES` | server, non-prod only | `localhost=nordic-home-de` |
| `TRUST_FORWARDED_HOST` | server | `false` |
| `HOST_MAP_TTL_SECONDS` | server | `60` |
| `NEXT_PUBLIC_IMAGE_PROVIDER` | public | `none` \| `cloudflare` |

`config/env.ts` validates these at startup (fail fast, same as the backend's pydantic-settings).

---

## 13. Testing

| Area | Tests (Vitest unless noted) |
|---|---|
| Domain resolution | `normalizeHost` (case, port, trailing dot, IDN, IP rejected); exact match; unknown host → 404; alias → 308 to primary with path and query; `/de/…` on a DE-default site → 308 unprefixed; `/en/products` → locale `en`; dev aliases ignored in production; stale map served when refresh fails |
| Routing parity | The same path/locale cases as backend `urls.py` (home/products, default vs prefixed) |
| Tags | Builders match `docs/frontend-revalidation.md` |
| Webhook | Valid signature → revalidates expected tags; bad signature, stale timestamp or non-numeric timestamp → 401; `full_site` adds `site:{id}` |
| Theme | Tokens → CSS vars; invalid color → default; `header_style` / `product_card_style` → expected component or class; design registry merge; font mapping + unknown-font fallback; seeded themes pass contrast |
| Rendering | `BlockRenderer` for every block type with fixtures; unknown block skipped; `ProductGrid` empty state; heading levels (exactly one h1) |
| SEO | `Seo` → `Metadata` mapping; sitemap filter drops `/products/{slug}`; robots per environment |
| E2E (Playwright, mock mode) | `nordic-home-de.localhost:3000` and `nordic-home-fr.localhost:3000`: correct `lang`, h1, theme variable, canonical, hreflang (DE only), JSON-LD, sitemap.xml, robots.txt; FR products never appear on DE; no-JS run (JavaScript disabled) still shows all content; axe has no serious violations |

CI: typecheck, lint, unit, `openapi-typescript` drift check (regenerate from backend `python -m app.cli openapi` and diff), build, e2e, Lighthouse CI.

---

## 14. Implementation phases

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Review the designs: map each Figma section to a block type or token; list missing backend fields (§1) | Signed-off mapping table; backend tickets for #2, #5, #7 if wanted |
| 1 | Scaffold: Next 16, TS strict, Tailwind v4, env validation, generated types, ApiClient (http + mock), recorded fixtures | `pnpm dev` renders raw JSON for both seeded sites in mock and http mode |
| 2 | Routing: host map, proxy, locale rules, `getSite`, 404/redirects, dev aliases | Domain-resolution tests green; both `*.localhost` hosts render their own site |
| 3 | Theme system: CSS vars, Tailwind mapping, fonts registry, image loader, base components (Header variants, Footer, ProductCard variants, sections, BlockRenderer) | Both sites visibly differ through tokens alone |
| 4 | Pages: home + products, states, i18n messages, language switcher | Rendering tests green; works with JS disabled |
| 5 | Design A / Design B modules from Figma (overrides only) | Visual match with designs at mobile, tablet and desktop |
| 6 | SEO: metadata, JSON-LD, sitemap.xml, robots.txt, staging noindex | SEO tests green; Rich Results Test passes |
| 7 | Revalidation webhook + end-to-end with backend worker | Editing a product in the admin API updates both affected sites within ~1 min (debounce 30 s) |
| 8 | Hardening: a11y pass, Lighthouse ≥ 90, security headers, README, "add a site" guide | CI green; docs reviewed |

---

## 15. Adding a website (README section to write)

1. **Same design as an existing site:** do the backend runbook (`docs/runbooks/add-website.md`), then add the domain to the frontend hosting (Vercel/Cloudflare). **No FE code, no deploy.** The host map picks it up within 60 s, or immediately via the `sites` tag on enable.
2. **New font:** add one line in `config/fonts.ts` → deploy.
3. **New design:** add `designs/design-c/` with only the components that differ, register it, and map the site key in `config/designs.ts` (or set `tokens.design` once the backend supports it) → deploy.
4. **New language:** add `i18n/messages/{lang}.ts`, and font subsets if needed.

---

## 16. Open questions for the product owner / backend

1. **Hosting** (Vercel / OpenNext on Cloudflare / self-hosted). This decides the shared cache for `revalidateTag` and whether the backend's Cloudflare HTML purge is needed.
2. **Figma designs:** not in the repo yet. Do they need sections with no block type (categories, testimonials, contact)?
3. **Categories:** needed in v1? If yes, the backend adds them first (§1 #2).
4. **Product detail pages:** confirm they're out of scope (then the sitemap is filtered). The backend is ready if they're wanted.
5. **Legal pages / footer content** (Impressum, privacy, contact) for DE and FR: needed by law, missing from both the backend and the brief.
6. **Backend additions** (small; (a) and (b) live in existing JSON columns, so no migration): (a) `HeroBlock.image_alt`; (b) `ThemeTokens.design`; (c) optional `ui_strings` per locale in site config, to remove the FE dictionaries entirely.
