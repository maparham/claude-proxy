"""Moving a pass between subscription accounts (multi-subscription design 2026-11-24, §4): the pass keeps its size in
Pro weeks and what it used, both converted into the new account's points, so a move never refills or shrinks it; its
borrows and bonuses go with it. The target's capacity is checked again and the move is all-or-nothing.

`m` is on Claude Max 20x and `p` on Claude Pro, so one point of `m` is 20 points of `p`. Anthropic's reports and the
observed rates are stubbed per account, as the measuring itself is tested elsewhere."""
import pytest

from claude_proxy import capacity, limits, plans, quota, routing, tickets
from claude_proxy.db import create_user
from tests.tickets_helpers import DAY, NOW, RESET7, WEEK_START, link, tcfg, user

RATES = {"m": 20000.0, "p": 1000.0}   # weighted tokens per point: a Max 20x point is 20 Pro points


@pytest.fixture
def env(db, monkeypatch):
    conn = db[1]
    cfg = tcfg()
    for a in ("m", "p", "x"):
        conn.execute("INSERT INTO settings(key, value) VALUES(?, '4102444800')", (f"subscription_ends_at:{a}",))
    link(conn, "m", "max_20x")
    link(conn, "p", "pro")
    for a in ("m", "p"):   # _ticket_7d reads the newest weekly snapshot for its "no live data" mark
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at, account) "
                     "VALUES(?,?,?,?,?,?)", (NOW - 60, "header", "7d", 0.0, RESET7, a))
    tickets.seed_prices(conn, cfg, now=NOW - DAY)
    tickets.seed_sizes(conn, cfg, now=NOW - DAY)
    admin, _ = create_user(conn, "admin", role="admin")
    alice, _ = create_user(conn, "alice")
    tickets.set_rate(conn, cfg, "EUR", 0.92, admin, now=NOW - 3600)
    att: dict = {}

    def fake_att(conn_, pricing, bucket, now, stale_after_s=1800, *, account):
        out = {"bucket": bucket, "utilization_pct": 0.0, "observed_at": NOW - 60, "stale": False, "shares": {},
               "ticket_shares": {}, "unattributed": 0.0, "history": [],
               "resets_at": RESET7 if bucket == "7d" else NOW + 3600,
               "window_start": WEEK_START + 60 if bucket == "7d" else NOW - 3600}
        out["counted_from"] = out["window_start"]
        out.update(att.get((account, bucket), {}))
        return out

    monkeypatch.setattr(quota, "attribution_cached", fake_att)
    monkeypatch.setattr(quota, "observed_rate", lambda conn_, pricing, bucket, now, days=7, *, account: RATES.get(account))
    return conn, cfg, {"admin": admin, "alice": alice}, att


def grant_on_m(conn, cfg, ids, starts_at=NOW - DAY, length="week", days=7):
    """A Standard pass of alice's on `m` (the most free Pro weeks), then set to run from `starts_at`."""
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "standard", length, "EUR", terms_version="v1",
                      now=NOW)
    assert t["account"] == "m" and t["share_pct"] == 1.25
    conn.execute("UPDATE tickets SET starts_at=?, ends_at=? WHERE id=?", (starts_at, starts_at + days * DAY, t["id"]))
    return tickets.get(conn, t["id"])


def use(conn, uid, ticket_id, tokens, t, account="m"):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens, output_tokens, "
                 "ticket_id, account) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid, t - 1, t, "POST", "/v1/messages", "anthropic",
                                                                     "claude-sonnet-5", int(round(tokens)), 0, ticket_id, account))


def ratio(st):
    return st.current / st.limit


