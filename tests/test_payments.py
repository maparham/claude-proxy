"""Payments design, section 4: start, callback, reconcile, with ZarinPal faked."""
import asyncio
import sqlite3
import time

import pytest

from claude_proxy import orders, payments, tickets, zarinpal
from claude_proxy.config import Currency, ZarinpalConfig
from tests.tickets_helpers import seeded, user

MID = "00000000-0000-0000-0000-000000000000"


class FakeZP:
    """Stands in for zarinpal.request/verify. Set `.request_answer` / `.verify_answer` to a value or an exception."""
    def __init__(self):
        self.requests, self.verifies = [], []
        self.request_answer, self.verify_answer = "A1", {"code": 100, "ref_id": 201, "card_pan": "502229******5995"}
        self.n = 0

    async def request(self, merchant_id, amount, description, callback_url, email, order_id, sandbox):
        self.requests.append({"amount": amount, "callback_url": callback_url, "email": email, "order_id": order_id,
                              "description": description, "merchant_id": merchant_id})
        if isinstance(self.request_answer, Exception):
            raise self.request_answer
        self.n += 1
        return f"{self.request_answer}-{self.n}"

    async def verify(self, merchant_id, amount, authority, sandbox):
        self.verifies.append((amount, authority))
        if isinstance(self.verify_answer, Exception):
            raise self.verify_answer
        return self.verify_answer


@pytest.fixture
def env(db, monkeypatch):
    conn, cfg, ids = seeded(db)
    now = int(time.time())
    conn.execute("UPDATE fx_rates SET set_at=?", (now - 3600,))
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 156250, ids["admin"], now=now - 3600)   # $8 -> 1,250,000 T
    cfg.zarinpal = ZarinpalConfig()
    cfg.listener.home_url = "https://rahkar.pro"
    cfg.listener.dashboard_url = "https://claude-dash.rahkar.pro"
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    zp = FakeZP()
    monkeypatch.setattr(zarinpal, "request", zp.request)
    monkeypatch.setattr(zarinpal, "verify", zp.verify)
    return conn, cfg, ids, zp


async def start(conn, cfg, ids, **kw):
    return await payments.start(conn, cfg, user(conn, ids["alice"]), kw.get("tier", "lite"), kw.get("length", "week"),
                                kw.get("email", "alice@example.com"))


def test_on_needs_everything(env, monkeypatch):
    conn, cfg, ids, zp = env
    assert payments.on(cfg)
    monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
    assert not payments.on(cfg)
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    cfg.listener.home_url = ""
    assert not payments.on(cfg)
    cfg.listener.home_url = "https://rahkar.pro"
    del cfg.tickets.currencies["IRT"]
    assert not payments.on(cfg)


async def test_start_sends_integer_amount(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    req = zp.requests[0]
    assert req["amount"] == 1250000 and type(req["amount"]) is int
    assert req["callback_url"] == "https://rahkar.pro/pay/callback" and req["merchant_id"] == MID
    assert req["description"] == f"Claude Gateway: Lite, week, order #{req['order_id']}"
    assert r["url"] == "https://payment.zarinpal.com/pg/StartPay/A1-1"
    p = payments.get(conn, r["payment_id"])
    assert (p["status"], p["amount"], p["authority"], p["user_id"]) == ("started", 1250000, "A1-1", ids["alice"])
    o = orders.get(conn, p["order_id"])
    assert (o["status"], o["currency"], o["admin_mail"]) == ("new", "IRT", "off")


async def test_stale_or_missing_rate_starts_nothing(env):
    conn, cfg, ids, zp = env
    conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))
    with pytest.raises(payments.PaymentError) as e:
        await start(conn, cfg, ids)
    assert e.value.status == 409 and "paused" in str(e.value)
    conn.execute("DELETE FROM fx_rates WHERE currency='IRT'")
    with pytest.raises(payments.PaymentError):
        await start(conn, cfg, ids)
    assert zp.requests == [] and conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


async def test_payments_off_is_404(env, monkeypatch):
    conn, cfg, ids, zp = env
    monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
    with pytest.raises(payments.PaymentError) as e:
        await start(conn, cfg, ids)
    assert e.value.status == 404


