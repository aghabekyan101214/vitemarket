"""Role matrix: who can do what, and site scoping for site_editor."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product
from tests.admin.conftest import Client

DE, FR = "{de}", "{fr}"

CONTENT = {"title": "T", "blocks": []}
THEME_IF_MATCH = {"If-Match": '"1"'}

# (method, path template, json body, extra headers)
OPS: dict[str, tuple[str, str, object, dict[str, str] | None]] = {
    "list_websites": ("GET", "/websites", None, None),
    "read_de": ("GET", f"/websites/{DE}", None, None),
    "read_fr": ("GET", f"/websites/{FR}", None, None),
    "create_website": (
        "POST",
        "/websites",
        {"key": "new-site", "name": "N", "country_code": "at", "default_locale": "de",
         "locales": ["de"]},
        None,
    ),
    "update_de": ("PATCH", f"/websites/{DE}", {"name": "Renamed"}, None),
    "content_de": ("PUT", f"/websites/{DE}/content/home/en", CONTENT, {"If-Match": '"1"'}),
    "content_fr": ("PUT", f"/websites/{FR}/content/home/fr", CONTENT, {"If-Match": '"1"'}),
    "domain_de": ("POST", f"/websites/{DE}/domains", {"hostname": "alias.example.de"}, None),
    "publish_de": ("POST", f"/websites/{DE}/publish", None, None),
    "list_products": ("GET", "/products", None, None),
    "create_product": ("POST", "/products", {"sku": "NEW-1"}, None),
    "assign_de": ("PUT", f"/websites/{DE}/products/{{pid}}", {"is_featured": True}, None),
    "assign_fr": ("PUT", f"/websites/{FR}/products/{{pid}}", {"is_featured": True}, None),
    "users": ("GET", "/users", None, None),
    "audit": ("GET", "/audit-log", None, None),
}  # fmt: skip

ALLOWED: dict[str, set[str]] = {
    "admin": set(OPS),
    "catalog_manager": {"list_websites", "read_de", "read_fr", "list_products", "create_product"},
    "site_editor": {
        "list_websites", "read_de", "content_de", "domain_de", "publish_de", "list_products",
        "assign_de",
    },
}  # fmt: skip


@pytest.mark.parametrize("role", list(ALLOWED))
@pytest.mark.parametrize("op", list(OPS))
async def test_role_matrix(
    as_role: Client, site_ids: dict[str, uuid.UUID], db_session: AsyncSession, role: str, op: str
) -> None:
    method, template, body, headers = OPS[op]
    pid = await db_session.scalar(select(Product.id).where(Product.sku == "NH-LAMP-LINEN"))
    path = template.format(de=site_ids["de"], fr=site_ids["fr"], pid=pid)
    r = await as_role(role).request(method, path, json=body, headers=headers)
    if op in ALLOWED[role]:
        assert r.status_code < 400, (role, op, r.text)
    else:
        assert r.status_code == 403, (role, op, r.status_code, r.text)


async def test_site_editor_sees_only_granted_sites(as_role: Client) -> None:
    sites = (await as_role("site_editor").get("/websites")).json()
    assert [s["key"] for s in sites] == ["nordic-home-de"]
    everything = (await as_role("catalog_manager").get("/websites")).json()
    assert len(everything) == 2


async def test_anonymous_is_rejected(as_role: Client) -> None:
    for path in ("/websites", "/products", "/users"):
        assert (await as_role("anon").get(path)).status_code == 401
