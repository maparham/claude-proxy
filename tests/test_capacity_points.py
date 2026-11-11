"""Passes are sold in points of the week (spec 2026-11-04-pass-capacity-in-points)."""
import pytest

from claude_proxy import tickets
from claude_proxy.config import Config, ConfigError, TicketsConfig
from tests.tickets_helpers import seeded


def test_new_settings_have_their_defaults():
    t = TicketsConfig()
    assert (t.max_sold_5h_pct, t.sell_margin_pct) == (100, 5)


@pytest.mark.parametrize("key, value, ok", [
    ("max_sold_5h_pct", "150", True), ("max_sold_5h_pct", "0", False), ("max_sold_5h_pct", "1001", False),
    ("max_sold_5h_pct", "true", False), ("sell_margin_pct", "0", True), ("sell_margin_pct", "100", True),
    ("sell_margin_pct", "-1", False), ("sell_margin_pct", "101", False)])
def test_new_settings_are_validated(tmp_path, key, value, ok):
    p = tmp_path / "c.toml"
    p.write_text(f"[tickets]\n{key} = {value}\n")
    if ok:
        assert getattr(Config.load(str(p)).tickets, key) == float(value)
    else:
        with pytest.raises(ConfigError, match=key):
            Config.load(str(p))


def test_a_tier_5h_share_over_the_5h_ceiling_is_refused(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[tickets]\nmax_sold_5h_pct = 30\n[tickets.tiers.big]\nlabel = \"Big\"\nshare_pct = 20\nshare_5h_pct = 40\ndefault_usd = { day = 3, week = 8, month = 20 }\n")
    with pytest.raises(ConfigError, match="max_sold_5h_pct"):
        Config.load(str(p))
    p.write_text("[tickets]\nmax_sold_5h_pct = 30\n[tickets.tiers.big]\nlabel = \"Big\"\nshare_pct = 40\ndefault_usd = { day = 3, week = 8, month = 20 }\n")
    with pytest.raises(ConfigError, match="max_sold_5h_pct"):   # no 5-hour share: the weekly one counts
        Config.load(str(p))


def test_set_share_5h_refuses_a_share_over_the_5h_ceiling(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_5h_pct = 50
    assert tickets.set_share_5h(conn, cfg, "lite", 50, ids["admin"]) == 50
    with pytest.raises(tickets.TicketError, match="max_sold_5h_pct"):
        tickets.set_share_5h(conn, cfg, "lite", 51, ids["admin"])


from claude_proxy import capacity
from tests.tickets_helpers import DAY, NOW

WEEK = 7 * DAY
W = (NOW - 4 * DAY, NOW + 3 * DAY)   # the week the tests' 7-day snapshots describe


def test_pieces_split_a_period_by_week_and_project_forward():
    assert capacity.pieces(W, 20, NOW, NOW + DAY) == {W[0]: pytest.approx(20 / 7)}
    p = capacity.pieces(W, 40, NOW + 2 * DAY, NOW + 9 * DAY)          # a week pass from Thursday
    assert p == {W[0]: pytest.approx(40 / 7), W[1]: pytest.approx(240 / 7)}
    assert capacity.pieces(W, 5, NOW + 3 * DAY, NOW + 4 * DAY) == {W[1]: pytest.approx(5 / 7)}
    assert capacity.pieces(W, 5, NOW - 10 * DAY, NOW - 9 * DAY) == {W[0] - WEEK: pytest.approx(5 / 7)}
    assert capacity.pieces(W, 5, NOW, NOW) == {}


def test_fresh_needs_a_young_snapshot_with_a_future_reset():
    att = {"utilization_pct": 10.0, "stale": False, "resets_at": NOW + 10}
    assert capacity.fresh(att, NOW)
    assert not capacity.fresh(att | {"stale": True}, NOW)
    assert not capacity.fresh(att | {"resets_at": NOW - 1}, NOW)
    assert not capacity.fresh(att | {"utilization_pct": None}, NOW)


def test_the_5h_sum_reads_5h_shares_and_its_own_ceiling(db):
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    tickets.set_share_5h(conn, cfg, "lite", 30, ids["admin"])
    from tests.test_tickets_capacity import grant
    t = grant(conn, cfg, ids, tier="lite", length="day")          # weekly 5, 5-hour 30
    assert tickets.sold_at(conn, 1, NOW) == 30
    conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, share_5h_pct, extra_days, starts_at, ends_at, granted_at) "
                 "VALUES(?,?,?,?,?,?,?)", (t["id"], 10, None, 0, NOW, NOW + 3600, NOW))
    assert tickets.sold_at(conn, 1, NOW) == 40                     # a bonus without a 5-hour share counts its share
    cfg.tickets.max_sold_5h_pct = 60
    tickets.check_capacity(conn, cfg, 1, 20, NOW, NOW + DAY)       # 40 + 20 = 60: fits
    with pytest.raises(tickets.CapacityError, match="5-hour shares would reach 61% .* over max_sold_5h_pct"):
        tickets.check_capacity(conn, cfg, 1, 21, NOW, NOW + DAY)


