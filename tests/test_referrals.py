"""Referrals (design 2026-11-07), task 1: settings, schema and the cost floor. Task 2: codes, the link, and
attaching a friend at sign-up."""
import pytest

from claude_proxy import referrals, tickets
from claude_proxy.config import ZarinpalConfig
from claude_proxy.db import create_user, delete_user
from tests.tickets_helpers import NOW, live, seeded, user


def audit_actions(conn):
    return [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]


@pytest.fixture
def env(tmp_path):
    from claude_proxy.db import init_db
    conn = init_db(str(tmp_path / "test.db"))
    conn, cfg, ids = seeded((None, conn))
    cfg.zarinpal = ZarinpalConfig(vat_percent=10)
    tickets.set_share(conn, cfg, "lite", 20, ids["admin"])
    tickets.set_share(conn, cfg, "standard", 40, ids["admin"])
    tickets.set_price(conn, cfg, "standard", "week", 15.0, ids["admin"])
    live(conn, NOW)
    yield conn, cfg, ids
    conn.close()


def test_defaults(env):
    conn, cfg, ids = env
    s = referrals.settings(conn)
    assert (s.enabled, s.friend_pct, s.unit_pct, s.max_held, s.max_per_month, s.unit_days, s.approval,
            s.account_cost_usd, s.fee_pct) == (False, 10, 10, 5, 5, 90, "auto", 100, 0)


def test_set_settings_validates_and_audits(env):
    conn, cfg, ids = env
    admin = ids["admin"]
    referrals.set_settings(conn, admin, {"unit_pct": 15})
    assert referrals.settings(conn).unit_pct == 15
    with pytest.raises(referrals.ReferralError):
        referrals.set_settings(conn, admin, {"approval": "maybe"})
    assert audit_actions(conn)[-1] == "referral_settings"


def test_pass_cost(env):
    conn, cfg, ids = env
    assert referrals.pass_cost(conn, cfg, "lite", 1) == pytest.approx(100 * 0.20 / 30)
    assert referrals.pass_cost(conn, cfg, "standard", 7) == pytest.approx(100 * 0.40 * 7 / 30)


def test_apply_pct_room(env):
    conn, cfg, ids = env
    assert referrals.apply_pct(conn, cfg, "standard", "week", 15.0, 10) == (13.5, 10)


def test_apply_pct_floored(env):
    conn, cfg, ids = env
    usd, pct = referrals.apply_pct(conn, cfg, "standard", "week", 15.0, 50)
    assert usd >= 100 * 0.40 * 7 / 30 and pct < 50


def test_apply_pct_fee_on_vat_included(env):
    conn, cfg, ids = env
    referrals.set_settings(conn, ids["admin"], {"fee_pct": 5})
    usd, _ = referrals.apply_pct(conn, cfg, "standard", "week", 15.0, 50)
    assert usd * (1 - 0.05 * 1.1) >= 100 * 0.40 * 7 / 30 - 1e-9


def test_apply_pct_no_room(env):
    conn, cfg, ids = env
    tickets.set_price(conn, cfg, "lite", "day", 0.67, ids["admin"])
    assert referrals.apply_pct(conn, cfg, "lite", "day", 0.67, 10) == (0.67, 0.0)


# ---------- Task 2: codes, the link, and attaching a friend at sign-up ----------

def add_user(conn, name):
    uid, _ = create_user(conn, name)
    return uid


def test_code_stable_and_resolves(env):
    conn, cfg, ids = env
    alice = ids["alice"]
    c = referrals.code_for(conn, alice)
    assert len(c) == 8 and all(ch in referrals.CODE_ALPHABET for ch in c)
    assert c == referrals.code_for(conn, alice)   # stable: the same code on a second call
    assert referrals.user_by_code(conn, c.upper()) == alice   # case-insensitive


def test_user_by_code_unknown_disabled_or_revoked(env):
    conn, cfg, ids = env
    alice = ids["alice"]
    code = referrals.code_for(conn, alice)
    assert referrals.user_by_code(conn, "zzzzzzzz") is None
    conn.execute("UPDATE users SET enabled=0 WHERE id=?", (alice,))
    assert referrals.user_by_code(conn, code) is None
    conn.execute("UPDATE users SET enabled=1, revoked_at=? WHERE id=?", (NOW, alice))
    assert referrals.user_by_code(conn, code) is None


def test_attach_needs_on_and_not_self(env):
    conn, cfg, ids = env
    admin, alice = ids["admin"], ids["alice"]
    bob = add_user(conn, "bob@x.com")
    code = referrals.code_for(conn, alice)
    assert referrals.attach(conn, bob, code) is False            # referrals off
    referrals.set_settings(conn, admin, {"enabled": True})
    assert referrals.attach(conn, alice, code) is False          # own code
    assert referrals.attach(conn, bob, code) is True
    assert conn.execute("SELECT referred_by FROM users WHERE id=?", (bob,)).fetchone()[0] == alice
    assert audit_actions(conn)[-1] == "referral_signup"


