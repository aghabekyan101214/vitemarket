import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import IMMUTABLE_CACHE_CONTROL, MemoryStorage
from app.models import DeletedMedia, Product, ProductImage
from app.modules.media import service as media_service
from app.modules.media.service import purge_due
from tests.admin.conftest import PUBLIC, Client, audit_actions, ok, pending_jobs
from tests.images import exif_with_gps_and_rotation, make_image


async def _pid(session: AsyncSession, sku: str = "NH-LAMP-LINEN") -> uuid.UUID:
    pid = await session.scalar(select(Product.id).where(Product.sku == sku))
    assert pid is not None
    return pid


async def _upload(
    client: httpx.AsyncClient,
    product_id: uuid.UUID,
    data: bytes,
    alt: dict[str, str] | str | None = None,
    filename: str = "photo.jpg",
) -> httpx.Response:
    form = {}
    if alt is not None:
        form["alt"] = alt if isinstance(alt, str) else json.dumps(alt)
    return await client.post(
        f"/products/{product_id}/images",
        files={"file": (filename, data, "application/octet-stream")},
        data=form,
    )


async def test_upload_product_image(
    as_role: Client,
    storage: MemoryStorage,
    db_session: AsyncSession,
    site_ids: dict[str, uuid.UUID],
) -> None:
    catalog = as_role("catalog_manager")
    lamp = await _pid(db_session)
    data = make_image("JPEG", size=(80, 60), exif=exif_with_gps_and_rotation())

    body = await ok(await _upload(catalog, lamp, data, {"de": "Leuchte", "en": "Lamp"}), 201)
    assert body["key"].startswith(f"products/{lamp}/")
    assert body["key"].endswith(".jpg")
    assert (body["width"], body["height"]) == (60, 80)  # orientation applied
    assert body["url"] == f"http://localhost:8333/media/{body['key']}"

    stored = storage.objects[body["key"]]
    assert stored.content_type == "image/jpeg"
    assert stored.cache_control == IMMUTABLE_CACHE_CONTROL
    assert b"SpyCam" not in stored.data  # metadata stripped
    assert body["size_bytes"] == len(stored.data)

    # Shown on both sites that use the product; both get a publish job.
    for site in ("nordic-home-de", "nordic-home-fr"):
        listing = await ok(await catalog.get(f"{PUBLIC}/sites/{site}/products?page_size=100"))
        lamp_item = next(p for p in listing["items"] if p["sku"] == "NH-LAMP-LINEN")
        assert lamp_item["image"]["key"] == body["key"]
    assert set(await pending_jobs(db_session)) == set(site_ids.values())
    assert await audit_actions(db_session) == ["image.create"]


@pytest.mark.parametrize(
    ("data", "status", "code"),
    [
        (b"GIF89a not really", 415, "unsupported_image"),
        (b"<svg onload=alert(1)></svg>", 415, "unsupported_image"),
        (b"", 415, "empty_file"),
    ],
)
async def test_rejected_uploads_leave_nothing_behind(
    as_role: Client,
    storage: MemoryStorage,
    db_session: AsyncSession,
    data: bytes,
    status: int,
    code: str,
) -> None:
    r = await _upload(as_role("admin"), await _pid(db_session), data)
    assert (r.status_code, r.json()["code"]) == (status, code)
    assert storage.objects == {}
    assert await db_session.scalar(select(func.count()).select_from(ProductImage)) == 0


async def test_upload_size_limit(
    as_role: Client, app: FastAPI, db_session: AsyncSession, storage: MemoryStorage
) -> None:
    app.state.settings.upload_max_bytes = 500
    r = await _upload(as_role("admin"), await _pid(db_session), make_image("PNG", size=(400, 400)))
    assert r.status_code == 413
    assert storage.objects == {}


async def test_upload_permissions_and_validation(as_role: Client, db_session: AsyncSession) -> None:
    pid = await _pid(db_session)
    assert (await _upload(as_role("site_editor"), pid, make_image())).status_code == 403
    bad_alt = await _upload(as_role("admin"), pid, make_image(), alt='{"xx_yy_zz": 1}')
    assert (bad_alt.status_code, bad_alt.json()["code"]) == (422, "invalid_alt")
    missing = await _upload(as_role("admin"), uuid.uuid4(), make_image())
    assert missing.status_code == 404


