"""The OpenAPI document is what frontends generate clients from: keep it complete."""

from typing import Any

from fastapi import FastAPI

METHODS = {"get", "post", "put", "patch", "delete"}


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, op)
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in METHODS
    ]


def test_every_operation_is_documented(app: FastAPI) -> None:
    spec = app.openapi()
    ops = _operations(spec)
    assert len(ops) > 40
    ids = [op["operationId"] for _, _, op in ops]
    assert len(ids) == len(set(ids)), "duplicate operationIds"
    for path, method, op in ops:
        where = f"{method.upper()} {path}"
        assert op.get("tags"), f"{where} has no tag"
        assert op.get("summary"), f"{where} has no summary"
        if path.startswith("/admin/v1/") and path != "/admin/v1/auth/login":
            assert op.get("security"), f"{where} is not marked as requiring a bearer token"


def test_admin_request_bodies_have_examples(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]
    for name in (
        "WebsiteCreate",
        "DomainCreate",
        "ThemeUpdate",
        "ContentUpdate",
        "SeoFields",
        "ProductCreate",
        "TranslationIn",
        "AssignmentIn",
        "UserCreate",
    ):
        assert schemas[name].get("examples"), f"{name} has no example"
