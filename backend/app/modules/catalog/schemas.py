"""Admin API schemas for products, translations and site assignments."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.catalog.models import SLUG_PATTERN
from app.modules.content.schemas import SafeHtml
from app.modules.websites.schemas import Locale

Sku = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", examples=["NH-CHAIR-OAK"])]
Slug = Annotated[str, Field(pattern=SLUG_PATTERN, max_length=200)]
ProductStatusName = Literal["active", "archived"]


class TranslationIn(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "Eichen-Loungesessel",
                    "short_description": "In Skandinavien entworfen.",
                    "description": "<p>Massive Eiche, von Hand geölt.</p>",
                    "attributes": {"care": "Mit Öl pflegen"},
                }
            ]
        },
    )

    name: str = Field(min_length=1, max_length=200)
    slug: Slug | None = Field(default=None, description="Derived from `name` when omitted")
    short_description: str | None = Field(default=None, max_length=500)
    description: SafeHtml | None = Field(default=None, max_length=20_000)
    attributes: dict[str, Any] = Field(default_factory=dict)


class TranslationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    locale: str
    name: str
    slug: str
    short_description: str | None
    description: str | None
    attributes: dict[str, Any]
    updated_at: datetime


class ProductCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "sku": "NH-CHAIR-OAK",
                    "attributes": {"material": "oak", "dimensions_cm": [78, 82, 76]},
                    "translations": {
                        "de": {"name": "Eichen-Loungesessel"},
                        "en": {"name": "Oak Lounge Chair"},
                    },
                }
            ]
        },
    )

    sku: Sku
    attributes: dict[str, Any] = Field(default_factory=dict)
    translations: dict[Locale, TranslationIn] = Field(default_factory=dict)


class ProductUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"attributes": {"material": "oak", "finish": "oiled"}}]},
    )

    sku: Sku | None = None
    attributes: dict[str, Any] | None = None
    status: ProductStatusName | None = None


class ProductAssignmentRef(BaseModel):
    website_id: uuid.UUID
    website_key: str
    position: int
    is_visible: bool
    is_featured: bool


class ProductListItem(BaseModel):
    id: uuid.UUID
    sku: str
    status: str
    names: dict[str, str]
    updated_at: datetime


class ProductPage(BaseModel):
    items: list[ProductListItem]
    total: int
    page: int
    page_size: int


class ProductOut(BaseModel):
    id: uuid.UUID
    sku: str
    status: str
    attributes: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    translations: list[TranslationOut]
    websites: list[ProductAssignmentRef]


class AssignmentIn(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"is_visible": True, "is_featured": True, "position": 10}]},
    )

    position: int | None = Field(default=None, ge=0, le=1_000_000)
    is_visible: bool | None = None
    is_featured: bool | None = None


class AssignmentOut(BaseModel):
    product_id: uuid.UUID
    sku: str
    status: str
    name: str | None = Field(description="Name in the website's default locale")
    has_default_translation: bool
    position: int
    is_visible: bool
    is_featured: bool


class ReorderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)


class BulkAssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    is_visible: bool = True


class BulkAssignOut(BaseModel):
    assigned: list[uuid.UUID]
    already_assigned: list[uuid.UUID]