def test_attach_none_or_unknown_code(env):
    conn, cfg, ids = env
    admin = ids["admin"]
    referrals.set_settings(conn, admin, {"enabled": True})
    bob = add_user(conn, "bob@x.com")
    assert referrals.attach(conn, bob, None) is False
    assert referrals.attach(conn, bob, "") is False
    assert referrals.attach(conn, bob, "zzzzzzzz") is False
    assert conn.execute("SELECT referred_by FROM users WHERE id=?", (bob,)).fetchone()[0] is None


def test_first_pass_by_email_survives_account_delete(env):
    conn, cfg, ids = env
    admin = ids["admin"]
    bob = add_user(conn, "bob@x.com")
    assert referrals.first_pass(conn, bob) is True
    tickets.grant(conn, cfg, admin, user(conn, bob), "lite", "day", "EUR", now=NOW, terms_version="v1")
    assert referrals.first_pass(conn, bob) is False
    conn.execute("UPDATE users SET revoked_at=? WHERE id=?", (NOW, bob))
    delete_user(conn, bob, admin, now=NOW + 2 * 86400)   # the ticket has ended by then: deletion is allowed
    bob2 = add_user(conn, "bob@x.com")
    assert referrals.first_pass(conn, bob2) is False   # the email already had a pass, on the now-deleted account


def test_first_pass_by_done_order_with_that_email(env):
    conn, cfg, ids = env
    carol = add_user(conn, "carol@x.com")
    assert referrals.first_pass(conn, carol) is True
    conn.execute("INSERT INTO orders(created_at, updated_at, user_id, name, email, tier, length, currency, quoted_usd, "
                "quoted_rate, quoted_amount, status, admin_mail, buyer_mail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (NOW, NOW, None, "Carol", "carol@x.com", "lite", "week", "EUR", 10.0, 1.0, 10.0, "done", "off", "off"))
    assert referrals.first_pass(conn, carol) is False


# ---------- Task 3: the discount on an order, and the life of a unit ----------

from claude_proxy import orders, payments   # noqa: E402
from claude_proxy.config import Currency   # noqa: E402

DAY = 86400


@pytest.fixture
def env_on(env):
    """Referrals on, bob signed up through alice's link, Toman priced for the online-payment path."""
    conn, cfg, ids = env
    referrals.set_settings(conn, ids["admin"], {"enabled": True})
    ids["bob"] = add_user(conn, "bob@x.com")
    conn.execute("UPDATE users SET referred_by=? WHERE id=?", (ids["alice"], ids["bob"]))
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 100000, ids["admin"], now=NOW - 3600)
    return conn, cfg, ids


def signed_in(conn, cfg, uid, now=NOW, **kw):
    args = {"tier": "lite", "length": "week", "currency": "EUR", "name": user(conn, uid)["name"],
            "email": user(conn, uid)["email"] or f"u{uid}@example.com", "terms_version": "v1"} | kw
    return orders.create(conn, cfg, user_id=uid, now=now, **args)


def add_unit(conn, uid, pct=10.0, earned_at=NOW - DAY, expires_at=NOW + 90 * DAY):
    return conn.execute("INSERT INTO referral_units(user_id, pct, earned_at, expires_at, state, updated_at) "
                        "VALUES(?,?,?,?,'saved',?)", (uid, pct, earned_at, expires_at, earned_at)).lastrowid


def unit_state(conn, unit_id):
    return conn.execute("SELECT state FROM referral_units WHERE id=?", (unit_id,)).fetchone()[0]


def add_payment(conn, o, status="started"):
    pid = conn.execute("INSERT INTO payments(order_id, user_id, created_at, updated_at, amount, authority, status) "
                       "VALUES(?,?,?,?,?,?,?)", (o["id"], o["user_id"], NOW, NOW, int(o["quoted_amount"]),
                                                 f"A-{o['id']}-{status}", status)).lastrowid
    return payments.get(conn, pid)


def test_friend_discount_on_first_order(env_on):
    conn, cfg, ids = env_on
    o = signed_in(conn, cfg, ids["bob"])
    assert (o["ref_kind"], o["quoted_usd"], o["list_usd"]) == ("friend", 7.2, 8.0)
    assert (o["ref_pct"], o["ref_off_usd"], o["ref_unit_id"]) == (10, 0.8, None)
    assert o["quoted_amount"] == tickets.round_local(7.2, 0.92, 0.5, 10)   # the amount is the discounted price, VAT on top
    assert "referral_discount" in audit_actions(conn)


def test_friend_discount_needs_referrals_on(env_on):
    conn, cfg, ids = env_on
    referrals.set_settings(conn, ids["admin"], {"enabled": False})
    o = signed_in(conn, cfg, ids["bob"])
    assert (o["ref_kind"], o["quoted_usd"], o["list_usd"]) == (None, 8.0, None)


def test_second_order_no_friend_discount(env_on):
    conn, cfg, ids = env_on
    o = signed_in(conn, cfg, ids["bob"])
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["bob"]), "lite", "week", "EUR", order_id=o["id"], paid=True, now=NOW)
    o2 = signed_in(conn, cfg, ids["bob"], now=NOW + 60)
    assert (o2["ref_kind"], o2["quoted_usd"]) == (None, 8.0)


