import httpx
from fastapi import FastAPI

from app.core.db import Database


async def test_live(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_ready_with_database(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


async def test_ready_returns_503_when_database_down(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    class DownDatabase(Database):
        async def ping(self) -> None:
            raise ConnectionRefusedError("db down")

    real_db = app.state.db
    app.state.db = DownDatabase.__new__(DownDatabase)
    try:
        resp = await client.get("/health/ready")
    finally:
        app.state.db = real_db

    assert resp.status_code == 503
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == "service_unavailable"
