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
from tests.tickets_helpers import live


@pytest.fixture
def env(tickets_env, monkeypatch):
    gw, conn, cfg, ids, keys = tickets_env
    live(conn, time.time())   # a fresh weekly report: a sale needs one (2026-11-04 spec)
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 156250, ids["admin"], now=int(time.time()) - 3600)
    cfg.zarinpal = ZarinpalConfig()
    cfg.listener.home_url = "https://rahkar.pro"
    cfg.listener.dashboard_url = "https://dash.rahkar.pro"
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
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        assert r.status_code == 200, r.text
        pid, url = r.json()["payment_id"], r.json()["url"]
        assert url.startswith("https://payment.zarinpal.com/pg/StartPay/")
        auth = url.rsplit("/", 1)[1]
        r = await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"https://dash.rahkar.pro/dashboard#payment/{pid}"
        me = (await c.get(f"/api/me/payments/{pid}", headers=bearer(keys["alice"]))).json()
        assert (me["status"], me["amount"], me["ref_id"], me["label"]) == ("paid", 1250000, "201", "Lite")
        assert "card_pan" not in me and "authority" not in me
        assert (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["current"]["tier"] == "lite"
    async with admin_client(gw) as c:
        o = (await c.get("/api/admin/orders?status=all")).json()["orders"][0]
        assert o["payment"] == {"status": "paid", "amount": 1250000, "ref_id": "201", "card_pan": "502229******5995", "refund": None}


async def test_pay_an_open_order(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        oid = payments.get(conn, r.json()["payment_id"])["order_id"]
        r = await c.post(f"/api/me/orders/{oid}/pay", headers=bearer(keys["alice"]), json={})
        assert r.status_code == 200, r.text
        assert r.json()["url"].startswith("https://payment.zarinpal.com/pg/StartPay/")
        assert payments.get(conn, r.json()["payment_id"])["order_id"] == oid


async def test_pay_needs_an_email(env):
    gw, conn, cfg, ids, keys, zp = env
    conn.execute("UPDATE users SET email=NULL WHERE id=?", (ids["alice"],))
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        assert r.status_code == 400
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "email": "a@b.co", "terms_version": "v1"})
        assert r.status_code == 200
    assert len(zp.requests) == 1 and zp.requests[0]["email"] == "a@b.co"


async def test_pay_needs_sign_in_and_payments_on(env, monkeypatch):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        assert (await c.post("/api/orders/pay", json={"tier": "lite", "length": "week", "terms_version": "v1"})).status_code == 401
        monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        assert r.status_code == 404
        # ZarinPal only stays offered without the merchant ID: paying fails, no unpaid order instead
        r = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()
        assert r["pay"] is not None and r["manual"] is False