@pytest.mark.parametrize("err", [zarinpal.ZarinpalUnavailable("down"), zarinpal.ZarinpalRefused(-9, "invalid")])
async def test_zarinpal_failure_withdraws_the_order(env, err):
    conn, cfg, ids, zp = env
    zp.request_answer = err
    with pytest.raises(payments.PaymentError) as e:
        await start(conn, cfg, ids)
    assert e.value.status == 502
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "withdrawn"
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0


async def test_order_rules_still_apply(env):
    conn, cfg, ids, zp = env
    await start(conn, cfg, ids)
    with pytest.raises(orders.OrderError) as e:   # one open order
        await start(conn, cfg, ids)
    assert e.value.status == 409


async def paid_flow(conn, cfg, ids):
    r = await start(conn, cfg, ids)
    p = payments.get(conn, r["payment_id"])
    return p, await payments.callback(conn, cfg, p["authority"], "OK")


async def test_paid_grants_the_ticket(env):
    conn, cfg, ids, zp = env
    p0, p = await paid_flow(conn, cfg, ids)
    assert (p["status"], p["ref_id"], p["card_pan"]) == ("paid", "201", "502229******5995")
    assert zp.verifies == [(1250000, p0["authority"])]
    o = orders.get(conn, p["order_id"])
    t = conn.execute("SELECT * FROM tickets WHERE id=?", (o["ticket_id"],)).fetchone()
    assert o["status"] == "done" and (t["user_id"], t["currency"], t["amount"]) == (ids["alice"], "IRT", 1250000)


async def test_repeated_callback_grants_once(env):
    conn, cfg, ids, zp = env
    p0, p = await paid_flow(conn, cfg, ids)
    again = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert again["status"] == "paid" and len(zp.verifies) == 1
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


async def test_verify_101_counts_as_paid(env):
    conn, cfg, ids, zp = env
    zp.verify_answer = {"code": 101, "ref_id": 201, "card_pan": "x"}
    _, p = await paid_flow(conn, cfg, ids)
    assert p["status"] == "paid"


async def test_cancelled(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p = await payments.callback(conn, cfg, payments.get(conn, r["payment_id"])["authority"], "NOK")
    assert p["status"] == "cancelled" and zp.verifies == []
    assert orders.get(conn, p["order_id"])["status"] == "withdrawn"


async def test_verify_refused_fails_and_withdraws(env):
    conn, cfg, ids, zp = env
    zp.verify_answer = zarinpal.ZarinpalRefused(-51, "Session is not valid, session is not active paid try.")
    _, p = await paid_flow(conn, cfg, ids)
    assert p["status"] == "failed" and "-51" in p["error"]
    assert orders.get(conn, p["order_id"])["status"] == "withdrawn"


async def test_refused_after_concurrent_paid_does_not_overwrite(env, monkeypatch):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])

    async def refused_after_concurrent_paid(merchant_id, amount, authority, sandbox):
        payments._set(conn, p0["id"], "paid", int(time.time()), ref_id="999", card_pan="y")   # another callback finished first
        raise zarinpal.ZarinpalRefused(-51, "late")
    monkeypatch.setattr(zarinpal, "verify", refused_after_concurrent_paid)

    p = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert p["status"] == "paid" and p["ref_id"] == "999"


async def test_verify_unreachable_stays_started_and_retries(env):
    conn, cfg, ids, zp = env
    zp.verify_answer = zarinpal.ZarinpalUnavailable("down")
    p0, p = await paid_flow(conn, cfg, ids)
    assert p["status"] == "started"
    zp.verify_answer = {"code": 101, "ref_id": 201, "card_pan": "x"}
    assert (await payments.callback(conn, cfg, p0["authority"], "OK"))["status"] == "paid"


async def test_capacity_gone_is_unfulfilled_and_order_stays_open(env, monkeypatch):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)   # order created while capacity is still fine
    p0 = payments.get(conn, r["payment_id"])

    def full(*a, **k):
        raise tickets.CapacityError("sold out")
    monkeypatch.setattr(tickets, "check_capacity", full)   # capacity is gone by the time the callback grants
    p = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert p["status"] == "paid_unfulfilled" and p["ref_id"] == "201" and "sold out" in p["error"]
    assert orders.get(conn, p["order_id"])["status"] == "new"
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


