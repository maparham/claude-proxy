"""Admin payment links (spec 2026-11-04): the endpoints."""
import time

from claude_proxy import db as dbm, orders
from tests.test_payments_web import client, env  # noqa: F401  (the fixture)
from tests.test_tickets_web import env as tickets_env  # noqa: F401  (env's own dependency)
from tests.test_web import admin_client, bearer


async def make(gw, **kw):
    async with admin_client(gw) as c:
        r = await c.post("/api/admin/orders/link", json={"email": "alice@example.com", "tier": "lite", "length": "week",
                                                         "pay_by": int(time.time()) + 86400} | kw)
    assert r.status_code == 200, r.text
    return r.json()


async def test_admin_quotes_creates_and_lists_a_link(env):
    gw, conn, cfg, ids, keys, zp = env
    async with admin_client(gw) as c:
        assert (await c.get("/api/admin/orders/link_quote?tier=lite&length=week")).json() == {"amount": 1250000}
        assert (await c.get("/api/admin/orders/link_quote?tier=gold&length=week")).status_code == 400
    r = await make(gw, note="hi")
    assert r["url"] == f"https://dash.rahkar.pro/pay/{r['order']['pay_code']}"
    async with admin_client(gw) as c:
        row = (await c.get("/api/admin/orders")).json()["orders"][0]
        assert row["pay_url"] == r["url"]
        assert (await c.post("/api/admin/orders/link", json={"email": "x", "tier": "lite", "length": "week",
                                                             "pay_by": int(time.time()) + 60})).status_code == 400


async def test_creating_needs_the_admin(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.post("/api/admin/orders/link", headers=bearer(keys["alice"]), json={})
    assert r.status_code == 403


async def test_pay_code_redirects_without_a_lookup(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.get("/pay/anything-at-all", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "https://dash.rahkar.pro/dashboard#paylink/anything-at-all"
        assert (await c.get("/pay/bad%20code", follow_redirects=False)).status_code == 404
        r = await c.get("/pay/callback?Authority=nope&Status=OK", follow_redirects=False)
        assert r.status_code == 404   # still the callback, not the link redirect


async def test_view_and_pay_a_link(env):
    gw, conn, cfg, ids, keys, zp = env
    code = (await make(gw, note="secret note"))["order"]["pay_code"]
    async with client(gw) as c:
        v = (await c.get(f"/api/me/paylinks/{code}", headers=bearer(keys["alice"]))).json()
        assert (v["state"], v["amount"], v["label"], v["length"], v["terms_version"]) == ("open", 1250000, "Lite", "week", "v1")
        assert "secret note" not in str(v) and "pay_code" not in v and "email" not in v
        assert (await c.get("/api/me/paylinks/nope", headers=bearer(keys["alice"]))).json()["state"] == "invalid"
        r = await c.post(f"/api/me/paylinks/{code}/pay", headers=bearer(keys["alice"]), json={"terms_version": "v1"})
        assert r.status_code == 200 and r.json()["url"].startswith("https://payment.zarinpal.com/pg/StartPay/")
        auth = r.json()["url"].rsplit("/", 1)[1]
        r = await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        assert r.headers["location"].startswith("https://dash.rahkar.pro/dashboard#payment/")
        v = (await c.get(f"/api/me/paylinks/{code}", headers=bearer(keys["alice"]))).json()
        assert (v["state"], v["unfulfilled"]) == ("paid", False)


async def test_someone_else_sees_only_the_masked_email(env):
    gw, conn, cfg, ids, keys, zp = env
    code = (await make(gw, email="bob@example.com"))["order"]["pay_code"]
    async with client(gw) as c:
        v = (await c.get(f"/api/me/paylinks/{code}", headers=bearer(keys["alice"]))).json()
        assert v == {"state": "other_email", "email": "bo…@example.com",
                     "message": "This link is for bo…@example.com; sign in with that email."}
        r = await c.post(f"/api/me/paylinks/{code}/pay", headers=bearer(keys["alice"]), json={"terms_version": "v1"})
        assert r.status_code == 403
        assert (await c.get(f"/api/me/paylinks/{code}")).status_code == 401