from claude_proxy import quota
from tests.tickets_helpers import PTS, WEEK_START, live, report, use


def test_report_keeps_the_rate_and_attributes_everything(db):
    conn, cfg, ids = seeded(db)
    report(conn, ids, 1, t=WEEK_START + 60)
    report(conn, ids, 42, t=NOW - 60)
    att = quota.attribution(conn, cfg.pricing, "7d", now=NOW, stale_after_s=cfg.quota.stale_after_s)
    assert (att["utilization_pct"], att["unattributed"], att["stale"]) == (42, pytest.approx(0), False)
    assert quota.observed_rate(conn, cfg.pricing, "7d", NOW) == pytest.approx(PTS)
    with pytest.raises(AssertionError, match="not after"):
        report(conn, ids, 43, t=NOW - 3 * DAY)


from tests.test_tickets_capacity import add_user, grant
from tests.tickets_helpers import user


@pytest.fixture
def acct(db):
    """Shares lite 20 / standard 40 as on the live gateway, a ceiling of 100, a rate of PTS, the account at 1 since the
    week began. The snapshot is 4 days old at NOW: a test that acts at NOW first calls report() or live() for then."""
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 100
    tickets.set_share(conn, cfg, "lite", 20, ids["admin"])
    tickets.set_share(conn, cfg, "standard", 40, ids["admin"])
    report(conn, ids, 1, t=WEEK_START + 60)
    return conn, cfg, ids


def three_day_passes(conn, cfg, ids, used_each):
    """Three users' lite day passes from NOW - 1200, each having used `used_each` points."""
    live(conn, NOW - 1200, 1)
    out = []
    for n in range(3):
        add_user(conn, ids, f"u{n}")
        out.append(grant(conn, cfg, ids, who=f"u{n}", tier="lite", length="day", now=NOW - 1200))
        use(conn, ids[f"u{n}"], out[-1]["id"], used_each, NOW - 1000)
    return out


def test_free_room_sells_what_anthropic_reports_unused(acct):           # spec test 1
    conn, cfg, ids = acct
    three_day_passes(conn, cfg, ids, 3)                                  # each past its 2.86 piece: reserved 0
    report(conn, ids, 42)
    ctx = capacity.context(conn, cfg, 1, NOW)
    assert (ctx.used, ctx.reserved, ctx.room) == (42, pytest.approx(0), pytest.approx(53))
    t = grant(conn, cfg, ids, tier="standard", length="day")
    assert t["share_pct"] == 40 and tickets.sold_at(conn, 1, NOW) == 100


def test_a_full_week_refuses_with_the_points_named(acct):              # spec test 2
    conn, cfg, ids = acct
    three_day_passes(conn, cfg, ids, 3)
    report(conn, ids, 93)
    with pytest.raises(tickets.CapacityError, match=r"This week has 2\.0 points free and the pass needs 5\.7"):
        grant(conn, cfg, ids, tier="standard", length="day")
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 3


