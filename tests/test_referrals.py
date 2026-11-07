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