async def test_grant_crash_still_records_the_verified_payment_and_reraises(env, monkeypatch):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(tickets, "grant", boom)

    with pytest.raises(sqlite3.OperationalError):
        await payments.callback(conn, cfg, p0["authority"], "OK")
    p = payments.get(conn, r["payment_id"])
    assert p["status"] == "paid_unfulfilled" and p["ref_id"] == "201" and "database is locked" in p["error"]


async def test_verify_succeeds_after_expiry_meanwhile_is_unfulfilled(env, monkeypatch):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])

    async def verify_but_expired_meanwhile(merchant_id, amount, authority, sandbox):
        payments._close(conn, payments.get(conn, p0["id"]), "expired", int(time.time()))   # reconcile closed it while this awaited
        return {"code": 100, "ref_id": 201, "card_pan": "x"}
    monkeypatch.setattr(zarinpal, "verify", verify_but_expired_meanwhile)

    p = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert p["status"] == "paid_unfulfilled" and p["ref_id"] == "201" and p["card_pan"] == "x"
    assert "expired" in p["error"]
    assert orders.get(conn, p["order_id"])["status"] == "withdrawn"


async def test_declined_during_verify_is_unfulfilled(env, monkeypatch):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])
    real_verify = zp.verify

    async def declined_meanwhile(*a, **kw):
        orders.set_status(conn, ids["admin"], p0["order_id"], "declined", note="no")
        return await real_verify(*a, **kw)
    monkeypatch.setattr(zarinpal, "verify", declined_meanwhile)
    p = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert p["status"] == "paid_unfulfilled"


@pytest.mark.parametrize("to", ["withdrawn", "declined"])
async def test_order_closed_before_callback_cancels_without_verifying(env, to):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])
    orders.set_status(conn, ids["alice"] if to == "withdrawn" else ids["admin"], p0["order_id"], to, note="no")
    p = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert p["status"] == "cancelled" and zp.verifies == [] and not p.get("changed")
    assert orders.get(conn, p["order_id"])["status"] == to
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='payment_cancelled'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


async def test_order_granted_by_hand_cancels_without_verifying(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])
    o = orders.get(conn, p0["order_id"])
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "IRT", order_id=o["id"], paid=True)
    p = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert p["status"] == "cancelled" and zp.verifies == []
    assert orders.get(conn, p["order_id"])["status"] == "done"


async def test_fulfil_error_after_commit_keeps_paid(env, monkeypatch):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])
    real_grant = tickets.grant

    def grant_then_boom(*a, **kw):
        real_grant(*a, **kw)
        raise sqlite3.OperationalError("after commit")
    monkeypatch.setattr(tickets, "grant", grant_then_boom)
    p = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert p["status"] == "paid" and p["changed"] is True and p["ref_id"] == "201"
    assert payments.get(conn, p0["id"])["status"] == "paid"
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


async def test_rate_change_during_payment_keeps_the_paid_amount(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    tickets.set_rate(conn, cfg, "IRT", 200000, ids["admin"])
    p = await payments.callback(conn, cfg, payments.get(conn, r["payment_id"])["authority"], "OK")
    o = orders.get(conn, p["order_id"])
    assert conn.execute("SELECT amount FROM tickets WHERE id=?", (o["ticket_id"],)).fetchone()[0] == 1250000


async def test_concurrent_callbacks_mark_changed_exactly_once(env, monkeypatch):
    """Two callbacks racing on the same authority (review fix round 1): only the one that actually moves the
    payment to paid/paid_unfulfilled is marked changed=True, so the web layer sends the mail only once."""
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])
    real_verify = zp.verify

    async def yielding_verify(*a, **kw):
        await asyncio.sleep(0)   # let the other callback run up to its own verify before either resumes
        return await real_verify(*a, **kw)
    monkeypatch.setattr(zarinpal, "verify", yielding_verify)

    r1, r2 = await asyncio.gather(payments.callback(conn, cfg, p0["authority"], "OK"),
                                  payments.callback(conn, cfg, p0["authority"], "OK"))
    assert sum(1 for res in (r1, r2) if res.get("changed")) == 1
    assert {r1["status"], r2["status"]} == {"paid"}
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


