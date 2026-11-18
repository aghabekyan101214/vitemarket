# Frontend contract: cache tags and the revalidation webhook

The backend tells the frontend **which cached data changed**; the frontend regenerates it.
Visitors never call the backend.

## 1. Tag every fetch

```ts
// lib/api.ts
const API = process.env.SHOWCASE_API_URL!; // e.g. https://api.example.com/public/v1

export async function getHome(site: { id: string; key: string }, locale: string) {
  const res = await fetch(`${API}/sites/${site.key}/home?locale=${locale}`, {
    next: { tags: [`site:${site.id}`, `site:${site.id}:home:${locale}`] },
  });
  if (!res.ok) throw new Error(`home ${res.status}`);
  return res.json();
}
```

| Tag | Use on |
|---|---|
| `sites` | `GET /sites` (host → site map used by middleware) |
| `site:{id}` | **every** fetch of that site (config, theme, nav, …) |
| `site:{id}:home:{locale}` | `GET /sites/{key}/home` |
| `site:{id}:products:{locale}` | `GET /sites/{key}/products` |
| `site:{id}:product:{productId}` | `GET /sites/{key}/products/{slug}` and its SEO |

Because every fetch also carries `site:{id}`, a full-site change only needs that one tag.

## 2. Receive the webhook

`POST REVALIDATE_WEBHOOK_URL` with:

```json
{
  "job_id": "…", "website_id": "…", "website_key": "nordic-home-de",
  "hosts": ["nordic-home-de.com", "www.nordic-home-de.com"], "primary_host": "nordic-home-de.com",
  "locales": ["de", "en"], "default_locale": "de",
  "tags": ["site:…:home:de"], "paths": ["/"], "full_site": false, "content_version": 42
}
```

Headers: `X-Showcase-Timestamp` (unix seconds), `X-Showcase-Signature: v1=<hex>`,
`X-Showcase-Job-Id`. The signature is `HMAC-SHA256(secret, "<timestamp>.<raw body>")`.

```ts
// app/api/revalidate/route.ts
import { createHmac, timingSafeEqual } from "node:crypto";
import { revalidateTag } from "next/cache";

const SECRET = process.env.REVALIDATE_WEBHOOK_SECRET!;
const TOLERANCE_SECONDS = 300;

export async function POST(req: Request) {
  const body = await req.text(); // raw body: sign exactly these bytes
  const ts = req.headers.get("x-showcase-timestamp") ?? "";
  const sig = req.headers.get("x-showcase-signature") ?? "";

  if (!/^\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > TOLERANCE_SECONDS) {
    return new Response("stale", { status: 401 });
  }
  const expected = "v1=" + createHmac("sha256", SECRET).update(`${ts}.${body}`).digest("hex");
  const a = Buffer.from(sig), b = Buffer.from(expected);
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    return new Response("bad signature", { status: 401 });
  }

  const job = JSON.parse(body) as { tags: string[]; website_id: string; full_site: boolean };
  const tags = job.full_site ? [`site:${job.website_id}`, ...job.tags] : job.tags;
  for (const tag of new Set(tags)) revalidateTag(tag);
  return Response.json({ revalidated: tags.length });
}
```

- Respond **2xx only after** revalidating. Anything else (or a timeout > 10 s) is retried
  with backoff: 15 s, 30 s, 1 min … up to 15 min, at most 8 attempts.
- Deliveries can repeat (retries, worker restarts). `revalidateTag` is idempotent; use
  `job_id` if you keep your own log.
- After a successful delivery the backend purges Cloudflare for `paths` on every host
  (or the whole hostnames when `full_site`), if `CDN_PURGE_PROVIDER=cloudflare`.

Reference implementation of the check: `backend/app/modules/publishing/webhook.py:verify`.