def test_reserved_holds_back_what_running_passes_may_still_draw(acct):
    conn, cfg, ids = acct
    add_user(conn, ids, "bob")
    live(conn, NOW - 1200, 1)
    b = grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW - 1200)
    use(conn, ids["bob"], b["id"], 1, NOW - 1000)
    report(conn, ids, 90)
    ctx = capacity.context(conn, cfg, 1, NOW)
    assert ctx.reserved == pytest.approx(40 / 7 - 1) and ctx.room == pytest.approx(100 - 90 - (40 / 7 - 1) - 5)
    assert tickets.sold_out(conn, cfg, "lite", "day", NOW)              # needs 2.86, 0.29 left


def test_later_weeks_are_capped_by_promised_pieces(acct):              # spec test 3
    conn, cfg, ids = acct
    cfg.tickets.max_sold_pct, cfg.tickets.max_sold_5h_pct = 60, 200
    report(conn, ids, 1)
    for n in ("a", "b"):
        add_user(conn, ids, n)
        grant(conn, cfg, ids, who=n, tier="standard", length="week")    # 17.1 here, 22.9 next week, each
    add_user(conn, ids, "c")
    with pytest.raises(tickets.CapacityError, match=r"The week of .* would hold 68\.6% .* over max_sold_pct"):
        grant(conn, cfg, ids, who="c", tier="standard", length="week")  # here 51.4 ≤ 60 − 1 − 5; next week 68.6 > 60
    assert capacity.context(conn, cfg, 1, NOW).sold[W[1]] == pytest.approx(2 * 40 * 4 / 7)


def test_a_week_pass_from_thursday_charges_each_week_its_piece(acct):  # spec test 4
    conn, cfg, ids = acct
    cfg.tickets.max_sold_pct = 60
    add_user(conn, ids, "a")
    thu = NOW + 2 * DAY
    live(conn, thu - 3600, 1)
    grant(conn, cfg, ids, who="a", tier="standard", length="month", now=thu - 3600, confirm_stale_rate=True)
    live(conn, thu, 1)
    assert capacity.context(conn, cfg, 1, thu).sold[W[1]] == pytest.approx(40)
    with pytest.raises(tickets.CapacityError, match=r"The week of .* would hold 74\.3%"):   # 40 + 240/7 next week
        capacity.check(conn, cfg, 1, 40, 40, thu, thu + 7 * DAY, thu)
    capacity.check(conn, cfg, 1, 20, 20, thu, thu + DAY, thu)          # this week only: fits


def test_a_pass_starting_next_week_needs_no_room_this_week(acct):      # spec test 5
    conn, cfg, ids = acct
    report(conn, ids, 99)
    capacity.check(conn, cfg, 1, 40, 40, NOW + 3 * DAY, NOW + 4 * DAY, NOW)
    with pytest.raises(tickets.CapacityError, match="points free"):
        capacity.check(conn, cfg, 1, 40, 40, NOW, NOW + DAY, NOW)


def test_unused_points_of_an_ended_pass_are_sold_again(acct):          # spec test 6, recycling
    conn, cfg, ids = acct
    add_user(conn, ids, "a")
    live(conn, NOW - 3 * DAY, 1)
    t = grant(conn, cfg, ids, who="a", tier="standard", length="day", now=NOW - 3 * DAY)
    use(conn, ids["a"], t["id"], 1, NOW - 3 * DAY + 3600)                # 1 of its 5.71
    report(conn, ids, 2)
    ctx = capacity.context(conn, cfg, 1, NOW)
    assert ctx.reserved == 0 and ctx.room == pytest.approx(100 - 2 - 5)
    assert ctx.sold[W[0]] == pytest.approx(40 / 7)                      # sold, shown on the panel, not a gate


def test_a_pass_sold_now_leaves_the_room_by_its_whole_piece(acct):
    conn, cfg, ids = acct
    report(conn, ids, 1)
    before = capacity.context(conn, cfg, 1, NOW).room
    grant(conn, cfg, ids, tier="standard", length="day")
    assert capacity.context(conn, cfg, 1, NOW).room == pytest.approx(before - 40 / 7)


