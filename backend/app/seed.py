"""Development seed data: 2 websites, 3 locales (de, en, fr), 10 shared products.

Idempotent: every row is upserted by its natural key, so running the seed again updates the
demo data in place without creating duplicates. Never run against production (enforced by
the CLI). Placeholder product images are uploaded by `seed_images` (needs storage).
"""

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slugs import slugify
from app.modules.catalog.models import Product, ProductTranslation, WebsiteProduct
from app.modules.content.models import PageType, SeoMetadata, WebsiteContent
from app.modules.content.schemas import content_blocks_adapter
from app.modules.websites.models import (
    DomainStatus,
    Website,
    WebsiteDomain,
    WebsiteStatus,
    WebsiteTheme,
)
from app.modules.websites.schemas import ThemeTokens

if TYPE_CHECKING:
    from app.core.storage import Storage

DEV_ADMIN_EMAIL = "admin@example.com"
DEV_ADMIN_PASSWORD = "admin-password-123"  # local development only  # noqa: S105

# --------------------------------------------------------------------------------------
# Products: sku, attributes, names per locale
# --------------------------------------------------------------------------------------
PRODUCTS: list[dict[str, Any]] = [
    {
        "sku": "NH-CHAIR-OAK",
        "attributes": {"material": "oak", "dimensions_cm": [78, 82, 76]},
        "names": {
            "en": "Oak Lounge Chair",
            "de": "Eichen-Loungesessel",
            "fr": "Fauteuil lounge en chêne",
        },
    },
    {
        "sku": "NH-TABLE-ASH",
        "attributes": {"material": "ash", "dimensions_cm": [200, 95, 74]},
        "names": {
            "en": "Ash Dining Table",
            "de": "Esstisch aus Esche",
            "fr": "Table à manger en frêne",
        },
    },
    {
        "sku": "NH-LAMP-LINEN",
        "attributes": {"material": "linen", "dimensions_cm": [45, 45, 160]},
        "names": {
            "en": "Linen Floor Lamp",
            "de": "Stehleuchte mit Leinenschirm",
            "fr": "Lampadaire en lin",
        },
    },
    {
        "sku": "NH-SOFA-WOOL",
        "attributes": {"material": "wool", "dimensions_cm": [220, 92, 80]},
        "names": {
            "en": "Wool Three-Seat Sofa",
            "de": "Dreisitzer-Sofa aus Wolle",
            "fr": "Canapé trois places en laine",
        },
    },
    {
        "sku": "NH-SHELF-PINE",
        "attributes": {"material": "pine", "dimensions_cm": [120, 25, 30]},
        "names": {
            "en": "Pine Wall Shelf",
            "de": "Wandregal aus Kiefer",
            "fr": "Étagère murale en pin",
        },
    },
    {
        "sku": "NH-RUG-JUTE",
        "attributes": {"material": "jute", "dimensions_cm": [240, 170, 1]},
        "names": {"en": "Jute Area Rug", "de": "Teppich aus Jute", "fr": "Tapis en jute"},
    },
    {
        "sku": "NH-BED-BIRCH",
        "attributes": {"material": "birch", "dimensions_cm": [168, 210, 90]},
        "names": {
            "en": "Birch Bed Frame",
            "de": "Bettgestell aus Birke",
            "fr": "Cadre de lit en bouleau",
        },
    },
    {
        "sku": "NH-STOOL-WALNUT",
        "attributes": {"material": "walnut", "dimensions_cm": [40, 40, 75]},
        "names": {
            "en": "Walnut Bar Stool",
            "de": "Barhocker aus Nussbaum",
            "fr": "Tabouret de bar en noyer",
        },
    },
    {
        "sku": "NH-MIRROR-BRASS",
        "attributes": {"material": "brass", "dimensions_cm": [80, 3, 80]},
        "names": {
            "en": "Round Brass Mirror",
            "de": "Runder Messingspiegel",
            "fr": "Miroir rond en laiton",
        },
    },
    {
        "sku": "NH-VASE-CERAMIC",
        "attributes": {"material": "ceramic", "dimensions_cm": [18, 18, 32]},
        "names": {"en": "Ceramic Vase", "de": "Keramikvase", "fr": "Vase en céramique"},
    },
]

