"""Referrals (design 2026-11-07), task 1: settings, schema and the cost floor."""
import pytest

from claude_proxy import referrals, tickets
from claude_proxy.config import ZarinpalConfig
from tests.tickets_helpers import NOW, live, seeded


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