def test_replacing_used_up_passes_frees_their_reserved_points(acct):       # Review Focus 3
    conn, cfg, ids = acct
    cfg.tickets.max_sold_5h_pct = 40
    live(conn, NOW - 3600, 1)
    a = grant(conn, cfg, ids, tier="standard", length="day", now=NOW - 3600)
    use(conn, ids["alice"], a["id"], 6, NOW - 1000)                       # used up: past its whole piece (5.71)
    report(conn, ids, 7)
    t = grant(conn, cfg, ids, tier="standard", length="day", start_now=True)   # replaces it: 5-hour 40 fits 40
    assert tickets.get(conn, a["id"])["used_up_at"] == NOW and t["starts_at"] == NOW


@pytest.mark.parametrize("case", ["stale", "none", "reset_passed"])
def test_no_fresh_report_sells_nothing(db, case):                       # spec test 7
    conn, cfg, ids = seeded(db)
    if case == "stale":
        live(conn, NOW - 2000)
    elif case == "reset_passed":
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                     (NOW - 120, "header", "7d", 10, NOW - 10))
    with pytest.raises(tickets.CapacityError, match="No fresh usage report"):
        grant(conn, cfg, ids)
    assert tickets.sold_out(conn, cfg, "lite", "day", NOW)
    live(conn, NOW)
    assert not tickets.sold_out(conn, cfg, "lite", "day", NOW)


def test_old_week_snapshot_after_the_reset_sells_nothing(db):           # Review Focus 1
    conn, cfg, ids = seeded(db)
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (NOW - 60, "header", "7d", 80, NOW - 30))                  # fresh by age, from the week that just ended
    assert capacity.context(conn, cfg, 1, NOW).fresh is False
    with pytest.raises(tickets.CapacityError, match="No fresh usage report"):
        grant(conn, cfg, ids)


def test_usage_jump_between_preview_and_grant_refuses_cleanly(acct):    # Review Focus 2
    conn, cfg, ids = acct
    report(conn, ids, 1, t=NOW - 120)
    assert tickets.preview(conn, cfg, user(conn, ids["alice"]), "standard", "day", "EUR", NOW)["available"]
    report(conn, ids, 97, t=NOW - 30)
    with pytest.raises(tickets.CapacityError, match="points free"):
        grant(conn, cfg, ids, tier="standard", length="day")
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_the_5h_ceiling_still_bounds_passes_running_at_once(acct):      # spec test 8
    conn, cfg, ids = acct
    report(conn, ids, 1)
    for n in range(4):
        add_user(conn, ids, f"u{n}")
        grant(conn, cfg, ids, who=f"u{n}", tier="lite", length="day")    # 4 × 20 = 80 of 5-hour share, 11.4 reserved
    add_user(conn, ids, "v")
    with pytest.raises(tickets.CapacityError, match="5-hour shares would reach 120%"):
        grant(conn, cfg, ids, who="v", tier="standard", length="day")
    cfg.tickets.max_sold_5h_pct = 150
    assert grant(conn, cfg, ids, who="v", tier="standard", length="day")["share_pct"] == 40


def test_outside_use_scales_reserved_and_check_1_but_not_later_weeks(acct):  # spec test 9
    conn, cfg, ids = acct
    live(conn, NOW, 11)                                                  # 10 points nobody at the gateway used
    grant(conn, cfg, ids, tier="standard", length="week")
    ctx = capacity.context(conn, cfg, 1, NOW)
    assert ctx.scale == pytest.approx(90)
    assert ctx.reserved == pytest.approx(40 * 3 / 7 * 0.9)
    assert ctx.sold[W[1]] == pytest.approx(40 * 4 / 7)


def test_without_a_rate_reserved_uses_the_attributed_points(acct):          # spec test 10
    conn, cfg, ids = acct
    live(conn, NOW - 1200, 1)
    t = grant(conn, cfg, ids, tier="standard", length="day", now=NOW - 1200)
    att = {"utilization_pct": 3.0, "stale": False, "resets_at": W[1], "ticket_shares": {t["id"]: 2.0}, "unattributed": 0.0}
    assert capacity.reserved(conn, cfg, 1, W, att, None, 100.0, NOW) == pytest.approx(40 / 7 - 2)


