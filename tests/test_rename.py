"""A user renames themselves from the dashboard; the status line shows the new name."""
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import admin_client, bearer, env  # noqa: F401  (fixture)


async def test_user_renames_themselves_and_status_line_follows(env):  # noqa: F811
    gw, conn, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/api/me/name", json={"name": "  Bobby  "}, headers=bearer(keys["bob"]))
        assert r.json() == {"ok": True, "name": "Bobby"}
        line = (await c.get("/api/me/status?format=text", headers=bearer(keys["bob"]))).text
    assert line.startswith("Bobby")
    assert conn.execute("SELECT target FROM audit_log WHERE action='rename'").fetchone()["target"] == "bob->Bobby"


async def test_rename_rejects_taken_and_bad_names(env):  # noqa: F811
    gw, conn, ids, keys = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.post("/api/me/name", json={"name": "alice"}, headers=bearer(keys["bob"]))).status_code == 409
        assert (await c.post("/api/me/name", json={"name": " "}, headers=bearer(keys["bob"]))).status_code == 400
        assert (await c.post("/api/me/name", json={"name": "x" * 65}, headers=bearer(keys["bob"]))).status_code == 400
    assert conn.execute("SELECT name FROM users WHERE id=?", (ids["bob"],)).fetchone()["name"] == "bob"


async def test_cookie_session_rename_needs_csrf(env):  # noqa: F811
    gw, conn, ids, keys = env
    async with admin_client(gw) as c:
        token = c.headers.pop("x-csrf-token")
        assert (await c.post("/api/me/name", json={"name": "boss"})).status_code == 403
        c.headers["x-csrf-token"] = token
        assert (await c.post("/api/me/name", json={"name": "boss"})).json()["name"] == "boss"


async def test_admin_rename_still_works(env):  # noqa: F811
    gw, conn, ids, keys = env
    async with admin_client(gw) as c:
        assert (await c.post(f"/api/admin/users/{ids['bob']}/rename", json={"name": "alice"})).status_code == 409
        assert (await c.post(f"/api/admin/users/{ids['bob']}/rename", json={"name": "rob"})).status_code == 200
    assert conn.execute("SELECT name FROM users WHERE id=?", (ids["bob"],)).fetchone()["name"] == "rob"