async def test_unknown_authority(env):
    conn, cfg, ids, zp = env
    with pytest.raises(payments.PaymentError) as e:
        await payments.callback(conn, cfg, "nope", "OK")
    assert e.value.status == 404


def later():
    return time.time() + payments.EXPIRE_S + 1


async def test_reconcile_leaves_recent_payments(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    out = await payments.reconcile(conn, cfg, now=time.time() + 30)
    assert out["changed"] == [] and zp.verifies == []
    assert payments.get(conn, r["payment_id"])["status"] == "started"


async def test_reconcile_grants_a_payment_whose_verify_was_unreachable(env, monkeypatch):
    conn, cfg, ids, zp = env
    from claude_proxy import mail
    from claude_proxy.config import EmailConfig
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    sent = []
    monkeypatch.setattr(mail, "send", lambda email, to, subject, body: sent.append(to))
    zp.verify_answer = zarinpal.ZarinpalUnavailable("down")
    p0, p = await paid_flow(conn, cfg, ids)
    assert p["status"] == "started"
    zp.verify_answer = {"code": 101, "ref_id": 201, "card_pan": "x"}
    out = await payments.reconcile(conn, cfg, now=later())
    assert out["changed"] == [p0["id"]] and out["paid"] == 1
    assert zp.verifies[-1] == (1250000, p0["authority"])
    p = payments.get(conn, p0["id"])
    o = orders.get(conn, p["order_id"])
    assert p["status"] == "paid" and o["status"] == "done" and o["ticket_id"]
    for pid in out["changed"]:
        await payments.send_mails(conn, cfg, pid)
    again = await payments.reconcile(conn, cfg, now=later() + 600)   # nothing left: no second mail, no second verify
    assert again["changed"] == [] and len(zp.verifies) == 2
    assert sorted(sent) == ["admin@example.com", "alice@example.com"]


async def test_reconcile_refused_expires_and_withdraws(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    zp.verify_answer = zarinpal.ZarinpalRefused(-51, "not paid")
    out = await payments.reconcile(conn, cfg, now=later())
    p = payments.get(conn, r["payment_id"])
    assert out["expired"] == 1 and out["changed"] == []
    assert p["status"] == "expired" and "-51" in p["error"]
    assert orders.get(conn, p["order_id"])["status"] == "withdrawn"


async def test_reconcile_unavailable_stays_started(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    zp.verify_answer = zarinpal.ZarinpalUnavailable("down")
    out = await payments.reconcile(conn, cfg, now=later())
    assert out["pending"] == 1 and out["changed"] == []
    p = payments.get(conn, r["payment_id"])
    assert p["status"] == "started" and orders.get(conn, p["order_id"])["status"] == "new"


async def test_reconcile_with_payments_off_expires_without_verifying(env, monkeypatch):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
    out = await payments.reconcile(conn, cfg, now=later())
    p = payments.get(conn, r["payment_id"])
    assert out["expired"] == 1 and zp.verifies == [] and p["status"] == "expired"
    assert orders.get(conn, p["order_id"])["status"] == "withdrawn"


async def test_reconcile_order_closed_cancels_without_verifying(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])
    orders.set_status(conn, ids["alice"], p0["order_id"], "withdrawn")
    out = await payments.reconcile(conn, cfg, now=later())
    assert out["cancelled"] == 1 and zp.verifies == []
    assert payments.get(conn, p0["id"])["status"] == "cancelled"


async def test_reconcile_skips_a_payment_finished_while_it_awaited(env, monkeypatch):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])

    async def callback_won(merchant_id, amount, authority, sandbox):
        payments._set(conn, p0["id"], "paid", int(time.time()), ref_id="999", card_pan="y")
        return {"code": 101, "ref_id": 999, "card_pan": "y"}
    monkeypatch.setattr(zarinpal, "verify", callback_won)
    out = await payments.reconcile(conn, cfg, now=later())
    assert out["changed"] == [] and conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


