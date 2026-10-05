"""Payments design, section 4: start, callback, expiry, with ZarinPal faked."""
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


async def test_declined_meanwhile_is_unfulfilled(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    p0 = payments.get(conn, r["payment_id"])
    orders.set_status(conn, ids["admin"], p0["order_id"], "declined", note="no")
    p = await payments.callback(conn, cfg, p0["authority"], "OK")
    assert p["status"] == "paid_unfulfilled"


async def test_rate_change_during_payment_keeps_the_paid_amount(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    tickets.set_rate(conn, cfg, "IRT", 200000, ids["admin"])
    p = await payments.callback(conn, cfg, payments.get(conn, r["payment_id"])["authority"], "OK")
    o = orders.get(conn, p["order_id"])
    assert conn.execute("SELECT amount FROM tickets WHERE id=?", (o["ticket_id"],)).fetchone()[0] == 1250000


async def test_unknown_authority(env):
    conn, cfg, ids, zp = env
    with pytest.raises(payments.PaymentError) as e:
        await payments.callback(conn, cfg, "nope", "OK")
    assert e.value.status == 404


async def test_expire(env):
    conn, cfg, ids, zp = env
    r = await start(conn, cfg, ids)
    assert payments.expire(conn, now=time.time() + 30) == 0
    assert payments.expire(conn, now=time.time() + payments.EXPIRE_S + 1) == 1
    p = payments.get(conn, r["payment_id"])
    assert p["status"] == "expired" and orders.get(conn, p["order_id"])["status"] == "withdrawn"


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