def test_visitor_order_gets_nothing(env_on):
    conn, cfg, ids = env_on
    add_unit(conn, ids["alice"])
    o = orders.create(conn, cfg, tier="lite", length="week", currency="EUR", name="V", email="v@example.com",
                      ip="10.0.0.1", terms_version="v1", now=NOW)
    assert (o["ref_kind"], o["quoted_usd"]) == (None, 8.0)


def test_quote_changes_nothing(env_on):
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"])
    q = referrals.quote(conn, cfg, ids["alice"], "lite", "week", 8.0, NOW)
    assert q == {"kind": "unit", "pct": 10, "pct_applied": 10, "usd": 7.2, "list_usd": 8.0, "off_usd": 0.8, "unit_id": u}
    assert unit_state(conn, u) == "saved"
    assert referrals.quote(conn, cfg, ids["admin"], "lite", "week", 8.0, NOW) is None


def test_unit_held_then_used_on_payment(env_on):
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"])
    o = signed_in(conn, cfg, ids["alice"], currency="IRT")
    assert o["ref_kind"] == "unit" and o["ref_unit_id"] == u and unit_state(conn, u) == "held"
    assert conn.execute("SELECT order_id FROM referral_units WHERE id=?", (u,)).fetchone()[0] == o["id"]
    p = add_payment(conn, o)
    r = payments._fulfil(conn, cfg, p, "201", None, NOW)
    assert r["status"] == "paid" and unit_state(conn, u) == "used"
    row = conn.execute("SELECT detail_json FROM audit_log WHERE action='referral_unit_used'").fetchone()
    assert '"off_usd": 0.8' in row[0]
    referrals.spend(conn, o["id"], NOW)   # idempotent: a second spend changes nothing
    assert audit_actions(conn).count("referral_unit_used") == 1


def test_unit_used_when_paid_but_not_granted(env_on):
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"])
    o = signed_in(conn, cfg, ids["alice"], currency="IRT")
    p = add_payment(conn, o)
    payments._unfulfilled(conn, p, "201", None, NOW, "sold out")
    assert unit_state(conn, u) == "used"


def test_unit_used_by_referrals_off_too(env_on):
    """Turning referrals off keeps saved units usable (design: Settings)."""
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"])
    referrals.set_settings(conn, ids["admin"], {"enabled": False})
    o = signed_in(conn, cfg, ids["alice"])
    assert o["ref_kind"] == "unit" and unit_state(conn, u) == "held"


def test_one_unit_per_order(env_on):
    conn, cfg, ids = env_on
    first = add_unit(conn, ids["alice"], earned_at=NOW - 3 * DAY)
    others = [add_unit(conn, ids["alice"], earned_at=NOW - 2 * DAY), add_unit(conn, ids["alice"], earned_at=NOW - DAY)]
    o = signed_in(conn, cfg, ids["alice"])
    assert (o["ref_unit_id"], o["ref_pct"], o["quoted_usd"]) == (first, 10, 7.2)
    assert [unit_state(conn, u) for u in others] == ["saved", "saved"]


def test_withdraw_returns_unit(env_on):
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"])
    o = signed_in(conn, cfg, ids["alice"])
    orders.withdraw(conn, ids["alice"], o["id"], now=NOW + 60)
    assert unit_state(conn, u) == "saved"
    assert conn.execute("SELECT order_id FROM referral_units WHERE id=?", (u,)).fetchone()[0] is None
    assert "referral_unit_returned" in audit_actions(conn)
    o2 = signed_in(conn, cfg, ids["alice"], now=NOW + 120)   # and the next order takes it again
    assert o2["ref_unit_id"] == u and unit_state(conn, u) == "held"


def test_withdraw_after_expiry_expires_unit(env_on):
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"], expires_at=NOW + 100)
    o = signed_in(conn, cfg, ids["alice"])
    orders.withdraw(conn, ids["alice"], o["id"], now=NOW + 200)
    assert unit_state(conn, u) == "expired"
    assert "referral_unit_expired" in audit_actions(conn)


def test_held_unit_survives_its_expiry(env_on):
    """A unit held by an open order stays with it past expires_at (design: the buyer already saw that price)."""
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"], expires_at=NOW + 100)
    signed_in(conn, cfg, ids["alice"])
    referrals.expire(conn, NOW + 200)
    assert unit_state(conn, u) == "held"


def test_floored_to_zero_keeps_unit_saved(env_on):
    conn, cfg, ids = env_on
    tickets.set_price(conn, cfg, "lite", "week", 4.67, ids["admin"])   # cost: 100 * 20% * 7 / 30 = 4.666...
    u = add_unit(conn, ids["alice"])
    o = signed_in(conn, cfg, ids["alice"])
    assert (o["ref_kind"], o["quoted_usd"], o["ref_unit_id"]) == (None, 4.67, None)
    assert unit_state(conn, u) == "saved"


def test_paid_unfulfilled_then_declined_keeps_unit_spent(env_on):
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"])
    o = signed_in(conn, cfg, ids["alice"], currency="IRT")
    add_payment(conn, o, status="paid_unfulfilled")
    orders.set_status(conn, ids["admin"], o["id"], "declined", now=NOW + 60)
    assert unit_state(conn, u) == "used"