_SHORT = {
    "en": "{name}: designed in Scandinavia, built to last for decades.",
    "de": "{name}: in Skandinavien entworfen, für Jahrzehnte gebaut.",
    "fr": "{name} : conçu en Scandinavie, fait pour durer des décennies.",
}
_LONG = {
    "en": "The {name} combines honest materials with clean lines. Every piece is finished by "
    "hand in small workshops and made from responsibly sourced {material}.",
    "de": "{name} verbindet ehrliche Materialien mit klaren Linien. Jedes Stück wird in kleinen "
    "Werkstätten von Hand veredelt und aus verantwortungsvoll gewonnenem Material gefertigt.",
    "fr": "{name} associe des matériaux authentiques à des lignes épurées. Chaque pièce est "
    "finie à la main dans de petits ateliers, à partir de matériaux d'origine responsable.",
}

# --------------------------------------------------------------------------------------
# Websites
# --------------------------------------------------------------------------------------
SITES: list[dict[str, Any]] = [
    {
        "key": "nordic-home-de",
        "name": "Nordic Home Deutschland",
        "country_code": "DE",
        "default_locale": "de",
        "locales": ["de", "en"],
        "domains": [
            {"hostname": "nordic-home-de.localhost", "is_primary": True, "status": "active"},
            {"hostname": "www.nordic-home-de.localhost", "is_primary": False, "status": "active"},
            {
                "hostname": "staging.nordic-home-de.localhost",
                "is_primary": False,
                "status": "pending",
            },
        ],
        "theme": {
            "colors": {
                "primary": "#1F3A5F",
                "primary_foreground": "#FFFFFF",
                "secondary": "#E8E1D9",
                "accent": "#C8553D",
                "background": "#FAFAF7",
                "foreground": "#1B1B1B",
                "muted": "#6B7280",
                "border": "#E5E7EB",
            },
            "fonts": {
                "heading": {"family": "Fraunces", "weights": [500, 700]},
                "body": {"family": "Inter", "weights": [400, 600]},
            },
            "radius": "sm",
            "header_style": "split",
            "product_card_style": "plain",
        },
        # (sku, position, featured, visible)
        "products": [
            ("NH-CHAIR-OAK", 10, True, True),
            ("NH-TABLE-ASH", 20, True, True),
            ("NH-LAMP-LINEN", 30, True, True),
            ("NH-SOFA-WOOL", 40, False, True),
            ("NH-SHELF-PINE", 50, False, True),
            ("NH-RUG-JUTE", 60, False, True),
            ("NH-BED-BIRCH", 70, False, True),
            ("NH-STOOL-WALNUT", 80, False, True),
        ],
    },
    {
        "key": "nordic-home-fr",
        "name": "Nordic Home France",
        "country_code": "FR",
        "default_locale": "fr",
        "locales": ["fr"],
        "domains": [
            {"hostname": "nordic-home-fr.localhost", "is_primary": True, "status": "active"},
        ],
        "theme": {
            "colors": {
                "primary": "#2F4F3E",
                "primary_foreground": "#FFFFFF",
                "secondary": "#F1EAE0",
                "accent": "#B08D57",
                "background": "#FFFFFF",
                "foreground": "#222222",
                "muted": "#707070",
                "border": "#E6E2DC",
            },
            "fonts": {
                "heading": {"family": "Playfair Display", "weights": [600]},
                "body": {"family": "Source Sans 3", "weights": [400, 600]},
            },
            "radius": "lg",
            "header_style": "centered",
            "product_card_style": "elevated",
        },
        "products": [
            ("NH-LAMP-LINEN", 10, False, True),
            ("NH-SOFA-WOOL", 20, False, True),
            ("NH-SHELF-PINE", 30, True, True),
            ("NH-RUG-JUTE", 40, True, True),
            ("NH-BED-BIRCH", 50, True, True),
            ("NH-STOOL-WALNUT", 60, False, True),
            ("NH-MIRROR-BRASS", 70, False, True),
            ("NH-VASE-CERAMIC", 80, False, False),
        ],
    },
]