def test_a_bonus_is_judged_as_one_change(acct):                         # spec test 11
    conn, cfg, ids = acct
    report(conn, ids, 1, t=NOW - 120)
    t = grant(conn, cfg, ids, tier="standard", length="day")
    report(conn, ids, 80, t=NOW - 30)                                    # room 100 − 80 − 5.71 − 5 = 9.3
    with pytest.raises(tickets.CapacityError, match="points free"):     # share over 2 days 11.4 + an extra day 5.7
        tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=40, extra_days=1, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM ticket_bonuses").fetchone()[0] == 0
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=40, now=NOW, ends_at=NOW + DAY)   # 5.7 alone fits


def test_hand_set_weekly_shares_are_not_reserved(acct):                 # spec test 12
    conn, cfg, ids = acct
    report(conn, ids, 1)
    add_user(conn, ids, "h")
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (ids["h"], "share_7d", "*", "100", "pct"))
    assert capacity.context(conn, cfg, 1, NOW).room == pytest.approx(100 - 1 - 5)


def test_cancel_whose_move_pulls_a_pass_into_a_full_week_keeps_dates(acct):   # spec test 14
    conn, cfg, ids = acct
    report(conn, ids, 1)
    a = grant(conn, cfg, ids, tier="standard", length="week")           # NOW .. NOW + 7d
    q = grant(conn, cfg, ids, tier="standard", length="day")            # queued NOW + 7d: next week
    report(conn, ids, 99, t=NOW + 30)
    r = tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + 60)  # q would move here: 5.7 more reserved, no room
    assert r["dates_kept"] and "points free" in r["reason"]
    assert tickets.get(conn, q["id"])["starts_at"] == a["ends_at"]
    assert tickets.get(conn, a["id"])["cancelled_at"] == NOW + 60


def test_cancel_whose_move_stays_inside_the_week_adds_nothing(acct):
    conn, cfg, ids = acct
    report(conn, ids, 1)
    a = grant(conn, cfg, ids, tier="standard", length="day")            # NOW .. NOW + 1d
    q = grant(conn, cfg, ids, tier="standard", length="day")            # queued NOW + 1d, still this week
    report(conn, ids, 99, t=NOW + 30)
    r = tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + 60)
    assert not r["dates_kept"] and tickets.get(conn, q["id"])["starts_at"] == NOW + 60


def test_lowering_max_sold_pct_refuses_and_cancels_nothing(acct):       # spec test 15
    conn, cfg, ids = acct
    report(conn, ids, 1)
    grant(conn, cfg, ids, tier="standard", length="week")
    cfg.tickets.max_sold_pct = 20
    add_user(conn, ids, "b")
    with pytest.raises(tickets.CapacityError, match="points free"):
        grant(conn, cfg, ids, who="b", tier="lite", length="day")
    assert conn.execute("SELECT COUNT(*) FROM tickets WHERE cancelled_at IS NULL").fetchone()[0] == 1


def test_upgrade_is_checked_in_points(acct):
    conn, cfg, ids = acct
    report(conn, ids, 1, t=NOW - 120)
    t = grant(conn, cfg, ids, tier="lite", length="week")               # 20: 8.6 here
    report(conn, ids, 90, t=NOW - 30)                                    # room 100 − 90 − 8.6 − 5 < 0
    q = tickets.upgrade_quote(conn, cfg, t, "standard", "EUR", NOW)
    assert q["sold_out"] and "points free" in q["reason"]
    with pytest.raises(tickets.CapacityError, match="points free"):
        tickets.upgrade(conn, cfg, ids["admin"], user(conn, ids["alice"]), t["id"], "standard", "EUR", now=NOW)


def test_price_table_says_unavailable_not_sold_out_without_a_report(db):
    conn, cfg, ids = seeded(db)
    l = tickets.price_table(conn, cfg, NOW, "EUR")["tiers"][0]["lengths"]["day"]
    assert (l["sold_out"], l["unavailable"]) == (False, True)
    live(conn, NOW)
    l = tickets.price_table(conn, cfg, NOW, "EUR")["tiers"][0]["lengths"]["day"]
    assert (l["sold_out"], l["unavailable"]) == (False, False)