def test_expired_saved_unit_not_offered(env_on):
    conn, cfg, ids = env_on
    old = add_unit(conn, ids["alice"], earned_at=NOW - 100 * DAY, expires_at=NOW - 1)
    o = signed_in(conn, cfg, ids["alice"])
    assert (o["ref_kind"], o["quoted_usd"]) == (None, 8.0)
    assert unit_state(conn, old) == "expired" and "referral_unit_expired" in audit_actions(conn)


def test_payment_link_untouched(env_on):
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"])
    o = orders.create_link(conn, cfg, ids["admin"], email="alice@example.com", tier="lite", length="week",
                           pay_by=NOW + DAY, currency="EUR", now=NOW)
    assert (o["ref_kind"], o["list_usd"]) == (None, None)
    assert unit_state(conn, u) == "saved"


# ---------- Task 3, fix round 1 ----------

def test_friend_order_at_cost_still_marked_friend(env_on):
    """The friend's first paid pass is what counts for the referrer (Task 4), discounted or not."""
    conn, cfg, ids = env_on
    tickets.set_price(conn, cfg, "lite", "week", 4.67, ids["admin"])   # at cost: the floor leaves no room
    o = signed_in(conn, cfg, ids["bob"])
    assert (o["ref_kind"], o["quoted_usd"], o["ref_off_usd"], o["ref_pct"], o["list_usd"]) == ("friend", 4.67, 0, 10, 4.67)
    row = conn.execute("SELECT actor_user_id FROM audit_log WHERE action='referral_discount'").fetchone()
    assert row[0] == ids["bob"]   # the buyer is the actor


def test_friend_pct_zero_still_marked_friend(env_on):
    conn, cfg, ids = env_on
    referrals.set_settings(conn, ids["admin"], {"friend_pct": 0})
    o = signed_in(conn, cfg, ids["bob"])
    assert (o["ref_kind"], o["quoted_usd"], o["ref_off_usd"]) == ("friend", 8.0, 0)


def test_late_verify_after_close_uses_the_returned_unit(env_on):
    """_close withdraws the order and gives the unit back; ZarinPal then verifies late: the buyer paid the discounted
    price, so the unit is used, not left saved for a second discount."""
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"])
    o = signed_in(conn, cfg, ids["alice"], currency="IRT")
    p = add_payment(conn, o)
    payments._close(conn, p, "expired", NOW + 60)
    assert orders.get(conn, o["id"])["status"] == "withdrawn" and unit_state(conn, u) == "saved"
    payments._unfulfilled(conn, payments.get(conn, p["id"]), "201", None, NOW + 120, "verified after the payment was closed")
    assert unit_state(conn, u) == "used"
    assert conn.execute("SELECT order_id FROM referral_units WHERE id=?", (u,)).fetchone()[0] == o["id"]
    referrals.spend(conn, o["id"], NOW + 180)   # still idempotent
    assert audit_actions(conn).count("referral_unit_used") == 1


def test_late_verify_when_another_order_took_the_unit_is_a_conflict(env_on):
    conn, cfg, ids = env_on
    u = add_unit(conn, ids["alice"])
    o = signed_in(conn, cfg, ids["alice"], currency="IRT")
    p = add_payment(conn, o)
    payments._close(conn, p, "expired", NOW + 60)
    o2 = signed_in(conn, cfg, ids["alice"], now=NOW + 90)
    assert o2["ref_unit_id"] == u and unit_state(conn, u) == "held"
    payments._unfulfilled(conn, payments.get(conn, p["id"]), "201", None, NOW + 120, "late")
    assert unit_state(conn, u) == "held"   # left with the open order
    row = conn.execute("SELECT detail_json FROM audit_log WHERE action='referral_unit_conflict'").fetchone()
    assert f'"order_id": {o["id"]}' in row[0] and f'"other_order_id": {o2["id"]}' in row[0]


# ---------- Task 4: grants keep the discount; earning units ----------

from datetime import UTC, datetime   # noqa: E402


def units_of(conn, uid):
    return [dict(r) for r in conn.execute("SELECT * FROM referral_units WHERE user_id=? ORDER BY id", (uid,))]


def reward_of(conn, order_id):
    row = conn.execute("SELECT * FROM referral_rewards WHERE order_id=?", (order_id,)).fetchone()
    return dict(row) if row else None


def friend_of(conn, referrer, name):
    uid = add_user(conn, name)
    conn.execute("UPDATE users SET referred_by=? WHERE id=?", (referrer, uid))
    return uid


def grant_paid(conn, cfg, ids, uid, o, now=NOW):
    return tickets.grant(conn, cfg, ids["admin"], user(conn, uid), o["tier"], o["length"], o["currency"],
                         order_id=o["id"], paid=True, now=now)


