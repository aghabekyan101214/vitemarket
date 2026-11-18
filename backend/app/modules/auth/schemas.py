import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.security import MIN_PASSWORD_LENGTH

RoleName = Literal["admin", "catalog_manager", "site_editor"]
Password = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320, examples=["admin@example.com"])
    password: str = Field(max_length=200, examples=["admin-password-123"])


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int
    expires_at: datetime


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    role: RoleName
    website_ids: list[uuid.UUID]


class UserCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "editor@example.com",
                    "password": "a-long-password-123",
                    "role": "site_editor",
                    "website_ids": ["01a1cbcf-db46-7e02-916e-4880d2a1b5c7"],
                }
            ]
        },
    )

    email: str = Field(max_length=320)
    password: str = Password
    role: RoleName
    website_ids: list[uuid.UUID] = Field(default_factory=list)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: RoleName | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=200)


class GrantsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    website_ids: list[uuid.UUID]


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: RoleName
    is_active: bool
    last_login_at: datetime | None
    locked_until: datetime | None
    website_ids: list[uuid.UUID]
    created_at: datetime


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    admin_user_id: uuid.UUID | None
    action: str
    entity: str
    entity_id: str
    website_id: uuid.UUID | None
    diff: dict[str, Any] | None
    created_at: datetime