async def test_failed_db_write_deletes_uploaded_object(
    as_role: Client,
    storage: MemoryStorage,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compensation: the object is uploaded first; a failing insert removes it again."""
    pid = await _pid(db_session)
    first = await ok(await _upload(as_role("admin"), pid, make_image()), 201)
    storage.objects.clear()
    monkeypatch.setattr(media_service, "product_image_key", lambda *_: first["key"])

    r = await _upload(as_role("admin"), pid, make_image())
    assert r.status_code == 409  # duplicate storage_key → IntegrityError
    assert storage.objects == {}
    assert await db_session.scalar(select(func.count()).select_from(ProductImage)) == 1


async def test_storage_outage(
    as_role: Client, storage: MemoryStorage, db_session: AsyncSession
) -> None:
    storage.fail_puts = True
    r = await _upload(as_role("admin"), await _pid(db_session), make_image())
    assert r.status_code == 503
    assert await db_session.scalar(select(func.count()).select_from(ProductImage)) == 0


async def test_alt_order_and_delete_with_delayed_gc(
    admin: httpx.AsyncClient, storage: MemoryStorage, db_session: AsyncSession
) -> None:
    pid = await _pid(db_session, "NH-MIRROR-BRASS")  # FR only
    a = await ok(await _upload(admin, pid, make_image("PNG")), 201)
    b = await ok(await _upload(admin, pid, make_image("WEBP")), 201)
    assert (a["position"], b["position"]) == (10, 20)

    patched = await ok(await admin.patch(f"/images/{b['id']}", json={"alt": {"fr": "Miroir"}}))
    assert patched["alt"] == {"fr": "Miroir"}

    order = await admin.put(f"/products/{pid}/images/order", json={"image_ids": [b["id"]]})
    assert (order.status_code, order.json()["code"]) == (422, "invalid_image_order")
    await ok(
        await admin.put(f"/products/{pid}/images/order", json={"image_ids": [b["id"], a["id"]]})
    )
    detail = await ok(
        await admin.get(f"{PUBLIC}/sites/nordic-home-fr/products/miroir-rond-en-laiton")
    )
    assert [i["key"] for i in detail["images"]] == [b["key"], a["key"]]
    assert detail["image"]["alt"] == "Miroir"

    assert (await admin.delete(f"/images/{b['id']}")).status_code == 204
    images = await ok(await admin.get(f"/products/{pid}/images"))
    assert [i["id"] for i in images] == [a["id"]]
    # The row is gone but the object stays until the retention period has passed.
    assert b["key"] in storage.objects
    retired = await db_session.scalar(
        select(DeletedMedia).where(DeletedMedia.storage_key == b["key"])
    )
    assert retired is not None
    assert retired.purge_after > datetime.now(UTC) + timedelta(days=6)

    assert await purge_due(db_session, storage) == 0
    assert b["key"] in storage.objects
    await db_session.execute(
        update(DeletedMedia).values(purge_after=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert await purge_due(db_session, storage) == 1
    assert b["key"] not in storage.objects
    assert a["key"] in storage.objects
    assert await db_session.scalar(select(func.count()).select_from(DeletedMedia)) == 0


async def test_theme_assets(
    as_role: Client,
    storage: MemoryStorage,
    db_session: AsyncSession,
    site_ids: dict[str, uuid.UUID],
) -> None:
    editor = as_role("site_editor")
    de = site_ids["de"]
    url = f"/websites/{de}/theme/assets"

    first = await ok(
        await editor.post(url, data={"kind": "logo"}, files={"file": ("l.png", make_image("PNG"))})
    )
    logo_key = first["logo_key"]
    assert logo_key.startswith(f"sites/{de}/logo/")
    config = await ok(await editor.get(f"{PUBLIC}/sites/nordic-home-de/config"))
    assert config["theme"]["logo"]["key"] == logo_key
    assert (await pending_jobs(db_session))[de].full_site

    replaced = await ok(
        await editor.post(
            url, data={"kind": "logo"}, files={"file": ("l.webp", make_image("WEBP"))}
        )
    )
    assert replaced["logo_key"] != logo_key
    assert logo_key in storage.objects  # retired, not deleted yet
    assert await db_session.scalar(
        select(DeletedMedia.id).where(DeletedMedia.storage_key == logo_key)
    )

    svg = await editor.post(url, data={"kind": "favicon"}, files={"file": ("f.svg", b"<svg/>")})
    assert svg.status_code == 415
    bad_kind = await editor.post(
        url, data={"kind": "banner"}, files={"file": ("x.png", make_image("PNG"))}
    )
    assert bad_kind.status_code == 422

    removed = await ok(await editor.delete(f"{url}/logo"))
    assert removed["logo_key"] is None
    assert (await editor.delete(f"{url}/logo")).status_code == 404
    fr = await editor.post(
        f"/websites/{site_ids['fr']}/theme/assets",
        data={"kind": "logo"},
        files={"file": ("l.png", make_image("PNG"))},
    )
    assert fr.status_code == 403