def test_manual_grant_keeps_friend_discount(env_on):
    conn, cfg, ids = env_on
    admin, bob = ids["admin"], ids["bob"]
    o = signed_in(conn, cfg, bob)                       # $7.20
    pv = tickets.preview(conn, cfg, user(conn, bob), "lite", "week", "EUR", NOW, order_id=o["id"])
    assert (pv["usd"], pv["ref_pct_applied"], pv["ref_off_usd"]) == (7.2, 10, 0.8)
    assert pv["amount"] == o["quoted_amount"]
    plain = tickets.preview(conn, cfg, user(conn, bob), "lite", "week", "EUR", NOW)
    assert plain["usd"] == 8.0 and "ref_pct_applied" not in plain
    t = tickets.grant(conn, cfg, admin, user(conn, bob), "lite", "week", "EUR", expect_usd=7.2, expect_rate=pv["rate"],
                      order_id=o["id"], now=NOW)
    assert t["usd"] == 7.2 and t["list_usd"] == 8.0 and t["amount"] == pv["amount"]


def test_manual_grant_at_the_regular_price_is_refused(env_on):
    conn, cfg, ids = env_on
    bob = ids["bob"]
    o = signed_in(conn, cfg, bob)
    with pytest.raises(tickets.QuoteChanged):
        tickets.grant(conn, cfg, ids["admin"], user(conn, bob), "lite", "week", "EUR", expect_usd=8.0, order_id=o["id"], now=NOW)


def test_share_raised_before_grant_refloors(env_on):
    conn, cfg, ids = env_on
    bob = ids["bob"]
    o = signed_in(conn, cfg, bob, tier="standard")       # $15 less 10% = $13.50
    assert o["quoted_usd"] == 13.5
    tickets.set_share(conn, cfg, "standard", 60, ids["admin"])   # cost now 100 * 60% * 7 / 30 = $14
    pv = tickets.preview(conn, cfg, user(conn, bob), "standard", "week", "EUR", NOW, order_id=o["id"])
    assert pv["usd"] >= referrals.pass_cost(conn, cfg, "standard", 7) and pv["usd"] == 14.0
    assert 0 < pv["ref_pct_applied"] < 10 and pv["ref_off_usd"] == 1.0


def test_friend_grant_earns_unit(env_on):
    conn, cfg, ids = env_on
    alice, bob = ids["alice"], ids["bob"]
    o = signed_in(conn, cfg, bob)
    grant_paid(conn, cfg, ids, bob, o)
    us = units_of(conn, alice)
    assert len(us) == 1 and (us[0]["state"], us[0]["pct"], us[0]["earned_at"], us[0]["expires_at"]) == \
        ("saved", 10, NOW, NOW + 90 * DAY)
    r = reward_of(conn, o["id"])
    assert (r["referrer_id"], r["friend_id"], r["status"], us[0]["reward_id"]) == (alice, bob, "earned", r["id"])
    assert "referral_unit_earned" in audit_actions(conn)


def test_unit_used_on_grant(env_on):
    """Task 3's ruling: a unit held by a manually granted order is used by the grant itself."""
    conn, cfg, ids = env_on
    alice = ids["alice"]
    u = add_unit(conn, alice)
    o = signed_in(conn, cfg, alice)
    pv = tickets.preview(conn, cfg, user(conn, alice), "lite", "week", "EUR", NOW, order_id=o["id"])
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, alice), "lite", "week", "EUR", expect_usd=pv["usd"],
                      order_id=o["id"], now=NOW)
    assert t["usd"] == 7.2 and unit_state(conn, u) == "used"
    assert reward_of(conn, o["id"]) is None   # a unit order earns nothing


def test_manual_approval_waits(env_on):
    conn, cfg, ids = env_on
    admin, alice, bob = ids["admin"], ids["alice"], ids["bob"]
    referrals.set_settings(conn, admin, {"approval": "manual"})
    o = signed_in(conn, cfg, bob)
    grant_paid(conn, cfg, ids, bob, o)
    r = reward_of(conn, o["id"])
    assert r["status"] == "waiting" and units_of(conn, alice) == []
    assert referrals.approve(conn, admin, r["id"], NOW + 60) == "earned"
    r = reward_of(conn, o["id"])
    assert (r["status"], r["decided_by"], r["decided_at"]) == ("earned", admin, NOW + 60)
    assert [u["state"] for u in units_of(conn, alice)] == ["saved"]
    with pytest.raises(referrals.ReferralError) as e:
        referrals.approve(conn, admin, r["id"], NOW + 120)
    assert e.value.status == 409


def test_refuse(env_on):
    conn, cfg, ids = env_on
    admin, alice, bob = ids["admin"], ids["alice"], ids["bob"]
    referrals.set_settings(conn, admin, {"approval": "manual"})
    o = signed_in(conn, cfg, bob)
    grant_paid(conn, cfg, ids, bob, o)
    r = reward_of(conn, o["id"])
    referrals.refuse(conn, admin, r["id"], NOW + 60)
    assert reward_of(conn, o["id"])["status"] == "refused" and units_of(conn, alice) == []
    assert audit_actions(conn)[-1] == "referral_refused"
    with pytest.raises(referrals.ReferralError):
        referrals.refuse(conn, admin, r["id"], NOW + 60)
    with pytest.raises(referrals.ReferralError):
        referrals.approve(conn, admin, r["id"], NOW + 60)


