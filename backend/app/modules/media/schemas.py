import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.modules.websites.schemas import Locale

AltText = dict[Locale, Annotated[str, Field(max_length=300)]]
alt_adapter: TypeAdapter[AltText] = TypeAdapter(AltText)

ThemeAssetKind = Literal["logo", "logo_dark", "favicon", "og"]
THEME_ASSET_COLUMNS: dict[str, str] = {
    "logo": "logo_key",
    "logo_dark": "logo_dark_key",
    "favicon": "favicon_key",
    "og": "og_default_image_key",
}


class ImageOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    key: str
    url: str
    content_type: str
    width: int
    height: int
    size_bytes: int
    position: int
    alt: dict[str, str]
    created_at: datetime


class ImageAltUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alt: AltText


class ImageOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