_HOME: dict[str, dict[str, Any]] = {
    "de": {
        "title": "Skandinavisches Design für Ihr Zuhause",
        "subtitle": "Langlebige Möbel aus natürlichen Materialien.",
        "hero": "Zeitlos wohnen",
        "cta": "Alle Produkte ansehen",
        "products": "Produkte",
        "featured": "Beliebte Stücke",
        "features": [
            ("Massives Holz", "Nur zertifizierte Hölzer."),
            ("Handgefertigt", "In kleinen Werkstätten gefertigt."),
            ("10 Jahre Garantie", "Gebaut für Generationen."),
        ],
    },
    "en": {
        "title": "Scandinavian design for your home",
        "subtitle": "Durable furniture made from natural materials.",
        "hero": "Timeless living",
        "cta": "Browse all products",
        "products": "Products",
        "featured": "Customer favourites",
        "features": [
            ("Solid wood", "Certified timber only."),
            ("Handmade", "Crafted in small workshops."),
            ("10-year warranty", "Built for generations."),
        ],
    },
    "fr": {
        "title": "Le design scandinave pour votre intérieur",
        "subtitle": "Des meubles durables en matériaux naturels.",
        "hero": "Un intérieur intemporel",
        "cta": "Voir tous les produits",
        "products": "Produits",
        "featured": "Nos coups de cœur",
        "features": [
            ("Bois massif", "Uniquement des bois certifiés."),
            ("Fait main", "Fabriqué dans de petits ateliers."),
            ("Garantie 10 ans", "Conçu pour durer."),
        ],
    },
}


@dataclass
class SeedSummary:
    websites: int = 0
    domains: int = 0
    products: int = 0
    translations: int = 0
    assignments: int = 0
    content_pages: int = 0
    seo_rows: int = 0
    website_ids: dict[str, uuid.UUID] = field(default_factory=dict)


async def _upsert(
    session: AsyncSession, model: Any, values: dict[str, Any], conflict: list[str]
) -> Any:
    """INSERT … ON CONFLICT (natural key) DO UPDATE; returns the primary key row."""
    stmt = pg_insert(model).values(**values)
    updates: dict[str, Any] = {k: stmt.excluded[k] for k in values if k not in conflict}
    if "updated_at" in model.__table__.c:
        updates["updated_at"] = func.now()
    upsert = stmt.on_conflict_do_update(index_elements=conflict, set_=updates).returning(
        *model.__table__.primary_key.columns
    )
    return (await session.execute(upsert)).one()


def _home_blocks(locale: str) -> list[dict[str, Any]]:
    t = _HOME[locale]
    blocks = [
        {
            "type": "hero",
            "heading": t["hero"],
            "subheading": t["subtitle"],
            "cta": {"label": t["cta"], "href": "/products"},
        },
        {"type": "featured_products", "heading": t["featured"], "limit": 3},
        {"type": "features", "items": [{"title": a, "description": b} for a, b in t["features"]]},
    ]
    validated = content_blocks_adapter.validate_python(blocks)
    dumped: list[dict[str, Any]] = content_blocks_adapter.dump_python(
        validated, mode="json", exclude_none=True
    )
    return dumped