def test_over_limit_held(env_on):
    conn, cfg, ids = env_on
    alice, bob = ids["alice"], ids["bob"]
    referrals.set_settings(conn, ids["admin"], {"max_held": 1})
    add_unit(conn, alice)
    o = signed_in(conn, cfg, bob)
    grant_paid(conn, cfg, ids, bob, o)
    assert reward_of(conn, o["id"])["status"] == "over_limit" and len(units_of(conn, alice)) == 1
    assert "referral_over_limit" in audit_actions(conn)


def test_month_limit_tehran(env_on):
    conn, cfg, ids = env_on
    admin, alice, bob = ids["admin"], ids["alice"], ids["bob"]
    referrals.set_settings(conn, admin, {"max_per_month": 1})
    o = signed_in(conn, cfg, bob)
    grant_paid(conn, cfg, ids, bob, o)
    assert reward_of(conn, o["id"])["status"] == "earned"
    carol = friend_of(conn, alice, "carol@x.com")
    o2 = signed_in(conn, cfg, carol)
    grant_paid(conn, cfg, ids, carol, o2)
    assert reward_of(conn, o2["id"])["status"] == "over_limit"
    # 1 Feb 00:00 in Tehran is still 31 Jan in UTC: the Tehran month is what counts.
    feb = int(datetime(2027, 2, 1, tzinfo=tickets.TEHRAN).timestamp())
    referrals.set_settings(conn, admin, {"approval": "manual"})
    dave = friend_of(conn, alice, "dave@x.com")
    o3 = signed_in(conn, cfg, dave)
    grant_paid(conn, cfg, ids, dave, o3)
    r3 = reward_of(conn, o3["id"])
    assert referrals.approve(conn, admin, r3["id"], feb - 60) == "over_limit"   # still January in Tehran
    erin = friend_of(conn, alice, "erin@x.com")
    o4 = signed_in(conn, cfg, erin)
    grant_paid(conn, cfg, ids, erin, o4)
    assert referrals.approve(conn, admin, reward_of(conn, o4["id"])["id"], feb + 60) == "earned"
    assert datetime.fromtimestamp(feb + 60, tickets.TEHRAN).month == 2 and datetime.fromtimestamp(feb + 60, UTC).month == 1


def test_deleted_referrer_friend_keeps_discount_no_reward(env_on):
    conn, cfg, ids = env_on
    admin, alice, bob = ids["admin"], ids["alice"], ids["bob"]
    o = signed_in(conn, cfg, bob)
    conn.execute("UPDATE users SET revoked_at=? WHERE id=?", (NOW, alice))
    delete_user(conn, alice, admin, now=NOW)
    pv = tickets.preview(conn, cfg, user(conn, bob), "lite", "week", "EUR", NOW, order_id=o["id"])
    t = tickets.grant(conn, cfg, admin, user(conn, bob), "lite", "week", "EUR", expect_usd=pv["usd"], order_id=o["id"], now=NOW)
    assert t["usd"] == 7.2 and reward_of(conn, o["id"]) is None


def test_revoked_or_disabled_referrer_no_reward(env_on):
    conn, cfg, ids = env_on
    alice, bob = ids["alice"], ids["bob"]
    conn.execute("UPDATE users SET enabled=0 WHERE id=?", (alice,))
    o = signed_in(conn, cfg, bob)
    grant_paid(conn, cfg, ids, bob, o)
    assert reward_of(conn, o["id"]) is None and units_of(conn, alice) == []
    conn.execute("UPDATE users SET enabled=1, revoked_at=? WHERE id=?", (NOW, alice))
    carol = friend_of(conn, alice, "carol@x.com")
    o2 = signed_in(conn, cfg, carol)
    grant_paid(conn, cfg, ids, carol, o2)
    assert reward_of(conn, o2["id"]) is None


def test_paid_online_grant_earns(env_on):
    conn, cfg, ids = env_on
    alice, bob = ids["alice"], ids["bob"]
    o = signed_in(conn, cfg, bob, currency="IRT")
    p = add_payment(conn, o)
    r = payments._fulfil(conn, cfg, p, "201", None, NOW)
    assert r["status"] == "paid"
    assert reward_of(conn, o["id"])["status"] == "earned" and len(units_of(conn, alice)) == 1
    t = tickets.get(conn, orders.get(conn, o["id"])["ticket_id"])
    assert (t["usd"], t["list_usd"]) == (7.2, 8.0)


def test_off_no_new_unit_but_approve_works(env_on):
    conn, cfg, ids = env_on
    admin, alice, bob = ids["admin"], ids["alice"], ids["bob"]
    referrals.set_settings(conn, admin, {"approval": "manual"})
    o = signed_in(conn, cfg, bob)
    grant_paid(conn, cfg, ids, bob, o)
    waiting = reward_of(conn, o["id"])
    carol = friend_of(conn, alice, "carol@x.com")
    u = add_unit(conn, carol)                            # carol also holds a unit, unused: a first pass gets only the friend discount
    o2 = signed_in(conn, cfg, carol)                     # a friend order made while referrals were on
    assert (o2["ref_kind"], o2["ref_unit_id"], unit_state(conn, u)) == ("friend", None, "saved")
    referrals.set_settings(conn, admin, {"enabled": False})
    grant_paid(conn, cfg, ids, carol, o2)
    assert reward_of(conn, o2["id"]) is None and unit_state(conn, u) == "saved"
    assert referrals.approve(conn, admin, waiting["id"], NOW + 60) == "earned"
    assert len(units_of(conn, alice)) == 1