def test_price_table_builds_one_context_per_start(db, monkeypatch):     # Review Focus 4
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    calls = []
    real = capacity.context
    monkeypatch.setattr(capacity, "context", lambda *a, **k: calls.append(1) or real(*a, **k))
    tickets.price_table(conn, cfg, NOW, "EUR", starts_at=NOW + DAY)
    assert len(calls) == 2


def test_an_order_without_a_report_is_refused_as_unavailable(db):
    from claude_proxy import orders
    conn, cfg, ids = seeded(db)
    with pytest.raises(orders.OrderError, match="temporarily unavailable"):
        orders._check_on_sale(conn, cfg, "lite", "day", NOW, ids["alice"], False)


def test_summary_reports_the_week_in_points(acct):                      # spec test 18
    conn, cfg, ids = acct
    add_user(conn, ids, "a")
    live(conn, NOW - 3 * DAY, 1)
    ended = grant(conn, cfg, ids, who="a", tier="standard", length="day", now=NOW - 3 * DAY)
    use(conn, ids["a"], ended["id"], 1, NOW - 3 * DAY + 3600)
    report(conn, ids, 2)
    grant(conn, cfg, ids, tier="lite", length="week")
    add_user(conn, ids, "h")
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (ids["h"], "share_7d", "*", "93", "pct"))
    s = capacity.summary(conn, cfg, 1, NOW)
    w = s["week"]
    assert (w["start"], w["end"], w["fresh"], w["used"], w["margin"], w["max_sold_pct"]) == (W[0], W[1], True, 2, 5, 100)
    assert w["reserved"] == pytest.approx(20 * 3 / 7)
    assert w["free"] == pytest.approx(100 - 2 - 20 * 3 / 7 - 5)
    assert w["sold"] == pytest.approx(40 / 7 + 20 * 3 / 7)
    assert w["returned"] == pytest.approx(40 / 7 - 1)
    assert s["peak_week"] == {"start": W[1], "sold": pytest.approx(20 * 4 / 7)}
    assert s["five_hour"] == {"now": 20, "peak_30d": 20, "max_sold_5h_pct": 100}
    assert s["hand_set_7d_pct"] == 93


def test_summary_without_a_report_is_not_fresh_and_frees_nothing(db):
    conn, cfg, ids = seeded(db)
    w = capacity.summary(conn, cfg, 1, NOW)["week"]
    assert (w["fresh"], w["free"]) == (False, 0)


# ---------- final review: a stale/missing report by the time money moves (findings 1 and 2) ----------

@pytest.fixture
def pay_env(db, monkeypatch):
    """A ZarinPal-ready gateway in IRT, same setup as test_payments.env, but with no weekly report yet: each test
    makes one fresh with live() only where it needs one, then can take it away again before paying."""
    from claude_proxy import payments, zarinpal
    from claude_proxy.config import Currency, ZarinpalConfig
    from tests.test_payments import MID, FakeZP
    conn, cfg, ids = seeded(db)
    cfg.tickets.currencies["IRT"] = Currency(round_to=1000)
    tickets.set_rate(conn, cfg, "IRT", 156250, ids["admin"], now=NOW - 3600)
    cfg.zarinpal = ZarinpalConfig()
    cfg.listener.home_url = "https://rahkar.pro"
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    zp = FakeZP()
    monkeypatch.setattr(zarinpal, "request", zp.request)
    monkeypatch.setattr(zarinpal, "verify", zp.verify)
    return conn, cfg, ids, zp, payments


async def test_pay_order_refuses_without_a_fresh_report(pay_env):
    """Review finding 1: the order was placed while the report was fresh; by the time alice comes back to pay, it is
    gone, so pay_order must refuse (not open a ZarinPal payment that tickets.grant could only fail at the callback,
    taking her money for nothing)."""
    from claude_proxy import orders
    conn, cfg, ids, zp, payments = pay_env
    live(conn, NOW)
    o = orders.create(conn, cfg, tier="lite", length="week", currency="IRT", name="Alice", email="alice@example.com",
                      user_id=ids["alice"], notify=False, terms_version="v1", now=NOW)
    conn.execute("DELETE FROM quota_snapshots")
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_order(conn, cfg, user(conn, ids["alice"]), o["id"], now=NOW)
    assert e.value.status == 409 and str(e.value) == orders.UNAVAILABLE
    assert zp.requests == [] and conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0


