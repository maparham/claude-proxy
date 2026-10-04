import asyncio
import threading
import time

import pytest

from claude_proxy import cli, mail, orders, tickets, turnstile, web
from claude_proxy.config import EmailConfig
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_tickets_web import env, grant  # noqa: F401  (the fixture)
from tests.test_web import admin_client, bearer
from tests.tickets_helpers import user

ORDER = {"tier": "lite", "length": "week", "currency": "EUR"}
VISITOR = ORDER | {"name": "Vera Visitor", "email": "vera@example.com", "message": "Hello", "turnstile_token": "tok"}
HIDDEN = ("ip", "admin_note", "admin_mail", "buyer_mail", "ticket_id")


@pytest.fixture
def ts(env, monkeypatch):
    """Turnstile on, with a stubbed verifier: set `ts.answer` to True, False or an exception."""
    gw, conn, cfg, ids, keys = env
    cfg.tickets.turnstile_site_key = "0xSITE"
    monkeypatch.setenv("TURNSTILE_SECRET", "sec")

    class Stub:
        def __init__(self):
            self.answer, self.calls = True, []

        async def verify(self, secret, token, ip):
            self.calls.append((secret, token, ip))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
    stub = Stub()
    monkeypatch.setattr(turnstile, "verify", stub.verify)
    return stub


@pytest.fixture
def sent(env, monkeypatch):
    """[email] configured and mail.send faked: the list of (to, subject) sent."""
    gw, conn, cfg, ids, keys = env
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    out = []
    monkeypatch.setattr(mail, "send", lambda email, to, subject, body: out.append((to, subject)))
    return out


def client(gw):
    return asgi_client(create_dashboard_app(gw))


async def drain():
    """Wait for the mail tasks the endpoints scheduled."""
    await asyncio.gather(*list(web._mail_tasks))


def only_order(conn):
    rows = conn.execute("SELECT * FROM orders").fetchall()
    assert len(rows) == 1
    return dict(rows[0])


# ---------- the public order ----------

async def test_public_order_needs_turnstile_config(env, monkeypatch):
    gw, conn, cfg, ids, keys = env
    monkeypatch.delenv("TURNSTILE_SECRET", raising=False)
    async with client(gw) as c:
        assert (await c.post("/api/orders", json=VISITOR)).status_code == 404
        cfg.tickets.turnstile_site_key = "0xSITE"   # a site key without the secret is still off
        assert (await c.post("/api/orders", json=VISITOR)).status_code == 404
        assert "turnstile_site_key" not in (await c.get("/api/pricing")).json()


async def test_public_order_is_stored_with_the_ip(env, ts):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        r = await c.post("/api/orders", json=VISITOR)
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert (await c.get("/api/pricing")).json()["turnstile_site_key"] == "0xSITE"
    o = only_order(conn)
    assert (o["name"], o["email"], o["message"], o["user_id"], o["ip"], o["quoted_amount"]) == (
        "Vera Visitor", "vera@example.com", "Hello", None, "127.0.0.1", 7.5)
    assert (o["admin_mail"], o["buyer_mail"]) == ("off", "off")
    assert ts.calls == [("sec", "tok", "127.0.0.1")]


async def test_failed_verification_is_400_and_stores_nothing(env, ts):
    gw, conn, cfg, ids, keys = env
    ts.answer = False
    async with client(gw) as c:
        r = await c.post("/api/orders", json=VISITOR)
        assert r.status_code == 400 and r.json()["error"] == "The verification failed; please try again."
        r = await c.post("/api/orders", json=VISITOR | {"turnstile_token": ""})
        assert r.status_code == 400 and r.json()["error"] == "The verification failed; please try again."
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


async def test_unreachable_cloudflare_is_503_with_retry_after(env, ts):
    gw, conn, cfg, ids, keys = env
    ts.answer = turnstile.TurnstileUnavailable("timeout")
    async with client(gw) as c:
        r = await c.post("/api/orders", json=VISITOR)
    assert r.status_code == 503 and r.headers["retry-after"] == "30"
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


async def test_public_order_input_errors(env, ts):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        assert (await c.post("/api/orders", json=VISITOR | {"email": "nope"})).status_code == 400
        assert (await c.post("/api/orders", json=VISITOR | {"tier": "gold"})).status_code == 400
        assert (await c.post("/api/orders", content=b"[1]", headers={"content-type": "application/json"})).status_code == 400


async def test_double_submit_never_500(env, ts):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        rs = await asyncio.gather(*[c.post("/api/orders", json=VISITOR) for _ in range(5)])
    assert {r.status_code for r in rs} <= {200, 429} and [r.status_code for r in rs].count(200) == 3
    assert all(r.json()["error"] == "Too many orders today; try again tomorrow or sign in." for r in rs if r.status_code == 429)


