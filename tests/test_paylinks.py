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


def bob(conn, email="bob@example.com"):
    uid, _ = dbm.create_user(conn, "bob")
    conn.execute("UPDATE users SET email=? WHERE id=?", (email, uid))
    return user(conn, uid)


def test_link_state_for_each_case(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    s = payments.link_state(conn, b, o["pay_code"])
    assert s["state"] == "open" and s["order"]["id"] == o["id"]           # case differs: Bob@ vs bob@
    assert payments.link_state(conn, b, "nope")["state"] == "invalid"
    other = user(conn, ids["alice"])                                       # alice has no email at all
    s = payments.link_state(conn, other, o["pay_code"])
    assert (s["state"], s["email"]) == ("other_email", "Bo…@Example.com") and "Bo…@Example.com" in s["message"]
    assert "order" not in s
    conn.execute("UPDATE orders SET pay_by=? WHERE id=?", (int(time.time()) - 1, o["id"]))
    assert payments.link_state(conn, b, o["pay_code"])["state"] == "invalid"


def test_link_state_other_order_and_withdrawn(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    own = orders.create(conn, cfg, tier="lite", length="week", currency="IRT", name="bob", email="bob@example.com",
                        user_id=b["id"], notify=False, terms_version="v1")
    assert payments.link_state(conn, b, o["pay_code"])["state"] == "other_order"
    orders.withdraw(conn, b["id"], own["id"])
    assert payments.link_state(conn, b, o["pay_code"])["state"] == "open"
    orders.set_status(conn, ids["admin"], o["id"], "declined")
    assert payments.link_state(conn, b, o["pay_code"])["state"] == "invalid"


async def test_pay_link_claims_then_pays_and_grants_at_the_link_price(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids, amount=1000000)
    b = bob(conn)
    r = await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    claimed = orders.get(conn, o["id"])
    assert (claimed["user_id"], claimed["name"]) == (b["id"], "bob")
    assert zp.requests[0]["amount"] == 1000000 and r["url"].startswith("https://payment.zarinpal.com/pg/StartPay/")
    assert conn.execute("SELECT 1 FROM audit_log WHERE action='order_paylink_claimed'").fetchone()
    p = payments.get(conn, r["payment_id"])
    assert (await payments.callback(conn, cfg, p["authority"], "OK"))["status"] == "paid"
    t = conn.execute("SELECT * FROM tickets WHERE user_id=?", (b["id"],)).fetchone()
    assert t["usd"] == pytest.approx(o["quoted_usd"])
    s = payments.link_state(conn, b, o["pay_code"])
    assert (s["state"], s["unfulfilled"]) == ("paid", False)
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    assert e.value.status == 409 and str(e.value) == payments.ALREADY_PAID


async def test_pay_link_refusals(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    with pytest.raises(orders.OrderError) as e:
        await payments.pay_link(conn, cfg, b, o["pay_code"], "v0")
    assert e.value.status == 400
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_link(conn, cfg, user(conn, ids["alice"]), o["pay_code"], "v1")
    assert e.value.status == 403
    conn.execute("UPDATE orders SET pay_by=? WHERE id=?", (int(time.time()) - 1, o["id"]))
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    assert e.value.status == 404 and str(e.value) == orders.LINK_INVALID
    assert zp.requests == [] and orders.get(conn, o["id"])["user_id"] is None


async def test_pay_link_skips_the_stale_rate_and_survives_zarinpal_failing(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    conn.execute("UPDATE fx_rates SET set_at=? WHERE currency='IRT'", (int(time.time()) - 40 * 3600,))
    zp.request_answer = payments.zarinpal.ZarinpalUnavailable("down")
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    assert e.value.status == 502 and orders.get(conn, o["id"])["status"] == "new"
    zp.request_answer = "A1"
    assert (await payments.pay_link(conn, cfg, b, o["pay_code"], "v1"))["url"]


async def test_two_tabs_pay_one_link_once(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    first = await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    second = await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='order_paylink_claimed'").fetchone()[0] == 1
    for r in (first, second):
        await payments.callback(conn, cfg, payments.get(conn, r["payment_id"])["authority"], "OK")
    assert conn.execute("SELECT COUNT(*) FROM tickets WHERE user_id=?", (b["id"],)).fetchone()[0] == 1


async def test_withdrawn_claimed_link_is_invalid(env):
    conn, cfg, ids, zp = env
    o = make(conn, cfg, ids)
    b = bob(conn)
    await payments.pay_link(conn, cfg, b, o["pay_code"], "v1")
    orders.withdraw(conn, b["id"], o["id"])
    assert payments.link_state(conn, b, o["pay_code"])["state"] == "invalid"
