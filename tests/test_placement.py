"""Placing passes on accounts (multi-subscription design 2026-11-24, §4): a sold pass goes to the sellable account where
it fits with the most free room in Claude Pro weeks, ties by slug; it fits nowhere: sold out."""
import time
from pathlib import Path

import httpx
import pytest

from claude_proxy import login, payments, placement, plans, tickets, trials, zarinpal
from claude_proxy.config import Currency, ZarinpalConfig
from claude_proxy.db import create_user
from tests.conftest import seed_oauth
from tests.tickets_helpers import DAY, NOW, link, tcfg, user

SRC = Path(__file__).resolve().parents[1] / "src" / "claude_proxy"


def fresh(conn, account, t=NOW, util7=0.0):
    """A fresh weekly report for `account` at `t`: `util7` used, the reset 3 days after `t`."""
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at, account) "
                 "VALUES(?,?,?,?,?,?)", (t - 60, "header", "7d", util7, t + 3 * DAY, account))


def room_util(room):
    """The weekly utilization that leaves `room` points free: max_sold_pct 80, sell_margin_pct 5, nothing reserved."""
    return 80 - 5 - room


def setup(db, t=NOW, p_room=60.0, m_room=10.0):
    """Accounts `p` (Claude Pro) and `m` (Claude Max 20x), both linked and fresh; prices and sizes from the default
    tiers; an admin and alice (unassigned: two accounts are linked); EUR at 0.92."""
    conn = db[1]
    cfg = tcfg()
    for a in ("p", "m", "u"):
        conn.execute("INSERT INTO settings(key, value) VALUES(?, '4102444800')", (f"subscription_ends_at:{a}",))
    link(conn, "p", "pro")
    link(conn, "m", "max_20x")
    fresh(conn, "p", t, room_util(p_room))
    fresh(conn, "m", t, room_util(m_room))
    tickets.seed_prices(conn, cfg, now=t - DAY)
    tickets.seed_sizes(conn, cfg, now=t - DAY)
    admin, _ = create_user(conn, "admin", role="admin")
    alice, _ = create_user(conn, "alice")
    tickets.set_rate(conn, cfg, "EUR", 0.92, admin, now=t - 3600)
    return conn, cfg, {"admin": admin, "alice": alice}


def grant(conn, cfg, ids, tier="standard", length="week", now=NOW, **kw):
    return tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), tier, length, "EUR", terms_version="v1",
                         now=now, **kw)


def test_most_free_pro_weeks_wins(db):
    conn, cfg, ids = setup(db)
    assert placement.free_pro_weeks(conn, cfg, "p", NOW) == pytest.approx(0.6)
    assert placement.free_pro_weeks(conn, cfg, "m", NOW) == pytest.approx(2.0)
    t = grant(conn, cfg, ids)
    assert t["account"] == "m" and t["share_pct"] == 1.25
    assert (t["size"], t["size_5h"]) == (0.25, 0.25)


def test_equal_room_goes_by_slug(db):
    conn, cfg, ids = setup(db, p_room=60.0, m_room=3.0)   # 0.6 Pro weeks each
    acct, rows = placement.pick_tier(conn, cfg, "standard", "day", NOW, NOW)
    assert acct == "m" and [r["account"] for r in rows] == ["m", "p"]


def test_fits_only_where_it_fits(db):
    conn, cfg, ids = setup(db)
    tickets.set_size(conn, cfg, "standard", 0.9, ids["admin"])   # 90% of Pro: over max_sold_pct (80) in a whole week
    acct, rows = placement.pick_tier(conn, cfg, "standard", "month", NOW, NOW)
    assert acct == "m"
    assert not next(r for r in rows if r["account"] == "p")["fits"]
    fresh(conn, "m", NOW + 30, 78.0)   # m full
    assert tickets.sold_out(conn, cfg, "standard", "month", NOW + 60)
    n = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    with pytest.raises(tickets.CapacityError):
        grant(conn, cfg, ids, length="month", now=NOW + 60)
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == n