async def test_the_response_does_not_wait_for_the_mail(env, ts, monkeypatch):
    gw, conn, cfg, ids, keys = env
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    release, sent = threading.Event(), []

    def slow_send(email, to, subject, body):
        assert release.wait(10)
        sent.append(to)
    monkeypatch.setattr(mail, "send", slow_send)
    async with client(gw) as c:
        r = await asyncio.wait_for(c.post("/api/orders", json=VISITOR), 5)
    assert r.status_code == 200
    o = only_order(conn)
    assert (o["admin_mail"], o["buyer_mail"]) == ("pending", "pending") and sent == []
    release.set()
    await drain()
    o = only_order(conn)
    assert (o["admin_mail"], o["buyer_mail"]) == ("sent", "sent") and sent == ["admin@example.com", "vera@example.com"]


async def test_a_failing_mail_server_records_failed(env, ts, monkeypatch):
    gw, conn, cfg, ids, keys = env
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")

    def down(*a):
        raise OSError("connection refused")
    monkeypatch.setattr(mail, "send", down)
    async with client(gw) as c:
        assert (await c.post("/api/orders", json=VISITOR)).status_code == 200
    await drain()
    o = only_order(conn)
    assert (o["status"], o["admin_mail"], o["buyer_mail"]) == ("new", "failed", "failed")


# ---------- the signed-in order ----------

async def test_signed_in_order_uses_the_account_email(env, sent):
    gw, conn, cfg, ids, keys = env
    conn.execute("UPDATE users SET email='alice@example.com' WHERE id=?", (ids["alice"],))
    async with client(gw) as c:
        r = await c.post("/api/me/orders", headers=bearer(keys["alice"]), json=ORDER | {"email": "other@example.com", "message": "hi"})
        assert r.status_code == 200, r.text
        m = r.json()["order"]
        assert (m["status"], m["email"], m["label"], m["quoted_amount"], m["message"]) == ("new", "alice@example.com", "Lite", 7.5, "hi")
        assert not set(HIDDEN) & set(m)
        r = await c.post("/api/me/orders", headers=bearer(keys["alice"]), json=ORDER)
        assert r.status_code == 409 and r.json()["error"] == "You already have an open order."
    await drain()
    assert sent == [("admin@example.com", "New order: Lite week"), ("alice@example.com", "Your order: Lite week")]
    o = only_order(conn)
    assert (o["user_id"], o["name"], o["ip"]) == (ids["alice"], "alice", None)


async def test_a_typed_email_is_stored_on_the_order_only(env):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        assert (await c.post("/api/me/orders", headers=bearer(keys["alice"]), json=ORDER)).status_code == 400   # no email at all
        r = await c.post("/api/me/orders", headers=bearer(keys["alice"]), json=ORDER | {"email": "typed@example.com"})
        assert r.status_code == 200 and r.json()["order"]["email"] == "typed@example.com"
    assert user(conn, ids["alice"])["email"] is None


async def test_signed_in_order_needs_csrf_and_a_full_key(env):
    gw, conn, cfg, ids, keys = env
    from claude_proxy import db as dbm
    routes_key = dbm.set_routes_key(conn, ids["alice"])
    async with client(gw) as c:
        assert (await c.post("/api/me/orders", json=ORDER)).status_code == 401
        assert (await c.post("/api/me/orders", headers=bearer(routes_key), json=ORDER | {"email": "a@b.cd"})).status_code == 403
        r = await c.post("/api/login/key", json={"key": keys["alice"]})
        assert (await c.post("/api/me/orders", json=ORDER | {"email": "a@b.cd"})).status_code == 403   # cookie without CSRF
        c.headers["x-csrf-token"] = r.json()["csrf"]
        assert (await c.post("/api/me/orders", json=ORDER | {"email": "a@b.cd"})).status_code == 200


async def test_sold_out_is_409(env):
    gw, conn, cfg, ids, keys = env
    cfg.tickets.max_sold_pct = 5
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["admin"]), "lite", "week", "EUR", now=time.time())
    async with client(gw) as c:
        r = await c.post("/api/me/orders", headers=bearer(keys["alice"]), json=ORDER | {"email": "a@b.cd"})
    assert r.status_code == 409 and "sold out" in r.json()["error"]


