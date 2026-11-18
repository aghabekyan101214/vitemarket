import httpx
import pytest
from fastapi import FastAPI

from app.core.errors import Conflict

PROBLEM = "application/problem+json"


@pytest.fixture
def app_with_test_routes(app: FastAPI) -> FastAPI:
    @app.get("/_test/conflict")
    async def conflict() -> None:
        raise Conflict("Slug already taken.", code="slug_taken", extra={"field": "slug"})

    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("unexpected")

    @app.get("/_test/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    return app


async def test_unknown_route_is_problem_json(client: httpx.AsyncClient) -> None:
    resp = await client.get("/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == PROBLEM
    body = resp.json()
    assert body["code"] == "not_found"
    assert body["instance"] == "/nope"
    assert body["request_id"] == resp.headers["x-request-id"]


async def test_app_error_rendering(
    app_with_test_routes: FastAPI, client: httpx.AsyncClient
) -> None:
    resp = await client.get("/_test/conflict")
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "slug_taken"
    assert body["detail"] == "Slug already taken."
    assert body["field"] == "slug"


async def test_validation_error_rendering(
    app_with_test_routes: FastAPI, client: httpx.AsyncClient
) -> None:
    resp = await client.get("/_test/items/not-a-number")
    assert resp.status_code == 422
    assert resp.headers["content-type"] == PROBLEM
    body = resp.json()
    assert body["code"] == "validation_failed"
    assert body["errors"][0]["loc"] == ["path", "item_id"]


async def test_unhandled_error_is_500_problem_without_leaking(
    app_with_test_routes: FastAPI, client: httpx.AsyncClient
) -> None:
    resp = await client.get("/_test/boom")
    assert resp.status_code == 500
    assert resp.headers["content-type"] == PROBLEM
    body = resp.json()
    assert body["code"] == "internal_error"
    assert "unexpected" not in resp.text
    assert body["request_id"] == resp.headers["x-request-id"]


async def test_request_id_is_propagated(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health/live", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["x-request-id"] == "abc-123"


async def test_invalid_request_id_is_replaced(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health/live", headers={"X-Request-ID": "bad id\twith spaces"})
    assert resp.headers["x-request-id"] != "bad id\twith spaces"
    assert len(resp.headers["x-request-id"]) == 32