def test_off_still_spends_held_unit_on_grant(env_on):
    conn, cfg, ids = env_on
    alice = ids["alice"]
    u = add_unit(conn, alice)
    o = signed_in(conn, cfg, alice)
    referrals.set_settings(conn, ids["admin"], {"enabled": False})
    pv = tickets.preview(conn, cfg, user(conn, alice), "lite", "week", "EUR", NOW, order_id=o["id"])
    tickets.grant(conn, cfg, ids["admin"], user(conn, alice), "lite", "week", "EUR", expect_usd=pv["usd"], order_id=o["id"], now=NOW)
    assert pv["usd"] == 7.2 and unit_state(conn, u) == "used"


def test_friend_order_after_another_pass_earns_nothing(env_on):
    """The friend's open order was made before the admin granted them a pass directly: it is no longer their first."""
    conn, cfg, ids = env_on
    alice, bob = ids["alice"], ids["bob"]
    o = signed_in(conn, cfg, bob, length="day")
    tickets.grant(conn, cfg, ids["admin"], user(conn, bob), "lite", "day", "EUR", now=NOW, terms_version="v1")
    grant_paid(conn, cfg, ids, bob, o)
    assert reward_of(conn, o["id"]) is None and units_of(conn, alice) == []


# ---------- Task 4, fix round 1 ----------

@pytest.mark.parametrize("grant_at, expires_at, after", [(NOW, NOW + 90 * DAY, "saved"), (NOW + 200, NOW + 100, "expired")])
def test_unit_floored_to_zero_at_manual_grant_is_not_used(env_on, grant_at, expires_at, after):
    """The share rose after the order so far that the floor leaves no room: the pass sells at full price and the unit
    goes back (design: The cost floor, if it is reduced to zero the unit is not used)."""
    conn, cfg, ids = env_on
    alice = ids["alice"]
    u = add_unit(conn, alice, expires_at=expires_at)
    o = signed_in(conn, cfg, alice)
    assert o["quoted_usd"] == 7.2 and unit_state(conn, u) == "held"
    tickets.set_share(conn, cfg, "lite", 40, ids["admin"])   # cost now 100 * 40% * 7 / 30 = $9.33, above $8
    pv = tickets.preview(conn, cfg, user(conn, alice), "lite", "week", "EUR", grant_at, order_id=o["id"])
    assert (pv["usd"], pv["ref_pct_applied"]) == (8.0, 0.0)
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, alice), "lite", "week", "EUR", expect_usd=8.0,
                      order_id=o["id"], now=grant_at)
    assert t["usd"] == 8.0 and unit_state(conn, u) == after
    assert "referral_unit_used" not in audit_actions(conn)


def test_unit_floored_to_zero_paid_grant_still_used(env_on):
    """Paid online at the quoted, discounted price: the unit is used whatever today's floor says."""
    conn, cfg, ids = env_on
    alice = ids["alice"]
    u = add_unit(conn, alice)
    o = signed_in(conn, cfg, alice, currency="IRT")
    tickets.set_share(conn, cfg, "lite", 40, ids["admin"])
    r = payments._fulfil(conn, cfg, add_payment(conn, o), "201", None, NOW)
    assert r["status"] == "paid" and unit_state(conn, u) == "used"


# ---------- Task 5, fix round 1: cancel_unit ----------

def test_cancel_unit_saved(env):
    conn, cfg, ids = env
    u = add_unit(conn, ids["alice"])
    referrals.cancel_unit(conn, ids["admin"], u, NOW)
    assert unit_state(conn, u) == "cancelled"
    assert audit_actions(conn)[-1] == "referral_unit_cancelled"


def test_cancel_unit_not_saved_is_409_and_not_audited_again(env):
    conn, cfg, ids = env
    u = add_unit(conn, ids["alice"])
    referrals.cancel_unit(conn, ids["admin"], u, NOW)   # cancelled once
    with pytest.raises(referrals.ReferralError) as e:
        referrals.cancel_unit(conn, ids["admin"], u, NOW)
    assert e.value.status == 409
    assert audit_actions(conn).count("referral_unit_cancelled") == 1   # the second attempt left no new row


def test_cancel_unit_unknown_is_404(env):
    conn, cfg, ids = env
    with pytest.raises(referrals.ReferralError) as e:
        referrals.cancel_unit(conn, ids["admin"], 99999, NOW)
    assert e.value.status == 404