async def test_pay_link_refuses_without_a_fresh_report(pay_env):
    """Same as above, for a payment link: the admin made it while the report was fresh; it is gone by the time the
    buyer pays."""
    from claude_proxy import orders
    from claude_proxy.db import create_user
    conn, cfg, ids, zp, payments = pay_env
    live(conn, NOW)
    o = payments.create_link(conn, cfg, ids["admin"], email="bob@example.com", tier="lite", length="week",
                             pay_by=NOW + 86400, now=NOW)
    bob, _ = create_user(conn, "bob")
    conn.execute("UPDATE users SET email='bob@example.com' WHERE id=?", (bob,))
    conn.execute("DELETE FROM quota_snapshots")
    with pytest.raises(payments.PaymentError) as e:
        await payments.pay_link(conn, cfg, user(conn, bob), o["pay_code"], "v1", now=NOW)
    assert e.value.status == 409 and str(e.value) == orders.UNAVAILABLE
    assert zp.requests == [] and conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0


def test_create_link_refuses_as_unavailable_without_a_report(pay_env):
    """Review finding 2: create_link without a fresh report must say unavailable, like _check_on_sale, not sold
    out."""
    from claude_proxy import orders
    conn, cfg, ids, zp, payments = pay_env
    with pytest.raises(orders.OrderError, match="temporarily unavailable"):
        payments.create_link(conn, cfg, ids["admin"], email="bob@example.com", tier="lite", length="week",
                             pay_by=NOW + 86400, now=NOW)


# ---------- final review: _returned misses a pass's share bonuses (finding 3) ----------

def test_returned_counts_a_share_bonus_of_an_ended_pass(acct):
    conn, cfg, ids = acct
    add_user(conn, ids, "a")
    live(conn, NOW - 3 * DAY, 1)
    ended = grant(conn, cfg, ids, who="a", tier="standard", length="day", now=NOW - 3 * DAY)
    tickets.add_bonus(conn, cfg, ids["admin"], ended["id"], share_pct=10, now=NOW - 3 * DAY, ends_at=NOW - 2 * DAY)
    use(conn, ids["a"], ended["id"], 1, NOW - 3 * DAY + 3600)
    report(conn, ids, 2)
    s = capacity.summary(conn, cfg, 1, NOW)
    assert s["week"]["returned"] == pytest.approx(40 / 7 + 10 / 7 - 1)   # the pass's own piece, its bonus's, less use


# ---------- the current 5-hour window must hold a new pass's whole 5-hour share (2026-11-07) ----------

def five_hour(conn, util, t=NOW - 30, resets=NOW + 2 * 3600):
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (t, "header", "5h", util, resets))


def _five_hour_cfg(conn, cfg, ids):
    cfg.tickets.sell_margin_pct = 0
    tickets.set_share_5h(conn, cfg, "lite", 10, ids["admin"])
    live(conn, NOW)


def test_a_pass_is_refused_when_the_5h_window_is_nearly_full(db):
    conn, cfg, ids = seeded(db)
    _five_hour_cfg(conn, cfg, ids)
    five_hour(conn, 95.0)                     # all of it the owner's own: no request explains it
    with pytest.raises(tickets.CapacityError, match="5-hour window has 5.0 points free and the pass needs 10.0"):
        grant(conn, cfg, ids, tier="lite", length="day")
    assert tickets.sold_out(conn, cfg, "lite", "day", NOW)