def test_unknown_plan_is_skipped_not_sold_out(db):
    conn, cfg, ids = setup(db)
    seed_oauth(conn, account="u")
    conn.execute("INSERT INTO account_plans(observed_at, organization_type, rate_limit_tier, account) "
                 "VALUES(?, 'claude_max', 'default_claude_max', 'u')", (NOW - 100,))
    fresh(conn, "u", NOW, 0.0)   # the most room of all
    assert plans.plan(conn, "u") is None
    acct, rows = placement.pick_tier(conn, cfg, "standard", "week", NOW, NOW)
    assert acct == "m"
    u = next(r for r in rows if r["account"] == "u")
    assert not u["fits"] and u["reason"] == tickets.PLAN_UNKNOWN and u["share_pct"] is None
    table = tickets.price_table(conn, cfg, NOW, "EUR")
    std = next(t for t in table["tiers"] if t["tier"] == "standard")
    assert not std["lengths"]["week"]["sold_out"] and not std["lengths"]["week"]["unavailable"]


def test_plan_changed_stops_selling(db):
    conn, cfg, ids = setup(db)
    conn.execute("INSERT INTO account_plans(observed_at, organization_type, rate_limit_tier, account) "
                 "VALUES(?, 'claude_max', 'default_claude_max_20x', 'm')", (NOW - 200,))
    plans.acknowledge(conn, "m", None)
    conn.execute("INSERT INTO account_plans(observed_at, organization_type, rate_limit_tier, account) "
                 "VALUES(?, 'claude_max', 'default_claude_max_5x', 'm')", (NOW - 100,))
    acct, rows = placement.pick_tier(conn, cfg, "standard", "week", NOW, NOW)
    assert acct == "p"
    assert next(r for r in rows if r["account"] == "m")["reason"] == placement.PLAN_CHANGED
    plans.acknowledge(conn, "m", None)
    assert placement.pick_tier(conn, cfg, "standard", "week", NOW, NOW)[0] == "m"


def test_admin_override_account(db):
    conn, cfg, ids = setup(db)
    t = grant(conn, cfg, ids, account="p")
    assert t["account"] == "p" and t["share_pct"] == 25.0
    fresh(conn, "p", NOW + 30, 78.0)   # p full
    n = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    with pytest.raises(tickets.CapacityError) as e:
        grant(conn, cfg, ids, account="p", now=NOW + 60, start_now=True)
    assert "points free" in str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == n


def test_preview_lists_accounts(db):
    conn, cfg, ids = setup(db)
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "standard", "week", "EUR", NOW)
    assert p["account"] == "m" and p["share_pct"] == 1.25 and p["available"]
    by = {r["account"]: r for r in p["accounts"]}
    assert set(by) == {"m", "p"}
    assert by["m"]["fits"] and by["p"]["fits"]
    assert by["m"]["free_pro_weeks"] == pytest.approx(2.0) and by["p"]["free_pro_weeks"] == pytest.approx(0.6)


def test_over_week_waiver_judged_on_the_best_account(db):
    conn, cfg, ids = setup(db)
    fresh(conn, "p", NOW + 30, 78.0)
    fresh(conn, "m", NOW + 30, 78.0)   # this week full on both
    alice = user(conn, ids["alice"])
    p = tickets.preview(conn, cfg, alice, "standard", "week", "EUR", NOW + 60)
    assert p["short_week"] and not p["available"] and p["account"] is None
    p = tickets.preview(conn, cfg, alice, "standard", "week", "EUR", NOW + 60, over_week=True)
    assert p["available"] and p["account"] == "m" and p["over_week"]   # no free room on either: by slug
    t = grant(conn, cfg, ids, now=NOW + 60, over_week=True)
    assert t["account"] == "m" and t["share_pct"] == 1.25