async def test_pay_prices_offered_even_with_a_stale_rate(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        pay = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"]
        assert pay["currency"] == "IRT"
        lite = next(t for t in pay["prices"]["tiers"] if t["tier"] == "lite")
        assert lite["lengths"]["week"]["amount"] == 1250000
        conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))
        assert (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"] is not None
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        assert r.status_code == 409


async def test_callback_unknown_and_cancel(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        assert (await c.get("/pay/callback?Authority=nope&Status=OK")).status_code == 404
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        assert (await c.get(f"/pay/callback?Authority={auth}&Status=NOK", follow_redirects=False)).status_code == 302
        me = (await c.get(f"/api/me/payments/{r.json()['payment_id']}", headers=bearer(keys["alice"]))).json()
        assert me["status"] == "cancelled"


async def test_someone_elses_payment_is_404(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
    async with admin_client(gw) as c:
        assert (await c.get(f"/api/me/payments/{r.json()['payment_id']}")).status_code == 404


async def test_callback_is_served_on_the_home_host(env):
    gw, conn, cfg, ids, keys, zp = env
    import httpx
    app = create_dashboard_app(gw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://rahkar.pro") as c:
        assert (await c.get("/pay/callback?Authority=nope&Status=OK")).status_code == 404   # served, not redirected


async def test_payment_result_hides_the_internal_reason_when_unfulfilled(env, monkeypatch):
    gw, conn, cfg, ids, keys, zp = env

    def full(*a, **k):
        raise tickets.CapacityError("sold out")
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        monkeypatch.setattr(tickets, "check_capacity", full)   # capacity is gone by the time the callback grants
        await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        me = (await c.get(f"/api/me/payments/{r.json()['payment_id']}", headers=bearer(keys["alice"]))).json()
    assert me["status"] == "paid_unfulfilled" and me["error_shown"] is None   # "sold out" never reaches the buyer


async def test_payment_result_shows_zarinpals_message_when_failed(env):
    gw, conn, cfg, ids, keys, zp = env
    zp.verify_answer = zarinpal.ZarinpalRefused(-51, "Session is not valid, session is not active paid try.")
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        me = (await c.get(f"/api/me/payments/{r.json()['payment_id']}", headers=bearer(keys["alice"]))).json()
    assert me["status"] == "failed" and me["error_shown"] == "-51: Session is not valid, session is not active paid try."


async def test_pay_needs_csrf_for_cookie_session(env):
    gw, conn, cfg, ids, keys, zp = env
    async with client(gw) as c:
        r = await c.post("/api/login/key", json={"key": keys["alice"]})
        assert r.status_code == 200, r.text
        assert (await c.post("/api/orders/pay", json={"tier": "lite", "length": "week", "terms_version": "v1"})).status_code == 403   # cookie, no CSRF
        c.headers["x-csrf-token"] = r.json()["csrf"]
        assert (await c.post("/api/orders/pay", json={"tier": "lite", "length": "week", "terms_version": "v1"})).status_code == 200


async def test_mail_once_per_payment(env, monkeypatch):
    gw, conn, cfg, ids, keys, zp = env
    from claude_proxy import mail, web
    from claude_proxy.config import EmailConfig
    import asyncio
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    sent = []
    monkeypatch.setattr(mail, "send", lambda email, to, subject, body, html=None: sent.append(to))
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        for _ in range(2):
            await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    assert sorted(sent) == ["admin@example.com", "alice@example.com"]


async def test_payment_mode_selects_zarinpal_manual_or_both(env):
    gw, conn, cfg, ids, keys, zp = env
    order = {"tier": "lite", "length": "week", "currency": "EUR", "terms_version": "v1"}
    pay = {"tier": "lite", "length": "week", "terms_version": "v1"}
    me = lambda c: c.get("/api/me/tickets", headers=bearer(keys["alice"]))  # noqa: E731
    async with client(gw) as c:   # default: ZarinPal only
        r = (await me(c)).json()
        assert r["manual"] is False and r["pay"] is not None
        r = await c.post("/api/me/orders", headers=bearer(keys["alice"]), json=order)
        assert r.status_code == 409 and "ZarinPal" in r.json()["error"]
    async with admin_client(gw) as c:
        assert (await c.get("/api/admin/orders")).json() | {"orders": []} == {"orders": [], "new": 0, "pay_mode": "zarinpal", "zarinpal_down": False, "mail_servers": None}
        assert (await c.post("/api/admin/orders/pay_mode", json={"mode": "yes"})).status_code == 400
        assert (await c.post("/api/admin/orders/pay_mode", json={"mode": "manual"})).json() == {"ok": True, "pay_mode": "manual"}
    async with client(gw) as c:   # manual only: no ZarinPal in the dialog, and the endpoint refuses
        r = (await me(c)).json()
        assert r["manual"] is True and r["pay"] is None
        assert (await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json=pay)).status_code == 409
        assert (await c.post("/api/me/orders", headers=bearer(keys["alice"]), json=order)).status_code == 200
    assert conn.execute("SELECT target FROM audit_log WHERE action='pay_mode'").fetchall()[0][0] == "manual"
    payments.set_pay_mode(conn, ids["admin"], "both")
    async with client(gw) as c:
        r = (await me(c)).json()
        assert r["manual"] is True and r["pay"] is not None
    payments.set_pay_mode(conn, ids["admin"], "zarinpal")
    conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))   # ZarinPal can't quote
    async with client(gw) as c:   # ZarinPal only stays ZarinPal only: the button shows, paying fails, no unpaid order
        r = (await me(c)).json()
        assert r["manual"] is False and r["pay"] is not None
        assert (await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json=pay)).status_code == 409
        assert (await c.post("/api/me/orders", headers=bearer(keys["alice"]), json=order)).status_code == 409
    async with admin_client(gw) as c:
        r = (await c.get("/api/admin/orders")).json()
        assert (r["pay_mode"], r["zarinpal_down"]) == ("zarinpal", True)


def test_pay_mode_reads_the_old_manual_switch(env):
    gw, conn, cfg, ids, keys, zp = env
    conn.execute("INSERT INTO settings(key, value) VALUES('manual_orders', '1')")
    assert payments.pay_mode(conn, cfg) == "both"
    payments.set_pay_mode(conn, ids["admin"], "zarinpal")
    assert payments.pay_mode(conn, cfg) == "zarinpal"


async def test_pay_without_a_toman_rate_has_no_prices_and_manual_only_says_so_first(env):
    gw, conn, cfg, ids, keys, zp = env
    conn.execute("DELETE FROM fx_rates WHERE currency='IRT'")
    pay = {"tier": "lite", "length": "week", "terms_version": "v1"}
    async with client(gw) as c:
        assert (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"] == {"currency": "IRT", "prices": None, "vat_percent": 0}
        payments.set_pay_mode(conn, ids["admin"], "manual")
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json=pay)
        assert r.status_code == 409 and r.json()["error"] == payments.MANUAL_ONLY

