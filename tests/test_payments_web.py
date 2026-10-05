"""Payments design, sections 4 and 6: the endpoints, with ZarinPal faked."""
import time

import pytest

from claude_proxy import payments, tickets, zarinpal
from claude_proxy.config import Currency, ZarinpalConfig
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_payments import MID, FakeZP
from tests.test_tickets_web import env as tickets_env  # noqa: F401  (the fixture)
from tests.test_web import admin_client, bearer


@pytest.fixture
def env(tickets_env, monkeypatch):
    gw, conn, cfg, ids, keys = tickets_env
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 156250, ids["admin"], now=int(time.time()) - 3600)
    cfg.zarinpal = ZarinpalConfig()
    cfg.listener.home_url = "https://rahkar.pro"
    cfg.listener.dashboard_url = "https://claude-dash.rahkar.pro"
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    zp = FakeZP()
    monkeypatch.setattr(zarinpal, "request", zp.request)
    monkeypatch.setattr(zarinpal, "verify", zp.verify)
    conn.execute("UPDATE users SET email='alice@example.com' WHERE id=?", (ids["alice"],))
    return gw, conn, cfg, ids, keys, zp


def client(gw):
    return asgi_client(create_dashboard_app(gw))


async def test_pay_redirect_callback_and_result(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
        assert r.status_code == 200, r.text
        pid, url = r.json()["payment_id"], r.json()["url"]
        assert url.startswith("https://payment.zarinpal.com/pg/StartPay/")
        auth = url.rsplit("/", 1)[1]
        r = await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"https://claude-dash.rahkar.pro/dashboard#payment/{pid}"
        me = (await c.get(f"/api/me/payments/{pid}", headers=bearer(keys["alice"]))).json()
        assert (me["status"], me["amount"], me["ref_id"], me["label"]) == ("paid", 1250000, "201", "Lite")
        assert "card_pan" not in me and "authority" not in me
        assert (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["current"]["tier"] == "lite"
    async with admin_client(gw) as c:
        o = (await c.get("/api/admin/orders?status=all")).json()["orders"][0]
        assert o["payment"] == {"status": "paid", "amount": 1250000, "ref_id": "201", "card_pan": "502229******5995"}


async def test_pay_needs_an_email(env):
    gw, conn, cfg, ids, keys, zp = env
    conn.execute("UPDATE users SET email=NULL WHERE id=?", (ids["alice"],))
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
        assert r.status_code == 400
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "email": "a@b.co"})
        assert r.status_code == 200
    assert len(zp.requests) == 1 and zp.requests[0]["email"] == "a@b.co"


async def test_pay_needs_sign_in_and_payments_on(env, monkeypatch):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        assert (await c.post("/api/orders/pay", json={"tier": "lite", "length": "week"})).status_code == 401
        monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
        assert r.status_code == 404
        assert (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"] is None


async def test_pay_prices_offered_only_with_a_fresh_rate(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        pay = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"]
        assert pay["currency"] == "IRT"
        lite = next(t for t in pay["prices"]["tiers"] if t["tier"] == "lite")
        assert lite["lengths"]["week"]["amount"] == 1250000
        conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))
        assert (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"] is None


async def test_callback_unknown_and_cancel(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        assert (await c.get("/pay/callback?Authority=nope&Status=OK")).status_code == 404
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        assert (await c.get(f"/pay/callback?Authority={auth}&Status=NOK", follow_redirects=False)).status_code == 302
        me = (await c.get(f"/api/me/payments/{r.json()['payment_id']}", headers=bearer(keys["alice"]))).json()
        assert me["status"] == "cancelled"


async def test_someone_elses_payment_is_404(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
    async with admin_client(gw) as c:
        assert (await c.get(f"/api/me/payments/{r.json()['payment_id']}")).status_code == 404


async def test_callback_is_served_on_the_home_host(env):
    gw, conn, cfg, ids, keys, zp = env
    import httpx
    app = create_dashboard_app(gw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://rahkar.pro") as c:
        assert (await c.get("/pay/callback?Authority=nope&Status=OK")).status_code == 404   # served, not redirected


async def test_mail_once_per_payment(env, monkeypatch):
    gw, conn, cfg, ids, keys, zp = env
    from claude_proxy import mail, web
    from claude_proxy.config import EmailConfig
    import asyncio
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    sent = []
    monkeypatch.setattr(mail, "send", lambda email, to, subject, body: sent.append(to))
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        for _ in range(2):
            await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    assert sorted(sent) == ["admin@example.com", "alice@example.com"]
