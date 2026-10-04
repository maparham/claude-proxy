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


# ---------- the dashboard (static checks and node harnesses) ----------

def _node_check(path):
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("no node here")
    subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, timeout=30)


STATIC = __import__("pathlib").Path(__file__).resolve().parent.parent / "src" / "claude_proxy" / "static"


async def test_dashboard_serves_the_orders_tab_and_the_buyer_view(env):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        js = (await c.get("/static/app.js")).text
    assert "renderOrders" in js and "/api/admin/orders" in js and '"/api/me/orders"' in js and "order_id: pre.order_id" in js
    assert 'orders: { label: "Orders", render: renderOrders, admin: true, feature: "tickets" }' in js
    _node_check(STATIC / "app.js")


# Pure helpers out of app.js, run with stand-ins for the page-wide ones they use.
APP_STUBS = r"""
var esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
var money = (a, c) => `${c} ${a}`;
var fmtShare = (v) => `${v}%`;
var fmtDate = (t) => `D${t}`;
var fmtAgo = (t) => `A${t}`;
var stateBadge = (s) => `<span class="badge state-${s}">${esc(s)}</span>`;
var S = { tkSkew: 0, pick: null, user: { email: null } };
var Date = { now: () => 0 };
"""
BAD = '<img src=x onerror="alert(1)">'


def _order_row(**kw):
    base = {"id": 7, "created_at": 100, "updated_at": 100, "user_id": None, "user_name": None, "name": "Vera", "email": "vera@example.com",
            "tier": "lite", "label": "Lite", "length": "week", "currency": "EUR", "quoted_usd": 8, "quoted_rate": 0.92, "quoted_amount": 7.5,
            "message": "Hello", "status": "new", "admin_note": "", "admin_mail": "sent", "buyer_mail": "sent", "ip": "1.2.3.4",
            "ticket_id": None, "suggested_user": None}
    return base | kw


def test_order_rows_escape_every_buyer_field(tmp_path):
    import json
    from tests.test_tickets_web import _app_fn
    o = _order_row(name=BAD, email=BAD, message=BAD, admin_note=BAD, label=BAD, user_name=BAD, user_id=3)
    html = _app_fn(tmp_path, ["orderRow", "mailState", "orderActions"], APP_STUBS + f"var out = orderRow({json.dumps(o)});")
    assert "<img" not in html and html.count("&lt;img") >= 5


def test_order_actions_follow_the_status_table(tmp_path):
    import json
    from tests.test_tickets_web import _app_fn
    rows = [_order_row(status=s) for s in ("new", "contacted", "done", "declined", "withdrawn")]
    out = _app_fn(tmp_path, ["orderActions"], APP_STUBS + f"var out = {json.dumps(rows)}.map(orderActions);")
    acts = [[a for a in ("contacted", "decline", "note", "grant") if f'data-oact="{a}"' in h] for h in out]
    assert acts == [["contacted", "decline", "note", "grant"], ["decline", "note", "grant"], ["note"], ["note"], ["note"]]


def test_order_mail_state_names_failures(tmp_path):
    import json
    from tests.test_tickets_web import _app_fn
    rows = [_order_row(admin_mail="failed", buyer_mail="sent"), _order_row(admin_mail="pending", buyer_mail="skipped"),
            _order_row(admin_mail="off", buyer_mail="off")]
    out = _app_fn(tmp_path, ["mailState"], APP_STUBS + f"var out = {json.dumps(rows)}.map(mailState);")
    assert "admin email failed" in out[0] and "buyer email sent" in out[0]
    assert "admin email pending" in out[1] and "buyer email skipped" in out[1]
    assert "email off" in out[2] and "admin" not in out[2]


