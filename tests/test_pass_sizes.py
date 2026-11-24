"""Pass sizes in Claude Pro weeks (multi-subscription design 2026-11-24): a tier's size becomes each account's share by
its plan factor; the migration keeps today's shares on the default account."""
import pytest

from claude_proxy import db, plans, referrals, tickets, trials
from claude_proxy.config import Config, ConfigError
from claude_proxy.db import init_db
from tests.tickets_helpers import NOW, link, live, seeded, tcfg, user


def test_share_from_size_per_plan(db):
    conn, cfg, ids = seeded(db)
    for account, plan in (("p", "pro"), ("m5", "max_5x"), ("m20", "max_20x")):
        link(conn, account, plan)
    tickets.set_size(conn, cfg, "standard", 0.5, ids["admin"])
    assert tickets.effective_share(conn, cfg, "standard", "p") == 50.0
    assert tickets.effective_share(conn, cfg, "standard", "m5") == 10.0
    assert tickets.effective_share(conn, cfg, "standard", "m20") == 2.5
    plans.set_factor(conn, "max_20x", 20, 10, ids["admin"])
    assert tickets.effective_share_5h(conn, cfg, "standard", "m20") == 5.0


def _old_db(tmp_path, tier_plan):
    """A database from before sizes: ticket_tier_shares without size columns, the default account on `tier_plan`."""
    conn = init_db(str(tmp_path / "old.db"))
    for col in ("size", "size_5h"):
        conn.execute(f"ALTER TABLE ticket_tier_shares DROP COLUMN {col}")
    conn.execute("INSERT INTO account_plans(observed_at, organization_type, rate_limit_tier, account) VALUES(1, 'claude_max', ?, 'default')",
                 (tier_plan,))
    conn.execute("INSERT INTO ticket_tier_shares(tier, share_pct, updated_at, share_5h_pct) VALUES('lite', 5, 1, 7)")
    conn.execute("INSERT INTO ticket_tier_shares(tier, share_pct, updated_at, share_5h_pct) VALUES('standard', 25, 1, NULL)")
    return conn


def test_migration_keeps_tier_shares_on_default(tmp_path):
    conn = _old_db(tmp_path, "default_claude_max_20x")
    conn.execute("INSERT INTO tickets(user_id, user_name, tier, share_pct, length, days, starts_at, ends_at, list_usd, usd, currency, "
                 "rate, amount, granted_at) VALUES(NULL, 'u', 'standard', 25, 'week', 7, 1, 2, 1, 1, 'USD', 1, 1, 1)")
    db._migrate(conn)
    rows = {r["tier"]: (r["size"], r["size_5h"]) for r in conn.execute("SELECT tier, size, size_5h FROM ticket_tier_shares")}
    assert rows["lite"] == pytest.approx((1.0, 1.4)) and rows["standard"][0] == pytest.approx(5.0) and rows["standard"][1] is None
    cfg = tcfg()
    assert abs(tickets.effective_share(conn, cfg, "standard", "default") - 25.0) < 1e-9
    assert abs(tickets.effective_share_5h(conn, cfg, "lite", "default") - 7.0) < 1e-9
    assert conn.execute("SELECT share_pct FROM tickets").fetchone()[0] == 25


def test_migration_unknown_plan_leaves_size_null(tmp_path):
    conn = _old_db(tmp_path, "default_claude_max")
    db._migrate(conn)
    assert conn.execute("SELECT size FROM ticket_tier_shares WHERE tier='lite'").fetchone()[0] is None
    assert tickets.tier_size(conn, tcfg(), "lite") == 0.05


def test_config_rejects_share_pct(tmp_path):
    def load(body):
        p = tmp_path / "c.toml"
        p.write_text("[tickets]\nmax_sold_pct = 80\n[tickets.tiers.x]\nlabel = 'X'\ndefault_usd = { day = 1, week = 2, month = 3 }\n" + body)
        return Config.load(str(p))
    with pytest.raises(ConfigError, match="size"):
        load("share_pct = 5\n")
    with pytest.raises(ConfigError, match="fits no plan"):
        load("size = 100\n")
    assert load("size = 10\n").tickets.tiers["x"].size == 10


def test_set_size_validation(db):
    conn, cfg, ids = seeded(db)
    with pytest.raises(tickets.TicketError):
        tickets.set_size(conn, cfg, "lite", 0, ids["admin"])
    tickets.set_size(conn, cfg, "lite", 0.2, ids["admin"])
    assert tickets.effective_share(conn, cfg, "lite", "default") == 20.0


def test_price_table_compare_is_size(db):
    conn, cfg, _ = seeded(db)
    live(conn)
    std = next(t for t in tickets.price_table(conn, cfg, NOW, "USD")["tiers"] if t["tier"] == "standard")
    assert (std["compare"], std["compare_pct"]) == ("Claude Pro", 25)


def test_trial_sizes(tmp_path):
    conn, cfg, ids = seeded((None, init_db(str(tmp_path / "t.db"))))
    live(conn, NOW)
    trials.set_settings(conn, cfg, ids["admin"], {"enabled": True, "size": 0.01})
    trials.claim(conn, cfg, user(conn, ids["alice"]), NOW)
    assert trials.start(conn, cfg, ids["alice"], NOW)["share_pct"] == pytest.approx(1.0)


def test_pass_cost_in_pro_weeks(db):
    conn, cfg, ids = seeded(db)
    referrals.set_settings(conn, ids["admin"], {"account_cost_usd": 20})
    assert referrals.pass_cost(conn, cfg, "standard", 30) == pytest.approx(5.0)


def test_migration_converts_trial_and_referral_settings(tmp_path):
    import json
    conn = _old_db(tmp_path, "default_claude_max_5x")
    conn.execute("INSERT INTO settings(key, value) VALUES('trial', ?)", (json.dumps({"enabled": True, "share_pct": 2, "share_5h_pct": None}),))
    conn.execute("INSERT INTO settings(key, value) VALUES('referrals', ?)", (json.dumps({"account_cost_usd": 100}),))
    db._migrate(conn)
    trial = json.loads(conn.execute("SELECT value FROM settings WHERE key='trial'").fetchone()[0])
    assert trial["size"] == pytest.approx(0.1) and "share_pct" not in trial and "size_5h" not in trial
    assert referrals.settings(conn).account_cost_usd == pytest.approx(20.0)   # $100 for Max 5x: $20 per Claude Pro


def test_migration_stores_the_old_referral_default_when_never_saved(tmp_path):
    # No referral settings row: the old $100 default was the default account's cost, so it is stored per Claude Pro.
    conn = _old_db(tmp_path, "default_claude_max_20x")
    db._migrate(conn)
    assert referrals.settings(conn).account_cost_usd == pytest.approx(5.0)


def test_migration_leaves_unsaved_referral_cost_with_plan_unknown(tmp_path):
    conn = _old_db(tmp_path, "default_claude_max")
    db._migrate(conn)
    assert conn.execute("SELECT 1 FROM settings WHERE key='referrals'").fetchone() is None
    assert referrals.settings(conn).account_cost_usd == 20.0
