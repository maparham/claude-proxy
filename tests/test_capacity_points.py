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
