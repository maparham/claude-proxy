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