async def seed(session: AsyncSession) -> SeedSummary:
    """Upsert all demo data. Flushes but does not commit."""
    summary = SeedSummary()

    product_ids: dict[str, uuid.UUID] = {}
    for p in PRODUCTS:
        (pid,) = await _upsert(
            session, Product, {"sku": p["sku"], "attributes": p["attributes"]}, ["sku"]
        )
        product_ids[p["sku"]] = pid
        summary.products += 1
        for locale, name in p["names"].items():
            fmt = {"name": name, "material": p["attributes"]["material"]}
            await _upsert(
                session,
                ProductTranslation,
                {
                    "product_id": pid,
                    "locale": locale,
                    "name": name,
                    "slug": slugify(name),
                    "short_description": _SHORT[locale].format(**fmt),
                    "description": _LONG[locale].format(**fmt),
                },
                ["product_id", "locale"],
            )
            summary.translations += 1

    for site in SITES:
        (wid,) = await _upsert(
            session,
            Website,
            {
                "key": site["key"],
                "name": site["name"],
                "country_code": site["country_code"],
                "default_locale": site["default_locale"],
                "locales": site["locales"],
                "status": WebsiteStatus.ACTIVE,
            },
            ["key"],
        )
        summary.website_ids[site["key"]] = wid
        # Re-seeding changes content: invalidate public ETags like an admin edit would.
        await session.execute(
            update(Website)
            .where(Website.id == wid)
            .values(content_version=Website.content_version + 1)
        )
        summary.websites += 1

        for d in site["domains"]:
            await _upsert(
                session,
                WebsiteDomain,
                {
                    "website_id": wid,
                    "hostname": d["hostname"],
                    "is_primary": d["is_primary"],
                    "status": DomainStatus(d["status"]),
                },
                ["hostname"],
            )
            summary.domains += 1

        tokens = ThemeTokens.model_validate(site["theme"]).model_dump(mode="json")
        await _upsert(session, WebsiteTheme, {"website_id": wid, "tokens": tokens}, ["website_id"])

        for sku, position, featured, visible in site["products"]:
            await _upsert(
                session,
                WebsiteProduct,
                {
                    "website_id": wid,
                    "product_id": product_ids[sku],
                    "position": position,
                    "is_featured": featured,
                    "is_visible": visible,
                },
                ["website_id", "product_id"],
            )
            summary.assignments += 1

        for locale in site["locales"]:
            t = _HOME[locale]
            pages = [
                (PageType.HOME, t["title"], t["subtitle"], _home_blocks(locale)),
                (PageType.PRODUCTS, t["products"], None, []),
            ]
            for page, title, subtitle, blocks in pages:
                await _upsert(
                    session,
                    WebsiteContent,
                    {
                        "website_id": wid,
                        "page": page,
                        "locale": locale,
                        "title": title,
                        "subtitle": subtitle,
                        "blocks": blocks,
                    },
                    ["website_id", "page", "locale"],
                )
                summary.content_pages += 1

            seo_rows: list[tuple[PageType, uuid.UUID | None, str, str]] = [
                (PageType.HOME, None, f"{site['name']} | {t['title']}", t["subtitle"]),
                (PageType.PRODUCTS, None, f"{t['products']} | {site['name']}", t["subtitle"]),
            ]
            # One product-level override per site, to exercise the product SEO path.
            first_sku = site["products"][0][0]
            first = next(p for p in PRODUCTS if p["sku"] == first_sku)
            seo_rows.append(
                (
                    PageType.PRODUCT,
                    product_ids[first_sku],
                    f"{first['names'][locale]} | {site['name']}",
                    _SHORT[locale].format(name=first["names"][locale]),
                )
            )
            for page, product_id, title, description in seo_rows:
                await _upsert(
                    session,
                    SeoMetadata,
                    {
                        "website_id": wid,
                        "locale": locale,
                        "page": page,
                        "product_id": product_id,
                        "title": title,
                        "description": description,
                    },
                    ["website_id", "locale", "page", "product_id"],
                )
                summary.seo_rows += 1

    await session.flush()
    return summary


# --------------------------------------------------------------------------------------
# Placeholder product images (needs object storage; used by `cli seed`)
# --------------------------------------------------------------------------------------


def _placeholder(sku: str, label: str) -> bytes:
    """A deterministic 1200x900 WebP: a soft color from the SKU plus the product name."""
    import hashlib
    import io

    from PIL import Image, ImageDraw, ImageFont

    digest = hashlib.sha256(sku.encode()).digest()
    background = tuple(150 + b % 90 for b in digest[:3])
    image = Image.new("RGB", (1200, 900), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 60, 1140, 840), outline=(255, 255, 255), width=6)
    font = ImageFont.load_default(size=64)
    box = draw.textbbox((0, 0), label, font=font)
    x = (1200 - (box[2] - box[0])) / 2
    draw.text((x, 410), label, fill=(40, 40, 40), font=font)
    out = io.BytesIO()
    image.save(out, "WEBP", quality=85)
    return out.getvalue()


async def seed_images(session: AsyncSession, storage: "Storage") -> int:
    """One placeholder image per product at a stable key (idempotent). Flushes only."""
    from sqlalchemy import select

    from app.core.storage import IMMUTABLE_CACHE_CONTROL
    from app.modules.catalog.models import ProductImage

    count = 0
    for p in PRODUCTS:
        product_id = await session.scalar(select(Product.id).where(Product.sku == p["sku"]))
        key = f"seed/products/{p['sku'].lower()}.webp"
        data = _placeholder(p["sku"], p["names"]["en"])
        await storage.put(
            key, data, content_type="image/webp", cache_control=IMMUTABLE_CACHE_CONTROL
        )
        await _upsert(
            session,
            ProductImage,
            {
                "product_id": product_id,
                "storage_key": key,
                "content_type": "image/webp",
                "width": 1200,
                "height": 900,
                "size_bytes": len(data),
                "position": 10,
                "alt": dict(p["names"]),
            },
            ["storage_key"],
        )
        count += 1
    await session.flush()
    return count