async def test_me_orders_withdraw_and_dismiss(env):
    gw, conn, cfg, ids, keys = env
    h = bearer(keys["alice"])
    async with client(gw) as c:
        assert (await c.get("/api/me/orders", headers=h)).json() == {"order": None}
        oid = (await c.post("/api/me/orders", headers=h, json=ORDER | {"email": "a@b.cd"})).json()["order"]["id"]
        orders.set_note(conn, ids["admin"], oid, "secret note")
        m = (await c.get("/api/me/orders", headers=h)).json()["order"]
        assert m["id"] == oid and not set(HIDDEN) & set(m) and "secret note" not in str(m)
        assert (await c.post(f"/api/me/orders/{oid}/dismiss", headers=h)).status_code == 409   # still open
        r = await c.post(f"/api/me/orders/{oid}/withdraw", headers=h)
        assert r.status_code == 200 and r.json()["order"] is None   # withdrawn: no notice
        assert (await c.post(f"/api/me/orders/{oid}/withdraw", headers=h)).status_code == 409
        oid = (await c.post("/api/me/orders", headers=h, json=ORDER | {"email": "a@b.cd"})).json()["order"]["id"]
        orders.set_status(conn, ids["admin"], oid, "declined", note="no reason for you")
        m = (await c.get("/api/me/orders", headers=h)).json()["order"]
        assert m["status"] == "declined" and "no reason" not in str(m)
        r = await c.post(f"/api/me/orders/{oid}/dismiss", headers=h)
        assert r.status_code == 200 and r.json()["order"] is None
        assert (await c.get("/api/me/orders", headers=h)).json() == {"order": None}


async def test_users_never_see_or_touch_other_orders(env, ts):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        await c.post("/api/orders", json=VISITOR)
        vid = only_order(conn)["id"]
        assert (await c.get("/api/me/orders", headers=bearer(keys["alice"]))).json() == {"order": None}
        assert (await c.post(f"/api/me/orders/{vid}/withdraw", headers=bearer(keys["alice"]))).status_code == 404
        assert (await c.post(f"/api/me/orders/{vid}/dismiss", headers=bearer(keys["alice"]))).status_code == 404
        assert (await c.get("/api/admin/orders", headers=bearer(keys["alice"]))).status_code == 403
        assert (await c.post(f"/api/admin/orders/{vid}", headers=bearer(keys["alice"]), json={"action": "contacted"})).status_code == 403
        assert "vera" not in (await c.get("/api/pricing")).text.lower()
    assert only_order(conn)["status"] == "new"


# ---------- admin ----------

async def test_admin_list_actions_and_audit(env, ts):
    gw, conn, cfg, ids, keys = env
    conn.execute("UPDATE users SET email='Vera@Example.com' WHERE id=?", (ids["alice"],))
    async with client(gw) as c:
        await c.post("/api/orders", json=VISITOR | {"name": "<img src=x onerror=alert(1)>"})
    oid = only_order(conn)["id"]
    async with admin_client(gw) as c:
        r = (await c.get("/api/admin/orders")).json()
        assert r["new"] == 1 and len(r["orders"]) == 1
        o = r["orders"][0]
        assert (o["id"], o["name"], o["ip"], o["admin_mail"], o["label"], o["user_name"]) == (oid, "<img src=x onerror=alert(1)>", "127.0.0.1", "off", "Lite", None)
        assert o["suggested_user"] == {"id": ids["alice"], "name": "alice", "email": "Vera@Example.com"}
        assert (await c.get("/api/session")).json()["tickets"]["orders_new"] == 1
        r = await c.post(f"/api/admin/orders/{oid}", json={"action": "contacted"})
        assert r.status_code == 200 and r.json()["order"]["status"] == "contacted"
        assert (await c.post(f"/api/admin/orders/{oid}", json={"action": "contacted"})).status_code == 409
        r = await c.post(f"/api/admin/orders/{oid}", json={"action": "note", "note": "called her"})
        assert r.json()["order"]["admin_note"] == "called her"
        assert (await c.post(f"/api/admin/orders/{oid}", json={"action": "decline"})).status_code == 400   # needs a note
        r = await c.post(f"/api/admin/orders/{oid}", json={"action": "decline", "note": "no capacity"})
        assert r.json()["order"]["status"] == "declined" and r.json()["order"]["ip"] is None
        assert (await c.post(f"/api/admin/orders/{oid}", json={"action": "bogus"})).status_code == 400
        assert (await c.post("/api/admin/orders/999", json={"action": "contacted"})).status_code == 404
        assert (await c.get("/api/admin/orders")).json() == {"orders": [], "new": 0}
        assert len((await c.get("/api/admin/orders?status=all")).json()["orders"]) == 1
        assert len((await c.get("/api/admin/orders?status=declined")).json()["orders"]) == 1
        assert (await c.get("/api/admin/orders?status=nope")).status_code == 400
    assert [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'order_%' ORDER BY id")] == [
        "order_new", "order_contacted", "order_note", "order_declined"]