def test_queued_pass_placed_independently(db):
    conn, cfg, ids = setup(db)
    first = grant(conn, cfg, ids)
    assert first["account"] == "m"
    fresh(conn, "m", NOW + 30, 78.0)   # m full
    queued = grant(conn, cfg, ids, now=NOW + 60)
    assert queued["starts_at"] == first["ends_at"] and queued["account"] == "p"


async def test_paid_order_autoplaces(db, monkeypatch):
    from tests.test_payments import MID, FakeZP
    now = int(time.time())
    conn, cfg, ids = setup(db, t=now)
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 156250, ids["admin"], now=now - 3600)
    cfg.zarinpal = ZarinpalConfig()
    cfg.listener.home_url = "https://rahkar.pro"
    cfg.listener.dashboard_url = "https://dash.rahkar.pro"
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    zp = FakeZP()
    monkeypatch.setattr(zarinpal, "request", zp.request)
    monkeypatch.setattr(zarinpal, "verify", zp.verify)
    r = await payments.start(conn, cfg, user(conn, ids["alice"]), "lite", "week", "alice@example.com", terms_version="v1")
    p = await payments.callback(conn, cfg, payments.get(conn, r["payment_id"])["authority"], "OK")
    assert p["status"] == "paid"
    t = conn.execute("SELECT t.* FROM tickets t JOIN orders o ON o.ticket_id=t.id WHERE o.id=?", (p["order_id"],)).fetchone()
    assert t["account"] == "m"


def test_trial_placed(db):
    conn, cfg, ids = setup(db)
    trials.set_settings(conn, cfg, ids["admin"], {"enabled": True})
    alice = user(conn, ids["alice"])
    trials.claim(conn, cfg, alice, NOW)
    t = trials.start(conn, cfg, alice["id"], NOW + 600)
    assert t["account"] == "m"
    assert t["share_pct"] == tickets.effective_share(conn, cfg, "lite", "m")
    assert t["size"] == tickets.tier_size(conn, cfg, "lite")


async def test_signup_assigns_best_account(cfg, db, monkeypatch):
    from claude_proxy.gateway import Gateway
    from tests.test_signup import DASH, PK, Clerk, app, signed_in
    now = int(time.time())
    conn = db[1]
    for a, plan in (("p", "pro"), ("m", "max_20x")):
        link(conn, a, plan)
        fresh(conn, a, now, room_util(60 if a == "p" else 10))
    cfg.tickets.enabled = True
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_secret")
    cfg.listener.public_url, cfg.listener.dashboard_url = "https://gw.test", DASH
    cfg.signup.clerk_publishable_key, cfg.signup.enabled = PK, True
    gw = Gateway(cfg, conn, http=httpx.AsyncClient(transport=httpx.MockTransport(Clerk().handler)))
    async with app(gw) as c:
        u = await signed_in(c)
    assert conn.execute("SELECT account FROM users WHERE id=?", (u["id"],)).fetchone()[0] == "m"


async def test_linking_an_account_assigns_unassigned_users(cfg, db):
    """A fresh install: `init` makes the admin before any account is linked; linking one assigns them."""
    from tests.test_login import token_endpoint
    conn = db[1]
    uid, _ = create_user(conn, "admin", role="admin")
    assert conn.execute("SELECT account FROM users WHERE id=?", (uid,)).fetchone()[0] is None
    login.start(conn, cfg, "main")
    state = login._pending(conn, "main")["state"]
    async with token_endpoint([]) as http:
        await login.finish(conn, cfg, http, f"CODE#{state}", "main")
    assert conn.execute("SELECT account FROM users WHERE id=?", (uid,)).fetchone()[0] == "main"
    assert conn.execute("SELECT 1 FROM audit_log WHERE action='user_account_set'").fetchone()


def test_no_sell_account_constant():
    assert not hasattr(tickets, "SELL_ACCOUNT")
    for name in ("placement.py", "routing.py", "tickets.py", "limits.py", "capacity.py", "app.py"):
        text = (SRC / name).read_text()
        assert "'default'" not in text and '"default"' not in text, name