def test_the_buyer_sees_their_order_and_never_the_admin_note(tmp_path):
    import json
    from tests.test_tickets_web import _app_fn
    mine = {"id": 4, "label": "<b>Lite</b>", "tier": "lite", "length": "week", "currency": "EUR", "quoted_amount": 7.5}
    orders_ = [mine | {"status": "new"}, mine | {"status": "contacted"}, mine | {"status": "done"}, mine | {"status": "declined"}, None]
    out = _app_fn(tmp_path, ["myOrderCard"], APP_STUBS + f"var out = {json.dumps(orders_)}.map(myOrderCard);")
    assert "Order received: &lt;b&gt;Lite&lt;/b&gt;, 1 week. The admin will contact you." in out[0] and 'data-my-order="withdraw"' in out[0]
    assert 'data-my-order="withdraw"' in out[1]
    assert "Your order is done." in out[2] and 'data-my-order="dismiss"' in out[2] and "withdraw" not in out[2]
    assert "Your order was declined." in out[3] and 'data-my-order="dismiss"' in out[3]
    assert out[4] == ""


def _price_list(tmp_path, order=None, pick=None, sold_out_week=False):
    import json
    from tests.test_tickets_web import _tier
    from tests.test_tickets_web import _app_fn
    tk = {"how_to_buy": "", "prices": {"currency": "EUR", "rate_set_at": None, "tiers": [_tier(week={"sold_out": sold_out_week})]}}
    script = APP_STUBS + f"S.pick = {json.dumps(pick)}; var out = priceListCard({json.dumps(tk)}, {json.dumps(order)});"
    return _app_fn(tmp_path, ["priceListCard", "pickedLine", "takePick"], script)


def test_the_price_list_offers_an_order_button_per_price(tmp_path):
    html = _price_list(tmp_path)
    assert html.count('data-order="lite:') == 3 and "disabled" not in html
    sold = _price_list(tmp_path, sold_out_week=True)
    assert 'data-order="lite:week"' not in sold and 'data-order="lite:day"' in sold


def test_order_buttons_are_disabled_while_an_order_is_open(tmp_path):
    assert _price_list(tmp_path, order={"status": "new"}).count("disabled") == 3
    assert _price_list(tmp_path, order={"status": "contacted"}).count("disabled") == 3
    assert "disabled" not in _price_list(tmp_path, order={"status": "done"})


def test_the_home_page_pick_is_preselected(tmp_path):
    html = _price_list(tmp_path, pick={"tier": "lite", "length": "month"})
    btn = next(b for b in html.split("<button")[1:] if 'data-order="lite:month"' in b)
    assert "primary" in btn
    assert all("primary" not in b for b in html.split("<button")[1:] if 'data-order="lite:month"' not in b)


def test_the_order_dialog_asks_for_an_email_only_without_one(tmp_path):
    import json
    from tests.test_tickets_web import _tier
    from tests.test_tickets_web import _app_fn
    p = {"currency": "EUR", "tiers": [_tier(label=BAD)]}
    script = APP_STUBS + f"var p = {json.dumps(p)}; var out = [orderFormHtml(p, p.tiers[0], 'week', null), orderFormHtml(p, p.tiers[0], 'week', 'a@b.cd')];"
    without, with_ = _app_fn(tmp_path, ["orderFormHtml", "orderCurrencies"], script)
    assert 'name="email"' in without and 'name="email"' not in with_
    assert "This is a request, not a payment. The admin will contact you with payment details." in with_
    assert "<img" not in without and '<option value="EUR" selected' in with_ and '<option value="USD"' in with_


# ---------- the home page's order dialog ----------

async def test_home_csp_allows_turnstile_only_when_it_is_on(env, monkeypatch):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        csp = (await c.get("/")).headers["content-security-policy"]
    assert "challenges.cloudflare.com" not in csp and "frame-src 'none'" in csp
    cfg.tickets.turnstile_site_key = "0xSITE"
    monkeypatch.setenv("TURNSTILE_SECRET", "sec")
    async with client(gw) as c:
        r = await c.get("/")
    csp = dict(d.strip().split(" ", 1) for d in r.headers["content-security-policy"].split(";"))
    assert "https://challenges.cloudflare.com" in csp["script-src"] and "https://challenges.cloudflare.com" in csp["frame-src"]
    assert "challenges.cloudflare.com" not in csp["connect-src"] and "challenges.cloudflare.com" not in csp["default-src"]
    assert 'id="order-dialog"' in r.text