async def test_link_and_grant_from_a_visitor_order(env, ts):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        await c.post("/api/orders", json=VISITOR)
    oid = only_order(conn)["id"]
    async with admin_client(gw) as c:
        r = await c.post(f"/api/admin/orders/{oid}", json={"action": "link", "user_id": 999})
        assert r.status_code == 404
        r = await c.post(f"/api/admin/orders/{oid}", json={"action": "link", "create": True})
        assert r.status_code == 200
        uid = r.json()["order"]["user_id"]
        assert user(conn, uid)["name"] == "vera@example.com" and user(conn, uid)["email"] is None
        # Granting to someone else than the linked user is refused, and nothing is granted.
        r = await grant(c, ORDER | {"user": "alice", "order_id": oid})
        assert r.status_code == 409 and r.json()["error"] == "This order was withdrawn or changed; reload."
        assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
        r = await grant(c, ORDER | {"user": uid, "order_id": oid})
        assert r.status_code == 200, r.text
        o = (await c.get("/api/admin/orders?status=done")).json()["orders"][0]
        assert (o["id"], o["ticket_id"], o["user_name"]) == (oid, r.json()["ticket"]["id"], "vera@example.com")
        assert (await c.post("/api/admin/tickets", json=ORDER | {"user": uid, "usd": 8, "rate": 0.92, "order_id": "x"})).status_code == 400


async def test_link_409_carries_the_existing_user(env, ts):
    gw, conn, cfg, ids, keys = env
    conn.execute("UPDATE users SET email='vera@example.com' WHERE id=?", (ids["alice"],))
    async with client(gw) as c:
        await c.post("/api/orders", json=VISITOR)
    oid = only_order(conn)["id"]
    async with admin_client(gw) as c:
        r = await c.post(f"/api/admin/orders/{oid}", json={"action": "link", "create": True})
        assert r.status_code == 409 and r.json()["existing_user_id"] == ids["alice"] and r.json()["error"]
        r = await c.post(f"/api/admin/orders/{oid}", json={"action": "link", "user_id": ids["alice"]})
        assert r.status_code == 200 and r.json()["order"]["user_id"] == ids["alice"]


async def test_withdrawn_then_granted_is_409(env):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        oid = (await c.post("/api/me/orders", headers=bearer(keys["alice"]), json=ORDER | {"email": "a@b.cd"})).json()["order"]["id"]
    async with admin_client(gw) as c:
        p = (await c.post("/api/admin/tickets/preview", json=ORDER | {"user": "alice"})).json()   # the dialog opens
        orders.withdraw(conn, ids["alice"], oid)                                                   # the buyer withdraws
        r = await c.post("/api/admin/tickets", json=ORDER | {"user": "alice", "usd": p["usd"], "rate": p["rate"], "order_id": oid})
        assert r.status_code == 409 and r.json()["error"] == "This order was withdrawn or changed; reload."
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
    assert orders.get(conn, oid)["status"] == "withdrawn"


# ---------- tickets off ----------

async def test_every_order_endpoint_is_404_while_tickets_are_off(env, ts):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        await c.post("/api/orders", json=VISITOR)
    oid = only_order(conn)["id"]
    cfg.tickets.enabled = False
    async with client(gw) as c:
        assert (await c.post("/api/orders", json=VISITOR)).status_code == 404
        h = bearer(keys["alice"])
        assert (await c.get("/api/me/orders", headers=h)).status_code == 404
        assert (await c.post("/api/me/orders", headers=h, json=ORDER)).status_code == 404
        assert (await c.post(f"/api/me/orders/{oid}/withdraw", headers=h)).status_code == 404
        assert (await c.post(f"/api/me/orders/{oid}/dismiss", headers=h)).status_code == 404
    async with admin_client(gw) as c:
        assert (await c.get("/api/admin/orders")).status_code == 404
        assert (await c.post(f"/api/admin/orders/{oid}", json={"action": "contacted"})).status_code == 404
        assert "orders_new" not in (await c.get("/api/session")).json()["tickets"]
    assert ts.calls == [("sec", "tok", "127.0.0.1")]   # the second POST never reached Turnstile


async def test_session_orders_new_is_admin_only(env):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        assert (await c.get("/api/session", headers=bearer(keys["alice"]))).json()["tickets"] == {"enabled": True}


# ---------- maintenance ----------

async def test_maintenance_clears_old_ips_even_when_the_others_fail(monkeypatch, caplog):
    from claude_proxy import db, estimates
    from claude_proxy.config import Config
    calls = []

    def boom(*a):
        calls.append("boom")
        raise RuntimeError("disk full")

    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(db, "cleanup", boom)
    monkeypatch.setattr(estimates, "refresh_if_due", boom)
    monkeypatch.setattr(orders, "clear_old_ips", lambda conn: calls.append("ips") or 0)
    monkeypatch.setattr(cli.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await cli._maintenance(None, Config())
    assert calls == ["boom", "boom", "ips"]