class _RaceOnCancel:
    """A thin proxy over a real connection: when cancel_unit's own cancelling UPDATE runs, first flips the unit's
    state out from under it, standing in for hold() (orders.create) landing between cancel_unit's SELECT and its
    UPDATE. sqlite3.Connection is an immutable C type (its methods can't be monkeypatched), and a *second* real
    connection can no longer land a write in that gap at all once cancel_unit holds its own BEGIN IMMEDIATE - which
    is the point of the fix - so this is the practical way left to exercise the UPDATE's rowcount check on its own."""

    def __init__(self, real, unit_id):
        self._real, self._unit_id = real, unit_id

    def execute(self, sql, parameters=()):
        if sql.startswith("UPDATE referral_units SET state='cancelled'"):
            self._real.execute("UPDATE referral_units SET state='held' WHERE id=?", (self._unit_id,))
        return self._real.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_cancel_unit_rechecks_state_at_update_not_just_at_select(env):
    """A unit taken by hold() between cancel_unit's own read and its UPDATE must not be cancelled: the UPDATE's
    rowcount decides it, not the state the SELECT first saw (fix round 1: the earlier version ran unlocked and
    never checked the UPDATE's rowcount, so it would have audited a cancel that changed nothing)."""
    conn, cfg, ids = env
    u = add_unit(conn, ids["alice"])
    with pytest.raises(referrals.ReferralError) as e:
        referrals.cancel_unit(_RaceOnCancel(conn, u), ids["admin"], u, NOW)
    assert e.value.status == 409
    assert unit_state(conn, u) == "saved"   # the whole transaction - the simulated race included - rolled back
    assert "referral_unit_cancelled" not in audit_actions(conn)


# ---------- final review fix wave ----------

@pytest.mark.parametrize("key,value", [
    ("friend_pct", True), ("friend_pct", "10"), ("friend_pct", None), ("friend_pct", float("nan")),
    ("friend_pct", float("inf")), ("friend_pct", -1), ("friend_pct", 90.5),
    ("unit_pct", False), ("unit_pct", [10]),
    ("max_held", 2.5), ("max_held", True), ("max_held", -1), ("max_held", 1001), ("max_per_month", "5"),
    ("unit_days", 0), ("unit_days", 3651), ("unit_days", True),
    ("account_cost_usd", float("nan")), ("account_cost_usd", float("inf")), ("account_cost_usd", 0),
    ("account_cost_usd", True), ("account_cost_usd", "100"),
    ("fee_pct", float("nan")), ("fee_pct", float("-inf")), ("fee_pct", 20.01), ("fee_pct", -0.1), ("fee_pct", True),
    ("enabled", 1), ("enabled", "true"), ("approval", 1), ("nope", 1),
])
def test_validate_refuses_bad_inputs_with_400(env, key, value):
    """Bools, non-finite numbers, wrong types and out-of-range values are a 400 ReferralError, never a 500 (and
    nothing is stored: a NaN account cost would silently switch the cost floor off)."""
    conn, cfg, ids = env
    before = referrals.settings(conn)
    with pytest.raises(referrals.ReferralError) as e:
        referrals.set_settings(conn, ids["admin"], {key: value})
    assert e.value.status == 400
    assert referrals.settings(conn) == before


@pytest.mark.parametrize("key,value", [
    ("friend_pct", 0), ("friend_pct", 90), ("unit_pct", 12.5), ("max_held", 0), ("max_per_month", 1000),
    ("unit_days", 1), ("unit_days", 3650), ("account_cost_usd", 0.01), ("fee_pct", 0), ("fee_pct", 20),
])
def test_validate_accepts_the_range_ends(env, key, value):
    conn, cfg, ids = env
    assert getattr(referrals.set_settings(conn, ids["admin"], {key: value}), key) == value


def test_my_status_off_still_lists_saved_and_held_units(env):
    """Saved units stay usable with referrals off (design: Settings), so the card and /invite still list them, with
    no link: only the link and the friend discount go away."""
    conn, cfg, ids = env
    add_unit(conn, ids["alice"], pct=15.0)
    s = referrals.my_status(conn, cfg, ids["alice"], NOW)
    assert s["enabled"] is False and "link" not in s
    assert [(u["pct"], u["state"]) for u in s["units"]] == [(15.0, "saved")] and s["waiting"] == 0
    assert conn.execute("SELECT ref_code FROM users WHERE id=?", (ids["alice"],)).fetchone()[0] is None   # no code made


def test_local_quote_prices_the_discount_in_a_currency(env):
    """local_quote: quote() priced like the price list in one currency (VAT in), with no unit_id, falling back to
    USD for a currency with no rate."""
    conn, cfg, ids = env
    add_unit(conn, ids["alice"])
    usd = tickets.price_now(conn, "standard", "week", NOW)["usd"]
    q = referrals.local_quote(conn, cfg, ids["alice"], "standard", "week", "USD", NOW)
    assert "unit_id" not in q and q["kind"] == "unit" and q["currency"] == "USD"
    assert q["list_amount"] == tickets.round_local(usd, 1, tickets.currencies(cfg)["USD"], 10)
    assert q["amount"] == tickets.round_local(q["usd"], 1, tickets.currencies(cfg)["USD"], 10) < q["list_amount"]
    assert referrals.local_quote(conn, cfg, ids["alice"], "standard", "week", "XYZ", NOW)["currency"] == "USD"
    assert referrals.local_quote(conn, cfg, ids["admin"], "standard", "week", "USD", NOW) is None
