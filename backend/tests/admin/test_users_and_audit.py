import uuid

import httpx

from tests.admin.conftest import PASSWORD, Client, ok


async def test_user_management(
    admin: httpx.AsyncClient, as_role: Client, site_ids: dict[str, uuid.UUID]
) -> None:
    new = {"email": "New.Editor@Test.example", "password": PASSWORD, "role": "site_editor"}
    no_sites = await admin.post("/users", json=new)
    assert no_sites.status_code == 422
    created = await ok(
        await admin.post("/users", json={**new, "website_ids": [str(site_ids["fr"])]}), 201
    )
    assert created["email"] == "new.editor@test.example"
    assert created["website_ids"] == [str(site_ids["fr"])]
    dup = {**new, "website_ids": [str(site_ids["fr"])]}
    assert (await admin.post("/users", json=dup)).status_code == 409
    short = await admin.post("/users", json={**new, "password": "short"})
    assert short.status_code == 422

    granted = await ok(
        await admin.put(
            f"/users/{created['id']}/websites",
            json={"website_ids": [str(site_ids["de"]), str(site_ids["fr"])]},
        )
    )
    assert len(granted["website_ids"]) == 2
    unknown = await admin.put(
        f"/users/{created['id']}/websites", json={"website_ids": [str(uuid.uuid4())]}
    )
    assert unknown.status_code == 404

    users = await ok(await admin.get("/users"))
    assert "new.editor@test.example" in {u["email"] for u in users}
    deactivated = await ok(await admin.patch(f"/users/{created['id']}", json={"is_active": False}))
    assert not deactivated["is_active"]


async def test_admin_cannot_lock_themselves_out(
    admin: httpx.AsyncClient, users: dict[str, uuid.UUID]
) -> None:
    me = users["admin"]
    for patch in ({"role": "site_editor"}, {"is_active": False}):
        r = await admin.patch(f"/users/{me}", json=patch)
        assert (r.status_code, r.json()["code"]) == (409, "self_lockout")


async def test_password_reset_unlocks(
    admin: httpx.AsyncClient, as_role: Client, users: dict[str, uuid.UUID]
) -> None:
    anon = as_role("anon")
    for _ in range(5):
        await anon.post(
            "/auth/login", json={"email": "site_editor@test.example", "password": "wrong-password!"}
        )
    new_password = "a-brand-new-password"
    await ok(await admin.patch(f"/users/{users['site_editor']}", json={"password": new_password}))
    login = await anon.post(
        "/auth/login", json={"email": "site_editor@test.example", "password": new_password}
    )
    assert login.status_code == 200


async def test_audit_log(
    admin: httpx.AsyncClient,
    as_role: Client,
    site_ids: dict[str, uuid.UUID],
    users: dict[str, uuid.UUID],
) -> None:
    editor = as_role("site_editor")
    de = site_ids["de"]
    await ok(
        await editor.patch(
            f"/domains/{(await editor.get(f'/websites/{de}/domains')).json()[1]['id']}",
            json={"redirect_to_primary": False},
        )
    )
    await ok(await admin.patch(f"/websites/{de}", json={"name": "Renamed"}))

    entries = await ok(await admin.get(f"/audit-log?website_id={de}"))
    assert [e["action"] for e in entries] == ["website.update", "domain.update"]  # newest first
    update, domain = entries
    assert update["diff"] == {"name": ["Nordic Home Deutschland", "Renamed"]}
    assert update["admin_user_id"] == str(users["admin"])
    assert domain["admin_user_id"] == str(users["site_editor"])
    assert domain["diff"] == {"redirect_to_primary": [True, False]}

    older = await ok(await admin.get(f"/audit-log?website_id={de}&before_id={update['id']}"))
    assert [e["id"] for e in older] == [domain["id"]]
    assert (await editor.get("/audit-log")).status_code == 403