def test_the_admin_may_grant_over_a_full_5h_window(db):
    import json
    from tests.tickets_helpers import report, user
    conn, cfg, ids = seeded(db)
    _five_hour_cfg(conn, cfg, ids)
    five_hour(conn, 95.0)
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "lite", "day", "EUR", NOW)
    assert (p["available"], p["short_5h"], p["over_5h"]) == (False, True, False)
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "lite", "day", "EUR", NOW, over_5h=True)
    assert (p["available"], p["short_5h"], p["over_5h"]) == (True, True, True) and "5-hour window" in p["reason"]
    tid = grant(conn, cfg, ids, tier="lite", length="day", over_5h=True)["id"]
    a = json.loads(conn.execute("SELECT detail_json FROM audit_log WHERE action='ticket_grant'").fetchone()[0])
    assert a["ticket_id"] == tid and a["over_5h"] is True


def test_over_5h_does_not_lift_the_weekly_checks(db):
    from tests.tickets_helpers import report, user
    conn, cfg, ids = seeded(db)
    _five_hour_cfg(conn, cfg, ids)
    five_hour(conn, 95.0)
    report(conn, ids, 99.0, NOW - 30)   # the week is nearly full too
    p = tickets.preview(conn, cfg, user(conn, ids["alice"]), "lite", "day", "EUR", NOW, over_5h=True)
    assert (p["available"], p["short_5h"], p["over_5h"]) == (False, False, False)
    with pytest.raises(tickets.CapacityError, match="This week has"):
        grant(conn, cfg, ids, tier="lite", length="day", over_5h=True)


def test_a_pass_fits_when_the_5h_window_has_its_share_free(db):
    conn, cfg, ids = seeded(db)
    _five_hour_cfg(conn, cfg, ids)
    five_hour(conn, 90.0)
    assert grant(conn, cfg, ids, tier="lite", length="day")["id"]


def test_running_passes_keep_their_5h_reservation(db):
    conn, cfg, ids = seeded(db)
    _five_hour_cfg(conn, cfg, ids)
    five_hour(conn, 0.0)
    grant(conn, cfg, ids, tier="lite", length="day")             # reserves 10: no use yet
    capacity.check_5h_room(conn, cfg, 1, 90, NOW, NOW)            # 100 − 0 − 10
    with pytest.raises(tickets.CapacityError, match="90.0 points free and the pass needs 95.0"):
        capacity.check_5h_room(conn, cfg, 1, 95, NOW, NOW)


def test_the_5h_room_is_not_checked_past_the_reset_or_without_a_fresh_report(db):
    conn, cfg, ids = seeded(db)
    _five_hour_cfg(conn, cfg, ids)
    five_hour(conn, 100.0, resets=NOW - 60)   # that window is over
    assert grant(conn, cfg, ids, tier="lite", length="day")["id"]
    capacity.check_5h_room(conn, cfg, 1, 10, NOW, NOW)


def test_a_pass_starting_after_the_5h_reset_is_not_held_to_the_current_window(db):
    conn, cfg, ids = seeded(db)
    _five_hour_cfg(conn, cfg, ids)
    five_hour(conn, 100.0)
    capacity.check_5h_room(conn, cfg, 1, 10, NOW + 2 * 3600, NOW)
    with pytest.raises(tickets.CapacityError):
        capacity.check_5h_room(conn, cfg, 1, 10, NOW + 3600, NOW)


def test_the_price_list_says_when_a_5h_sold_out_card_is_back(db):
    conn, cfg, ids = seeded(db)
    _five_hour_cfg(conn, cfg, ids)
    five_hour(conn, 95.0)
    lite = next(t for t in tickets.price_table(conn, cfg, NOW, "EUR")["tiers"] if t["tier"] == "lite")["lengths"]["day"]
    assert (lite["sold_out"], lite["available_at"]) == (True, NOW + 2 * 3600)


def test_a_card_sold_out_on_the_week_has_no_return_time(db):
    conn, cfg, ids = seeded(db)
    _five_hour_cfg(conn, cfg, ids)
    cfg.tickets.max_sold_pct = 0.5   # under a day pass's 5/7 piece
    lite = next(t for t in tickets.price_table(conn, cfg, NOW, "EUR")["tiers"] if t["tier"] == "lite")["lengths"]["day"]
    assert (lite["sold_out"], lite["available_at"]) == (True, None)
