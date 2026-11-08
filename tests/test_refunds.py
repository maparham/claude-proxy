"""Cancel an unused pass and get the money back within 15 minutes (refunds.py, 2026-11-08), ZarinPal faked."""
import time

import pytest

from claude_proxy import orders, payments, referrals, refunds, tickets, zarinpal
from claude_proxy.config import EmailConfig
from tests.test_payments import env, paid_flow   # noqa: F401  (env is a fixture)
from claude_proxy.db import create_user
from tests.tickets_helpers import user


@pytest.fixture
def rv(env, monkeypatch):   # noqa: F811
    conn, cfg, ids, zp = env
    ids["bob"] = create_user(conn, "bob")[0]
    calls, box = [], {"answer": None}

    async def reverse(merchant_id, authority, sandbox):
        calls.append(authority)
        if isinstance(box["answer"], Exception):
            raise box["answer"]
    monkeypatch.setattr(zarinpal, "reverse", reverse)
    return conn, cfg, ids, calls, box


async def bought(conn, cfg, ids):
    p0, p = await paid_flow(conn, cfg, ids)
    return p, orders.get(conn, p["order_id"])["ticket_id"]


def use(conn, user_id, at):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status) "
                 "VALUES(?,?,?,'POST','/v1/messages','anthropic','claude-opus-5-5',200)", (user_id, at, at))


async def test_offer_then_refund_cancels_and_marks(rv):
    conn, cfg, ids, calls, box = rv
    p, tid = await bought(conn, cfg, ids)
    assert p["paid_at"] is not None
    [o] = refunds.offers(conn, cfg, ids["alice"])
    assert (o["payment_id"], o["amount"], o["until"]) == (p["id"], 1250000, p["paid_at"] + 900)
    r = await refunds.refund(conn, cfg, user(conn, ids["alice"]), p["id"])
    assert calls == [p["authority"]]
    t = tickets.get(conn, tid)
    assert t["cancelled_at"] is not None and t["refunded_at"] is not None and r["ticket"]["id"] == tid
    p2 = payments.get(conn, p["id"])
    assert p2["reversed_at"] is not None and p2["status"] == "paid"
    assert refunds.offers(conn, cfg, ids["alice"]) == []
    with pytest.raises(payments.PaymentError) as e:
        await refunds.refund(conn, cfg, user(conn, ids["alice"]), p["id"])
    assert e.value.status == 409 and len(calls) == 1


async def test_no_offer_after_15_minutes_or_once_used_or_for_others(rv):
    conn, cfg, ids, calls, box = rv
    p, tid = await bought(conn, cfg, ids)
    assert refunds.offers(conn, cfg, ids["bob"]) == []
    with pytest.raises(payments.PaymentError):
        await refunds.refund(conn, cfg, user(conn, ids["bob"]), p["id"])
    assert refunds.offers(conn, cfg, ids["alice"], now=p["paid_at"] + 901) == []
    with pytest.raises(payments.PaymentError):
        await refunds.refund(conn, cfg, user(conn, ids["alice"]), p["id"], now=p["paid_at"] + 901)
    t = tickets.get(conn, tid)
    use(conn, ids["alice"], max(t["starts_at"], int(time.time())))
    assert refunds.offers(conn, cfg, ids["alice"]) == []
    assert calls == []


async def test_refused_changes_nothing_and_can_be_retried(rv):
    conn, cfg, ids, calls, box = rv
    p, tid = await bought(conn, cfg, ids)
    box["answer"] = zarinpal.ZarinpalRefused(-62, "ip")
    with pytest.raises(payments.PaymentError) as e:
        await refunds.refund(conn, cfg, user(conn, ids["alice"]), p["id"])
    assert e.value.status == 502 and str(e.value) == refunds.FAILED
    assert tickets.get(conn, tid)["cancelled_at"] is None
    assert payments.get(conn, p["id"])["reverse_started_at"] is None
    box["answer"] = None
    await refunds.refund(conn, cfg, user(conn, ids["alice"]), p["id"])
    assert tickets.get(conn, tid)["cancelled_at"] is not None


async def test_unreachable_keeps_the_claim_and_offers_no_more(rv):
    conn, cfg, ids, calls, box = rv
    p, tid = await bought(conn, cfg, ids)
    box["answer"] = zarinpal.ZarinpalUnavailable("timeout")
    with pytest.raises(payments.PaymentError) as e:
        await refunds.refund(conn, cfg, user(conn, ids["alice"]), p["id"])
    assert str(e.value) == refunds.UNKNOWN
    assert tickets.get(conn, tid)["cancelled_at"] is None
    assert payments.get(conn, p["id"])["reverse_started_at"] is not None
    assert refunds.offers(conn, cfg, ids["alice"]) == []


async def test_upgraded_pass_is_not_offered(rv):
    conn, cfg, ids, calls, box = rv
    p, tid = await bought(conn, cfg, ids)
    t = tickets.get(conn, tid)
    conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, starts_at, ends_at, granted_at, tier) VALUES(?,?,?,?,?,?)",
                 (tid, 1, t["starts_at"], t["ends_at"], t["starts_at"], "pro"))
    assert refunds.offers(conn, cfg, ids["alice"]) == []


async def test_refunded_friend_keeps_first_pass_and_referrer_loses_reward(rv):
    conn, cfg, ids, calls, box = rv
    referrals.set_settings(conn, ids["admin"], {"enabled": True})
    code = referrals.code_for(conn, ids["bob"])
    assert referrals.attach(conn, ids["alice"], code)
    p, tid = await bought(conn, cfg, ids)
    r = conn.execute("SELECT * FROM referral_rewards WHERE order_id=?", (p["order_id"],)).fetchone()
    assert r["status"] == "earned"
    assert not referrals.first_pass(conn, ids["alice"])
    await refunds.refund(conn, cfg, user(conn, ids["alice"]), p["id"])
    assert conn.execute("SELECT status FROM referral_rewards WHERE id=?", (r["id"],)).fetchone()[0] == "refused"
    assert conn.execute("SELECT state FROM referral_units WHERE reward_id=?", (r["id"],)).fetchone()[0] == "cancelled"
    assert referrals.first_pass(conn, ids["alice"])


async def test_mails(rv):
    conn, cfg, ids, calls, box = rv
    cfg.email = EmailConfig(smtp_host="smtp.example.com", from_="gw@example.com", admin_to="admin@example.com")
    p, tid = await bought(conn, cfg, ids)
    await refunds.refund(conn, cfg, user(conn, ids["alice"]), p["id"])
    p = payments.get(conn, p["id"])
    o = orders.get(conn, p["order_id"])
    [(a, sa, ba), (b, sb, bb)] = refunds.mails(conn, cfg, p, o)
    assert a == "admin@example.com" and sa.startswith("Refunded:") and "1,250,000 Toman" in ba
    assert b == o["email"] and "went back to the card" in bb and str(p["ref_id"]) in bb
    [(a, s, body)] = refunds.mails(conn, cfg, p, o, unknown=True)
    assert s.startswith("Refund NOT confirmed") and "ZarinPal panel" in body