def test_move_keeps_used_percent(env):
    conn, cfg, ids, att = env
    t = grant_on_m(conn, cfg, ids, starts_at=WEEK_START - 2 * DAY)   # runs across the reset: 5 days of this week
    # It borrowed from this week in the last: 0.15 points used there against 0.1 of its own, so 0.05 of debt.
    use(conn, ids["alice"], t["id"], 0.15 * RATES["m"], WEEK_START - 3600)
    conn.execute("INSERT INTO pass_borrows(ticket_id, week_end, own_pts, rate, updated_at) VALUES(?,?,?,?,?)",
                 (t["id"], WEEK_START, 0.1, RATES["m"], WEEK_START - 60))
    debt = capacity._debt(conn, cfg, t, WEEK_START, 100.0, NOW)
    assert debt == pytest.approx(0.05)
    limit = 1.25 * 5 / 7 - debt
    use(conn, ids["alice"], t["id"], 0.4 * limit * RATES["m"], NOW - 600)
    att[("m", "5h")] = {"ticket_shares": {t["id"]: 0.3 * 1.25}}
    w0, h0 = limits._ticket_7d(conn, cfg, t, NOW), limits._ticket_5h(conn, cfg, t, NOW)
    assert ratio(w0) == pytest.approx(0.4, abs=1e-4) and ratio(h0) == pytest.approx(0.3)

    out = tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    t2 = tickets.get(conn, t["id"])
    assert (out["from"], out["to"], t2["account"]) == ("m", "p", "p")
    assert t2["share_pct"] == out["share_pct"] == 25.0 and t2["share_5h_pct"] == 25.0
    assert out["carry_7d_pts"] == pytest.approx(w0.current * 20)
    assert out["carry_5h_pts"] == pytest.approx(h0.current * 20)
    w1, h1 = limits._ticket_7d(conn, cfg, t2, NOW + 10), limits._ticket_5h(conn, cfg, t2, NOW + 10)
    assert ratio(w1) == pytest.approx(ratio(w0), abs=1e-3)
    assert ratio(h1) == pytest.approx(ratio(h0), abs=1e-3)
    assert capacity._debt(conn, cfg, t2, WEEK_START, 100.0, NOW + 10) == pytest.approx(debt * 20)
    assert routing.account_for(conn, ids["alice"], NOW + 10) == "p"
    mv = conn.execute("SELECT from_account, to_account, from_share_pct, moved_by FROM ticket_moves WHERE ticket_id=?",
                      (t["id"],)).fetchall()
    assert [tuple(r) for r in mv] == [("m", "p", 1.25, ids["admin"])]
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='ticket_move'").fetchone()[0] == 1
    # Use on the new account counts from the move on, at the new account's rate.
    use(conn, ids["alice"], t["id"], 1.0 * RATES["p"], NOW + 20, account="p")
    assert limits._ticket_7d(conn, cfg, t2, NOW + 30).current == pytest.approx(w1.current + 1.0)


def test_move_rechecks_capacity(env):
    conn, cfg, ids, att = env
    t = grant_on_m(conn, cfg, ids)
    att[("p", "7d")] = {"utilization_pct": 78.0}   # p full
    with pytest.raises(tickets.CapacityError):
        tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    assert tickets.get(conn, t["id"])["account"] == "m"
    assert conn.execute("SELECT COUNT(*) FROM ticket_moves").fetchone()[0] == 0


def test_move_refusals(env):
    conn, cfg, ids, att = env
    t = grant_on_m(conn, cfg, ids)
    with pytest.raises(tickets.TicketError, match="already"):
        tickets.move(conn, cfg, ids["admin"], t["id"], "m", now=NOW)
    with pytest.raises(tickets.TicketError, match="No subscription account"):
        tickets.move(conn, cfg, ids["admin"], t["id"], "x", now=NOW)
    for tier in ("default_claude_pro", "default_claude_max_5x"):   # p's plan changed and nobody acknowledged it yet
        conn.execute("INSERT INTO account_plans(observed_at, organization_type, rate_limit_tier, account) "
                     "VALUES(?, 'claude_pro', ?, 'p')", (NOW - 100 if tier.endswith("pro") else NOW - 50, tier))
        if tier.endswith("pro"):
            plans.acknowledge(conn, "p", None)
    assert plans.sells(conn, "p") == "plan_changed"
    with pytest.raises(tickets.TicketError, match="plan changed"):
        tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    plans.acknowledge(conn, "p", None)
    conn.execute("UPDATE tickets SET ends_at=? WHERE id=?", (NOW - 1, t["id"]))
    with pytest.raises(tickets.TicketError, match="ended"):
        tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    conn.execute("UPDATE tickets SET ends_at=?, cancelled_at=? WHERE id=?", (NOW + DAY, NOW - 1, t["id"]))
    with pytest.raises(tickets.TicketError, match="cancelled"):
        tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM ticket_moves").fetchone()[0] == 0


def test_move_queued_no_carry(env):
    conn, cfg, ids, att = env
    t = grant_on_m(conn, cfg, ids, starts_at=NOW + DAY)
    out = tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    assert out["carry_7d_pts"] == 0 and out["carry_5h_pts"] == 0
    t2 = tickets.get(conn, t["id"])
    assert (t2["account"], t2["share_pct"], t2["share_5h_pct"]) == ("p", 25.0, 25.0)


