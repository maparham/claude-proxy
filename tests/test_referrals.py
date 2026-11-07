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
