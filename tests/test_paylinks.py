"""Admin payment links (spec 2026-11-04): creating, viewing, paying and expiring a link."""
import time

import pytest

from claude_proxy import db as dbm, orders, payments, tickets
from tests.test_payments import env, later  # noqa: F401  (the fixture and helper)
from tests.tickets_helpers import user


def make(conn, cfg, ids, **kw):
    args = {"email": "Bob@Example.com", "tier": "lite", "length": "week", "pay_by": int(time.time()) + 3 * 86400} | kw
    return payments.create_link(conn, cfg, ids["admin"], **args)


def test_create_link_stores_an_unclaimed_order(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids, note="for Bob")
    assert o["user_id"] is None and o["name"] == o["email"] == "Bob@Example.com"
    assert (o["currency"], o["quoted_amount"], o["status"], o["lang"], o["start_now"]) == ("IRT", 1250000, "new", "fa", 0)
    assert (o["admin_mail"], o["buyer_mail"], o["admin_note"], o["ip"]) == ("off", "off", "for Bob", None)
    assert len(o["pay_code"]) == 22 and o["pay_by"] > time.time()
    row = conn.execute("SELECT action, actor_user_id, detail_json FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["action"], row["actor_user_id"]) == ("order_paylink", ids["admin"]) and '"edited": false' in row["detail_json"]


def test_codes_are_unique_and_links_are_not_visitor_orders(env):
    conn, cfg, ids, zp = env
    codes = {make(conn, cfg, ids)["pay_code"] for _ in range(3)}
    assert len(codes) == 3
    assert orders._count(conn, f"{orders.VISITOR}", ()) == 0


def test_edited_amount_records_usd_without_vat(env):
    conn, cfg, ids, zp = env
    cfg.zarinpal.vat_percent = 10
    o = make(conn, cfg, ids, amount=1100000)
    assert o["quoted_amount"] == 1100000
    assert o["quoted_usd"] == pytest.approx(1100000 / (156250 * 1.1))


@pytest.mark.parametrize("kw, msg", [
    ({"email": "not-an-email"}, "email"),
    ({"tier": "gold"}, "Unknown tier"),
    ({"length": "year"}, "Unknown length"),
    ({"pay_by": 0}, "next 30 days"),
    ({"pay_by": int(time.time()) + 31 * 86400}, "next 30 days"),
    ({"pay_by": "soon"}, "date and time"),
    ({"amount": 999}, "between"),
    ({"amount": 12500001}, "between"),
    ({"amount": "12.5"}, "whole number"),
])
def test_create_link_refuses_bad_input(env, kw, msg):
    conn, cfg, ids, zp = env
    with pytest.raises(orders.OrderError) as e:
        make(conn, cfg, ids, **kw)
    assert e.value.status == 400 and msg in str(e.value)


def test_create_link_needs_payments_on_not_manual_and_a_fresh_rate(env, monkeypatch):
    conn, cfg, ids, zp = env
    payments.set_pay_mode(conn, ids["admin"], "manual")
    with pytest.raises(payments.PaymentError) as e:
        make(conn, cfg, ids)
    assert e.value.status == 409
    payments.set_pay_mode(conn, ids["admin"], "zarinpal")
    conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))
    with pytest.raises(payments.PaymentError) as e:
        make(conn, cfg, ids)
    assert e.value.status == 409 and str(e.value) == payments.PAUSED
    monkeypatch.delenv("ZARINPAL_MERCHANT_ID")
    with pytest.raises(payments.PaymentError) as e:
        make(conn, cfg, ids)
    assert e.value.status == 404


def test_create_link_refuses_a_sold_out_pass(env, monkeypatch):
    conn, cfg, ids, zp = env
    monkeypatch.setattr(tickets, "sold_out", lambda *a, **k: True)
    with pytest.raises(orders.OrderError) as e:
        make(conn, cfg, ids)
    assert e.value.status == 409 and "sold out" in str(e.value)


@pytest.mark.parametrize("email, masked", [("bob@example.com", "bo…@example.com"), ("ab@x.io", "a…@x.io"),
                                           ("a@x.io", "a…@x.io")])
def test_mask_email(email, masked):
    assert orders.mask_email(email) == masked
