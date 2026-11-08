"""Each user's IP history (user_ips): gateway requests and dashboard sign-ins, shown to the admin only."""
import pytest

from claude_proxy import db as dbm
from claude_proxy.app import create_app
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import admin_client, bearer, env  # noqa: F401  (fixture)

pytestmark = pytest.mark.anyio


def test_note_ip_counts_per_address_and_source(db):
    conn = db[1]
    uid, _ = dbm.create_user(conn, "carol")
    for ip, src in (("1.2.3.4", "gateway"), ("1.2.3.4", "gateway"), ("1.2.3.4", "dashboard"), ("5.6.7.8", "gateway")):
        dbm.note_ip(conn, uid, ip, src)
    dbm.note_ip(conn, None, "9.9.9.9", "gateway")      # unauthenticated: nothing to attach it to
    dbm.note_ip(conn, uid, "unknown", "gateway")
    rows = {(r["ip"], r["source"]): r["count"] for r in dbm.user_ips(conn, uid)}
    assert rows == {("1.2.3.4", "gateway"): 2, ("1.2.3.4", "dashboard"): 1, ("5.6.7.8", "gateway"): 1}


async def test_requests_and_sign_ins_are_recorded_and_only_the_admin_sees_them(env):  # noqa: F811
    gw, conn, ids, keys = env
    async with asgi_client(create_app(gw)) as c:   # ASGITransport's client is 127.0.0.1
        await c.get("/v1/models", headers=bearer(keys["alice"]))
        await c.get("/v1/models", headers=bearer("sk-proxy-nope"))   # refused: no user, no row
    async with admin_client(gw) as c:
        r = await c.get(f"/api/admin/users/{ids['alice']}/ips")
        assert r.status_code == 200
        assert [(x["ip"], x["source"], x["count"]) for x in r.json()["ips"]] == [("127.0.0.1", "gateway", 1)]
        r = await c.get(f"/api/admin/users/{ids['admin']}/ips")
        assert [(x["ip"], x["source"]) for x in r.json()["ips"]] == [("127.0.0.1", "dashboard")]
        users = {u["name"]: u for u in (await c.get("/api/users")).json()["users"]}
        assert (users["alice"]["last_ip"], users["bob"]["last_ip"]) == ("127.0.0.1", None)
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get(f"/api/admin/users/{ids['alice']}/ips", headers=bearer(keys["alice"]))
        assert r.status_code == 403
        assert "127.0.0.1" not in (await c.get("/api/me/status", headers=bearer(keys["alice"]))).text