async def test_home_js_orders_through_the_public_endpoint(env):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        js = (await c.get("/static/home.js")).text
    assert '"/api/orders"' in js and "turnstile/v0/api.js?render=explicit" in js and "turnstile_token" in js
    _node_check(STATIC / "home.js")


HOME_ORDER_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const data = JSON.parse(process.argv[3]);
const created = [];
const els = {};
const el = (id) => (els[id] ||= { id, innerHTML: "", textContent: "", addEventListener: () => {} });
const ctx = {
  console, Intl, Math, String, Object, Number, JSON, encodeURIComponent, Promise, Date,
  document: { documentElement: { dataset: {} }, getElementById: el, querySelectorAll: () => [],
              head: { appendChild: (s) => created.push(s.src) }, createElement: () => ({}) },
  location: { reload: () => {} }, localStorage: { getItem: () => null }, sessionStorage: { getItem: () => null, setItem: () => {} },
  setInterval: () => {},
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(data) }),
};
ctx.window = ctx;
vm.runInNewContext(src, ctx);
setTimeout(() => {
  const out = { atLoad: created.length };
  out.cards = Object.fromEntries(["day", "week", "month"].map((k) => [k, ctx.cardsHtml(data, k)]));
  out.form = ctx.orderFormHtml(data, data.tiers[0], "week");
  ctx.loadTurnstile(); ctx.loadTurnstile();
  out.scripts = created;
  console.log(JSON.stringify(out));
}, 10);
"""


def _run_home_order(tmp_path, data):
    import json
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("no node here")
    harness = tmp_path / "home_order.js"
    harness.write_text(HOME_ORDER_HARNESS)
    out = subprocess.run(["node", str(harness), str(STATIC / "home.js"), json.dumps(data)], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def test_with_turnstile_get_it_opens_the_order_dialog(tmp_path):
    from tests.test_tickets_web import _prices, _tier
    data = _prices(_tier(week={"sold_out": True})) | {"turnstile_site_key": "0xSITE"}
    r = _run_home_order(tmp_path, data)
    assert 'data-order="lite:month"' in r["cards"]["month"] and 'href="/dashboard?tier=' not in r["cards"]["month"]
    assert "Sold out" in r["cards"]["week"] and "data-order" not in r["cards"]["week"]          # sold out still can't be ordered
    assert r["atLoad"] == 0                                                                      # the script waits for the dialog
    assert r["scripts"] == ["https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"]   # and loads once


def test_without_turnstile_get_it_stays_a_link(tmp_path):
    from tests.test_tickets_web import _prices
    r = _run_home_order(tmp_path, _prices())
    assert 'href="/dashboard?tier=lite&length=week"' in r["cards"]["week"] and "data-order" not in r["cards"]["week"]


def test_the_visitor_dialog_offers_signing_in_and_escapes(tmp_path):
    from tests.test_tickets_web import _prices, _tier
    data = _prices(_tier(label=BAD)) | {"turnstile_site_key": "0xSITE", "currency": "EUR"}
    form = _run_home_order(tmp_path, data)["form"]
    assert '<a href="/dashboard?tier=lite&amp;length=week">Or sign in to order</a>' in form
    for field in ('name="name"', 'name="email"', 'name="currency"', 'name="message"', 'class="turnstile'):
        assert field in form
    assert "This is a request, not a payment. The admin will contact you with payment details." in form
    assert '<option value="EUR" selected' in form and '<option value="USD"' in form
    assert "<img" not in form