async def test_reconcile_one_bad_row_does_not_stop_the_rest(env, monkeypatch):
    conn, cfg, ids, zp = env
    r1 = await start(conn, cfg, ids)
    conn.execute("UPDATE orders SET status='withdrawn'")
    r2 = await start(conn, cfg, ids)
    calls = []

    async def first_explodes(merchant_id, amount, authority, sandbox):
        calls.append(authority)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"code": 100, "ref_id": 202, "card_pan": "x"}
    monkeypatch.setattr(zarinpal, "verify", first_explodes)
    conn.execute("UPDATE orders SET status='new' WHERE id=(SELECT order_id FROM payments WHERE id=?)", (r1["payment_id"],))
    out = await payments.reconcile(conn, cfg, now=later())
    assert out["errors"] == 1 and out["paid"] == 1 and len(calls) == 2


async def test_mails(env):
    conn, cfg, ids, zp = env
    from claude_proxy.config import EmailConfig
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    _, p = await paid_flow(conn, cfg, ids)
    o = orders.get(conn, p["order_id"])
    t = dict(conn.execute("SELECT * FROM tickets WHERE id=?", (o["ticket_id"],)).fetchone())
    m = payments.mails(cfg, p, o, t, None)
    assert [x[0] for x in m] == ["admin@example.com", "alice@example.com"]
    assert "1,250,000 Toman" in m[0][2] and "201" in m[0][2] and "201" in m[1][2]
    m = payments.mails(cfg, p | {"status": "paid_unfulfilled"}, o, None, "sold out")
    assert [x[0] for x in m] == ["admin@example.com"] and "NOT granted: sold out" in m[0][2]


def test_startup_warning_names_what_is_missing(env, monkeypatch, caplog):
    conn, cfg, ids, zp = env
    assert payments.missing(cfg) == []
    with caplog.at_level("WARNING"):
        payments.warn_if_off(cfg)
    assert caplog.records == []
    cfg.listener.home_url = ""
    del cfg.tickets.currencies["IRT"]
    assert payments.missing(cfg) == ["IRT in [tickets.currencies]", "[listener] home_url"]
    monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
    cfg.tickets.enabled = False
    with caplog.at_level("WARNING"):
        payments.warn_if_off(cfg)
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "ZARINPAL_MERCHANT_ID" in msg and "IRT" in msg and "home_url" in msg and "[tickets]" in msg
    caplog.clear()
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    with caplog.at_level("WARNING"):
        payments.warn_if_off(cfg)
    assert MID not in caplog.text
    cfg.zarinpal = None
    caplog.clear()
    with caplog.at_level("WARNING"):
        payments.warn_if_off(cfg)
    assert caplog.records == []   # no [zarinpal] section: payments are simply not configured


async def test_cli_reconcile_mails_each_changed_payment_once(env, monkeypatch):
    conn, cfg, ids, zp = env
    from claude_proxy import cli, mail
    from claude_proxy.config import EmailConfig
    sent = []
    monkeypatch.setattr(mail, "send", lambda email, to, subject, body: sent.append(to))
    r = await start(conn, cfg, ids)
    conn.execute("UPDATE payments SET created_at=created_at-?", (payments.EXPIRE_S + 1,))
    await cli._reconcile_once(conn, cfg)   # no [email]: granted, no mail
    assert payments.get(conn, r["payment_id"])["status"] == "paid" and sent == []
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    conn.execute("UPDATE orders SET status='withdrawn'")
    r = await start(conn, cfg, ids)
    conn.execute("UPDATE payments SET created_at=created_at-? WHERE id=?", (payments.EXPIRE_S + 1, r["payment_id"]))
    await cli._reconcile_once(conn, cfg)
    await cli._reconcile_once(conn, cfg)
    assert sorted(sent) == ["admin@example.com", "alice@example.com"]


async def test_cli_reconcile_survives_a_failure(monkeypatch, caplog):
    from claude_proxy import cli
    from claude_proxy.config import Config

    async def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(payments, "reconcile", boom)
    await cli._reconcile_once(None, Config())
    assert "reconciling payments failed" in caplog.text