def test_move_frees_source_and_reserves_target(env):
    conn, cfg, ids, att = env
    t = grant_on_m(conn, cfg, ids)   # NOW - 1 day to NOW + 6 days: 4 days of this week (to RESET7)
    used = 0.2
    use(conn, ids["alice"], t["id"], used * RATES["m"], NOW - 600)
    m0, p0 = capacity.context(conn, cfg, "m", NOW).reserved, capacity.context(conn, cfg, "p", NOW).reserved
    remaining = 1.25 * 4 / 7 - used
    assert m0 == pytest.approx(remaining)
    tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    m1, p1 = capacity.context(conn, cfg, "m", NOW).reserved, capacity.context(conn, cfg, "p", NOW).reserved
    assert m0 - m1 == pytest.approx(remaining)
    assert p1 - p0 == pytest.approx(remaining * 20)
    # The admin's per-pass view on p says the same.
    assert capacity.week_use(conn, cfg, "p", NOW)["passes"][t["id"]]["reserved"] == pytest.approx(remaining * 20)


def test_reserved_5h_counts_the_carry(env):
    conn, cfg, ids, att = env
    t = grant_on_m(conn, cfg, ids)
    att[("m", "5h")] = {"ticket_shares": {t["id"]: 0.5}}
    out = tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    assert out["carry_5h_pts"] == pytest.approx(10.0)
    a5 = quota.attribution_cached(conn, cfg.pricing, "5h", NOW, account="p")
    assert capacity.reserved_5h(conn, "p", a5, 100.0, NOW) == pytest.approx(25.0 - 10.0)


def test_dry_run_changes_nothing(env):
    conn, cfg, ids, att = env
    t = grant_on_m(conn, cfg, ids)
    use(conn, ids["alice"], t["id"], 0.1 * RATES["m"], NOW - 600)
    conn.execute("INSERT INTO pass_borrows(ticket_id, week_end, own_pts, rate, updated_at) VALUES(?,?,?,?,?)",
                 (t["id"], RESET7, 0.1, RATES["m"], NOW - 60))
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, starts_at=NOW - 60, ends_at=NOW + DAY, now=NOW - 60)

    def state():
        return ([tuple(r) for r in conn.execute("SELECT * FROM tickets")],
                [tuple(r) for r in conn.execute("SELECT * FROM ticket_bonuses")],
                [tuple(r) for r in conn.execute("SELECT * FROM pass_borrows")],
                [tuple(r) for r in conn.execute("SELECT * FROM ticket_moves")],
                [tuple(r) for r in conn.execute("SELECT * FROM audit_log")])

    before = state()
    preview = tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW, dry_run=True)
    assert state() == before
    real = tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    assert preview == real


def test_bonus_scaled(env):
    conn, cfg, ids, att = env
    t = grant_on_m(conn, cfg, ids)
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, starts_at=NOW - 60, ends_at=NOW + DAY, now=NOW - 60)
    tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    b = tickets.active_bonuses(conn, t["id"], NOW)
    assert [x["share_pct"] for x in b] == [pytest.approx(40.0)]
    assert tickets.bonus_share(conn, t["id"], NOW) == pytest.approx(40.0)


def test_move_checks_the_bonuses_with_the_pass(env):
    conn, cfg, ids, att = env
    t = grant_on_m(conn, cfg, ids)   # 4 days of this week: 25 × 4/7 = 14.3 points of p
    b = tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=2, starts_at=NOW - 60, ends_at=NOW + DAY,
                          now=NOW - 60)["bonus"]   # 40% of p for a day: 5.7 points more
    att[("p", "7d")] = {"utilization_pct": 59.0}   # 16 points free on p: the pass alone fits, with its bonus it doesn't
    with pytest.raises(tickets.CapacityError, match="points free"):
        tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    assert tickets.get(conn, t["id"])["account"] == "m"
    assert tickets.get(conn, t["id"])["share_pct"] == 1.25
    assert tickets.bonuses(conn, t["id"])[0]["share_pct"] == 2
    assert conn.execute("SELECT COUNT(*) FROM ticket_moves").fetchone()[0] == 0
    conn.execute("UPDATE ticket_bonuses SET cancelled_at=? WHERE id=?", (NOW - 30, b["id"]))
    assert tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW, dry_run=True)["to"] == "p"   # alone it fits
    conn.execute("UPDATE ticket_bonuses SET cancelled_at=NULL WHERE id=?", (b["id"],))
    att[("p", "7d")] = {"utilization_pct": 50.0}   # 25 points free: room for both
    tickets.move(conn, cfg, ids["admin"], t["id"], "p", now=NOW)
    assert tickets.get(conn, t["id"])["account"] == "p"
    assert tickets.bonuses(conn, t["id"])[0]["share_pct"] == pytest.approx(40.0)
