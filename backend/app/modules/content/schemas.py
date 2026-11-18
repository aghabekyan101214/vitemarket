"""Homepage/page content blocks (stored in website_content.blocks).

A discriminated union on `type`: the frontend renders one component per block type.
Adding a block type = add a model here + a component in Next.js; no DB migration.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter

from app.core.html import sanitize_html

SafeHtml = Annotated[str, AfterValidator(sanitize_html)]


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Link(_Block):
    label: str = Field(min_length=1, max_length=80)
    href: str = Field(min_length=1, max_length=500)


class HeroBlock(_Block):
    type: Literal["hero"] = "hero"
    heading: str = Field(min_length=1, max_length=200)
    subheading: str | None = Field(default=None, max_length=500)
    image_key: str | None = None
    cta: Link | None = None


class TextBlock(_Block):
    type: Literal["text"] = "text"
    heading: str | None = Field(default=None, max_length=200)
    body: SafeHtml = Field(min_length=1, max_length=10_000, description="Sanitized HTML")


class FeatureItem(_Block):
    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    icon: str | None = Field(default=None, max_length=50)


class FeatureListBlock(_Block):
    type: Literal["features"] = "features"
    heading: str | None = Field(default=None, max_length=200)
    items: list[FeatureItem] = Field(min_length=1, max_length=12)


class FeaturedProductsBlock(_Block):
    """Renders the website's products flagged is_featured (resolved by the API)."""

    type: Literal["featured_products"] = "featured_products"
    heading: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=8, ge=1, le=24)


class CtaBlock(_Block):
    type: Literal["cta"] = "cta"
    heading: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=1000)
    link: Link


ContentBlock = Annotated[
    HeroBlock | TextBlock | FeatureListBlock | FeaturedProductsBlock | CtaBlock,
    Field(discriminator="type"),
]

content_blocks_adapter: TypeAdapter[list[ContentBlock]] = TypeAdapter(list[ContentBlock])


# ======================================================================================
# Admin API schemas
# ======================================================================================

EditablePage = Literal["home", "products"]
SeoPageName = Literal["home", "products", "product"]


class ContentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    page: str
    locale: str
    title: str
    version: int
    updated_at: datetime


class ContentOut(ContentSummary):
    subtitle: str | None
    blocks: list[ContentBlock]


class ContentUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "title": "Skandinavisches Design für Ihr Zuhause",
                    "subtitle": "Langlebige Möbel aus natürlichen Materialien.",
                    "blocks": [
                        {
                            "type": "hero",
                            "heading": "Zeitlos wohnen",
                            "cta": {"label": "Alle Produkte", "href": "/products"},
                        },
                        {"type": "featured_products", "heading": "Beliebte Stücke", "limit": 3},
                        {
                            "type": "text",
                            "body": "<p>Seit 1998 <strong>handgefertigt</strong>.</p>",
                        },
                    ],
                }
            ]
        },
    )

    title: str = Field(min_length=1, max_length=300)
    subtitle: str | None = Field(default=None, max_length=2000)
    blocks: list[ContentBlock] = Field(default_factory=list, max_length=50)


class SeoFields(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "title": "Nordic Home | Skandinavische Möbel",
                    "description": "Langlebige Möbel aus natürlichen Materialien.",
                    "robots": "index,follow",
                }
            ]
        },
    )

    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=500)
    canonical_url: str | None = Field(default=None, max_length=2000, pattern=r"^https://")
    og_title: str | None = Field(default=None, max_length=300)
    og_description: str | None = Field(default=None, max_length=500)
    og_image_key: str | None = Field(default=None, max_length=500)
    robots: str = Field(
        default="index,follow", max_length=100, pattern=r"^[a-z-]+(,\s*[a-z-]+(:[\w-]+)?)*$"
    )
    structured_data: dict[str, Any] | None = None


class SeoOut(SeoFields):
    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: uuid.UUID
    page: str
    locale: str
    product_id: uuid.UUID | None
    updated_at: datetime
