# Runbook: add a new website

A new showcase site needs **no code change and no deploy**: everything below is data,
entered through the admin API (or an admin UI built on it). The whole flow is covered by
the test `backend/tests/admin/test_onboarding_e2e.py`. If that test passes, this runbook
works.

**Time:** about 15 minutes of work, plus DNS propagation.
**Who:** an `admin` (domain verification is admin-only). Content and SEO can be handed to a
`site_editor` once the site exists; products to a `catalog_manager`.

---

## 0. Prerequisites

| What | Notes |
| --- | --- |
| The domain, in a Cloudflare zone | Needed for edge caching and purge. Note the **zone ID** (dashboard → domain → Overview → API). |
| Frontend hosting accepts the host | The Next.js deployment must serve the new hostname, e.g. add the domain in Vercel/Pages, or add a Cloudflare route to the frontend. This is configuration, not code: the frontend maps host → site through `GET /public/v1/sites`. |
| `CLOUDFLARE_API_TOKEN` can purge the zone | The token needs *Zone → Cache Purge* on the new zone. If it is scoped to "all zones", nothing to do. |
| Locales | Any BCP-47 language code (`de`, `en`, `pt-BR`). The default locale gets un-prefixed URLs (`/products`); others are prefixed (`/en/products`). |

```bash
API=https://api.example.com/admin/v1          # local: http://localhost:8000/admin/v1
TOKEN=$(curl -s $API/auth/login -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"…"}' | jq -r .access_token)
H=(-H "Authorization: Bearer $TOKEN" -H 'content-type: application/json')
```

## 1. Create the website (draft)

Drafts are invisible on the public API, so you can prepare everything before launch.

```bash
SITE=$(curl -s "${H[@]}" $API/websites -d '{
  "key": "casa-verde-it",
  "name": "Casa Verde",
  "country_code": "IT",
  "default_locale": "it",
  "locales": ["it", "en"]
}' | jq -r .id)
```

`key` is permanent: it appears in public API URLs and in the frontend build. Use
`<brand>-<country>`, lowercase with dashes.

## 2. Theme

The site starts from a neutral default theme. Replace it (optimistic locking: send the
current version in `If-Match`):

```bash
curl -s "${H[@]}" $API/websites/$SITE/theme | jq .tokens > theme.json   # edit colors/fonts
curl -s "${H[@]}" -X PUT $API/websites/$SITE/theme -H 'If-Match: "1"' \
  -d "{\"tokens\": $(cat theme.json)}"

# Logo, dark logo, favicon, default social image (JPEG/PNG/WebP/AVIF; no SVG)
curl -s -H "Authorization: Bearer $TOKEN" $API/websites/$SITE/theme/assets \
  -F kind=logo -F file=@logo.png
```

## 3. Domain and DNS

```bash
DOMAIN=$(curl -s "${H[@]}" $API/websites/$SITE/domains -d '{
  "hostname": "casaverde.it",
  "cloudflare_zone_id": "<zone id>"
}')
echo "$DOMAIN" | jq .verification
# → {"record_name": "_showcase-verify.casaverde.it", "value": "<32 hex chars>"}
```

In Cloudflare DNS for the zone:

1. **TXT** `_showcase-verify.casaverde.it` set to the `value` above. This proves you control
   the domain.
2. The record that points the site at the frontend (e.g. **CNAME** `casaverde.it` →
   your frontend host), **proxied** (orange cloud) so the edge cache and purge apply.

Then verify (repeat until DNS has propagated, usually under a minute on Cloudflare):

```bash
curl -s "${H[@]}" -X POST $API/domains/$(echo "$DOMAIN" | jq -r .id)/verify | jq .status
# "active"
```

The first domain becomes primary automatically. To add more domains (e.g.
`www.casaverde.it`), repeat this step. Non-primary domains redirect to the primary by
default (`redirect_to_primary`).

> Emergency: `POST /domains/{id}/verify?force=true` skips the DNS check (admin-only,
> audited). Use it only for a domain you have confirmed some other way.

## 4. Content and SEO, per locale

Pages: `home`, `products` (listing intro). Blocks: `hero`, `text` (sanitized HTML),
`features`, `featured_products`, `cta`. See `/docs` for the schemas and examples.

```bash
for LOC in it en; do
  curl -s "${H[@]}" -X PUT $API/websites/$SITE/content/home/$LOC -d @home.$LOC.json
  curl -s "${H[@]}" -X PUT $API/websites/$SITE/seo/home/$LOC \
    -d '{"title": "Casa Verde | …", "description": "…"}'
done
```

Updating existing content later requires `If-Match: "<version>"` (412 means someone else
changed it).

## 5. Products

Products are shared across sites. Reuse existing ones, or create new ones.

```bash
# Existing catalog products: search, then bulk-assign
curl -s "${H[@]}" "$API/products?q=chair&page_size=100" | jq '.items[] | {id, sku}'
curl -s "${H[@]}" $API/websites/$SITE/products/bulk-assign \
  -d '{"product_ids": ["…", "…"]}'

# Make sure every assigned product has a translation in each site locale
curl -s "${H[@]}" -X PUT $API/products/$PRODUCT/translations/it -d '{"name": "Divano in lino"}'

# Featured on the home page, and the order
curl -s "${H[@]}" -X PUT $API/websites/$SITE/products/$PRODUCT \
  -d '{"is_featured": true, "position": 1}'
```

A visible product needs a translation in the site's **default** locale (422 otherwise).
Missing secondary-locale translations fall back to the default locale; the page then sets
its canonical URL to the default-locale page, so it is not duplicate content.

## 6. Preview, then go live

The public API serves only `active` sites. To review a draft, read it back through the
admin API, or run the frontend against a staging backend.

```bash
curl -s "${H[@]}" -X POST $API/websites/$SITE/enable | jq '{status, primary_host}'
```

Enabling the site:

* makes it appear in `GET /public/v1/sites` and `/resolve?host=casaverde.it`;
* queues a full-site publish job. The worker calls the frontend revalidation webhook
  (hosts, locales, tags), then purges the zone's cache.

`enable` refuses (409 `primary_domain_required`) while there is no verified primary domain.

## 7. Check

```bash
PUB=https://api.example.com/public/v1
curl -s "$PUB/resolve?host=casaverde.it" | jq .
curl -s "$PUB/sites/casa-verde-it/bundle" | jq '{locale: .config.locale, products: (.products|length)}'
curl -s "$PUB/sites/casa-verde-it/sitemap" | jq '.entries[].loc'
curl -s "${H[@]}" $API/websites/$SITE/publish-jobs | jq '.[0] | {status, attempts, last_error}'
```

* Publish job `succeeded`: the frontend was told to build. Then open `https://casaverde.it`.
* Job `pending` with `last_error`: the worker is retrying with backoff (15 s → 15 min). Check
  the webhook URL and secret, and the frontend logs.
* Job `failed`: fix the cause, then `POST /publish-jobs/{id}/retry`, or
  `POST /websites/{id}/publish`.
* Search Console: add the property and submit `https://casaverde.it/sitemap.xml` (the
  frontend renders it from `/sitemap`).

## 8. Hand over

```bash
# A site editor for this site only
curl -s "${H[@]}" $API/users -d '{
  "email": "editor@casaverde.it", "password": "<generated>", "role": "site_editor",
  "website_ids": ["'$SITE'"]
}'
```

## Rollback

`POST /websites/{id}/disable` takes the site off the public API at once and queues a
full publish, so the frontend drops it. Nothing is deleted. Re-enable at any time.
