"""Dependencies shared by the public and admin routers."""

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.storage import Storage


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def get_storage(request: Request) -> Storage:
    storage: Storage = request.app.state.storage
    return storage


StorageDep = Annotated[Storage, Depends(get_storage)]
