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
