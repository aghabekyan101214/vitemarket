"""Audit log helpers: compact field-level diffs of admin mutations."""

from collections.abc import Iterable, Mapping
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import inspect


def snapshot(obj: object, fields: Iterable[str]) -> dict[str, Any]:
    """Current values of `fields` on an ORM object (JSON-safe)."""
    state = inspect(obj)
    return {f: jsonable_encoder(state.attrs[f].loaded_value) for f in fields if f in state.attrs}


def diff(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> dict[str, Any]:
    """{field: [old, new]} for changed fields. Created/deleted entities keep all fields."""
    before = jsonable_encoder(before or {})
    after = jsonable_encoder(after or {})
    keys = list(dict.fromkeys([*before, *after]))
    return {k: [before.get(k), after.get(k)] for k in keys if before.get(k) != after.get(k)}
