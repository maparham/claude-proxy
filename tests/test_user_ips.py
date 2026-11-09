"""Each user's IP history (user_ips): gateway requests and dashboard sign-ins, shown to the admin only."""
import pytest

from claude_proxy import db as dbm
from claude_proxy.app import create_app
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import PW, admin_client, bearer, env  # noqa: F401  (fixture)

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


def test_cleanup_drops_addresses_past_retention(db):
    conn = db[1]
    uid, _ = dbm.create_user(conn, "dave")
    dbm.note_ip(conn, uid, "1.2.3.4", "gateway")
    conn.execute("UPDATE user_ips SET last_seen=last_seen-400*86400")
    dbm.note_ip(conn, uid, "5.6.7.8", "gateway")
    assert dbm.cleanup(conn, 180, keep_usage=True)["user_ips"] == 0   # kept with the usage
    assert dbm.cleanup(conn, 180)["user_ips"] == 1
    assert [r["ip"] for r in dbm.user_ips(conn, uid)] == ["5.6.7.8"]


async def test_users_table_shows_the_latest_request_address_not_a_sign_in(env):  # noqa: F811
    gw, conn, ids, keys = env
    dbm.note_ip(conn, ids["alice"], "1.1.1.1", "gateway")
    conn.execute("UPDATE user_ips SET last_seen=last_seen-3600")
    dbm.note_ip(conn, ids["alice"], "2.2.2.2", "dashboard")
    async with admin_client(gw) as c:
        users = {u["name"]: u for u in (await c.get("/api/users")).json()["users"]}
        assert users["alice"]["last_ip"] == "1.1.1.1"
        assert users["admin"]["last_ip"] is None   # signed in, never sent a request
        assert (await c.get(f"/api/admin/users/{ids['alice']}/ips")).json()["more"] is False


class _Gw:
    """Just what geo.locate uses: an http client and a db runner."""
    def __init__(self, conn, handler):
        import httpx
        from claude_proxy.dbpool import DbPool
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.db = DbPool(conn)


async def test_a_new_address_is_located_once_and_a_failed_lookup_stores_nothing(db):
    import httpx
    from claude_proxy import geo
    conn = db[1]
    uid, _ = dbm.create_user(conn, "erin")
    asked = []

    def handler(req):
        asked.append(req.url.path)
        if "203.0.113.9" in req.url.path:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"ip": "8.8.8.8", "city": "Mountain View", "region": "California", "country": "US"})
    gw = _Gw(conn, handler)
    assert dbm.note_ip(conn, uid, "8.8.8.8", "gateway") is True
    assert dbm.note_ip(conn, uid, "8.8.8.8", "gateway") is False   # seen before: no new lookup
    await geo.locate(gw, "8.8.8.8")
    await geo.locate(gw, "8.8.8.8")          # known: not asked again
    await geo.locate(gw, "127.0.0.1")        # not public: never sent
    await geo.locate(gw, "203.0.113.9")      # documentation range: not public either
    assert asked == ["/8.8.8.8/json"]
    row = dbm.user_ips(conn, uid)[0]
    assert (row["city"], row["region"], row["country"]) == ("Mountain View", "California", "US")

    geo_rows = conn.execute("SELECT COUNT(*) FROM ip_geo").fetchone()[0]
    gw2 = _Gw(conn, lambda req: httpx.Response(429, json={}))
    await geo.locate(gw2, "1.1.1.1")
    assert conn.execute("SELECT COUNT(*) FROM ip_geo").fetchone()[0] == geo_rows


async def test_proxy_and_sign_in_look_up_a_new_address_only(env, monkeypatch):  # noqa: F811
    import asyncio

    import httpx
    from claude_proxy import geo
    gw, conn, ids, keys = env
    looked = []

    async def fake_locate(gw_, ip):
        looked.append(ip)
    monkeypatch.setattr(geo, "locate", fake_locate)
    transport = httpx.ASGITransport(app=create_app(gw), client=("8.8.4.4", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        for _ in range(2):
            await c.get("/v1/models", headers=bearer(keys["alice"]))
    transport = httpx.ASGITransport(app=create_dashboard_app(gw), client=("9.9.9.9", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post("/api/login", json={"username": "admin", "password": PW})).status_code == 200
    await asyncio.sleep(0)
    assert sorted(looked) == ["8.8.4.4", "9.9.9.9"]   # the second request from 8.8.4.4 asks nothing