async def test_the_price_shown_is_the_price_charged_discount_included(env):
    gw, conn, cfg, ids, keys, zp = env
    now = int(time.time())
    payments.set_pay_mode(conn, ids["admin"], "both")
    tickets.create_discount(conn, cfg, "lite", "week", 6.4, now - 10, now + 86400, ids["admin"], now=now - 10)
    body = {"tier": "lite", "length": "week", "terms_version": "v1"}
    lite_week = lambda table: next(t for t in table["tiers"] if t["tier"] == "lite")["lengths"]["week"]  # noqa: E731
    async with client(gw) as c:
        shown = (await c.get("/api/pricing")).json()
        assert (shown["currency"], lite_week(shown)["usd"], lite_week(shown)["amount"]) == ("EUR", 6.4, 6.0)   # 5.888 to the half euro
        r = await c.post("/api/me/orders", headers=bearer(keys["alice"]), json=body | {"currency": "EUR"})
        assert r.status_code == 200, r.text
        o = r.json()["order"]
        assert (o["currency"], o["quoted_amount"]) == ("EUR", lite_week(shown)["amount"])
        assert (await c.post(f"/api/me/orders/{o['id']}/withdraw", headers=bearer(keys["alice"]))).status_code == 200
        pay = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"]
        assert (pay["currency"], lite_week(pay["prices"])["amount"]) == ("IRT", 1000000)   # $6.40 at 156,250
        cfg.zarinpal.vat_percent = 10   # on top of every currency's prices
        shown = (await c.get("/api/pricing")).json()
        assert (shown["vat_percent"], lite_week(shown)["amount"], lite_week(shown)["usd_total"]) == (10, 6.5, 7.04)   # 6.4768 to the half euro
        vat_pay = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["pay"]["prices"]
        assert (vat_pay["vat_percent"], lite_week(vat_pay)["amount"]) == (10, 1100000)
        cfg.zarinpal.vat_percent = 0
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json=body)
        assert r.status_code == 200, r.text
    o = conn.execute("SELECT o.quoted_amount, p.amount FROM orders o JOIN payments p ON p.order_id=o.id").fetchone()
    assert zp.requests[-1]["amount"] == int(o["quoted_amount"]) == o["amount"] == lite_week(pay["prices"])["amount"]


async def test_refund_within_15_minutes(env, monkeypatch):
    gw, conn, cfg, ids, keys, zp = env
    reversed_ = []

    async def reverse(merchant_id, authority, sandbox):
        reversed_.append(authority)
    monkeypatch.setattr(zarinpal, "reverse", reverse)
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        [offer] = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["refunds"]
        assert (offer["tier"], offer["amount"], offer["currency"]) == ("lite", 1250000, "IRT")
        r = await c.post(f"/api/me/payments/{offer['payment_id']}/refund", headers=bearer(keys["alice"]), json={})
        assert r.status_code == 200, r.text
        assert reversed_ == [auth]
        tk = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()
        assert tk["refunds"] == [] and tk["current"] is None
        r = await c.post(f"/api/me/payments/{offer['payment_id']}/refund", headers=bearer(keys["alice"]), json={})
        assert r.status_code == 409 and r.json()["error"] == "This pass can no longer be refunded."
    async with admin_client(gw) as c:
        assert (await c.get("/api/admin/orders?status=all")).json()["orders"][0]["payment"]["refund"] == "done"


WIN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15"


@pytest.mark.parametrize("ua", [WIN_UA, MAC_UA])
async def test_a_windows_buyer_gets_the_windows_setup_in_the_pass_mail(env, monkeypatch, ua):
    gw, conn, cfg, ids, keys, zp = env
    from claude_proxy import mail, web
    from claude_proxy.config import EmailConfig
    import asyncio
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    cfg.listener.public_url = "https://gw.rahkar.pro"
    sent = []
    monkeypatch.setattr(mail, "send", lambda email, to, subject, body, html=None: sent.append((to, body, html)))
    async with client(gw) as c:
        r = await c.post("/api/orders/pay", headers=bearer(keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        await c.get(f"/pay/callback?Authority={auth}&Status=OK", headers={"user-agent": ua}, follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    (_, admin, _), (_, body, html) = sent
    url = "https://dash.rahkar.pro/install.cmd"
    assert url not in admin
    if ua == WIN_UA:
        assert "Set up on Windows:" in body and url in body and f'<a href="{url}">{url}</a>' in html
    else:
        assert "install.cmd" not in body


async def test_install_cmd_runs_the_powershell_install(env):
    gw, conn, cfg, ids, keys, zp = env
    cfg.listener.public_url = "https://gw.rahkar.pro"
    async with client(gw) as c:
        r = await c.get("/install.cmd")
        assert r.status_code == 200
        assert r.headers["content-disposition"] == 'attachment; filename="claude-gateway-setup.cmd"'
        s = r.content.decode("ascii")
        assert s.startswith("@echo off\r\n") and s.endswith("pause\r\n")
        assert "powershell -NoProfile -ExecutionPolicy Bypass -Command \"irm 'https://dash.rahkar.pro/install.ps1' | iex\"" in s
        cfg.listener.public_url = ""
        assert (await c.get("/install.cmd")).status_code == 503
