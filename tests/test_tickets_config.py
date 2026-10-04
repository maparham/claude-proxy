import pytest

from claude_proxy.config import LENGTHS, Config, ConfigError, Tier, TicketsConfig


def load(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return Config.load(str(p))


def test_defaults_are_the_spec_tiers_and_disabled():
    t = TicketsConfig()
    assert t.enabled is False and t.max_sold_pct == 80 and t.currencies == {}
    assert t.tiers["lite"].share_pct == 5 and t.tiers["lite"].default_usd == {"day": 3, "week": 8, "month": 20}
    assert t.tiers["standard"].share_pct == 25 and t.tiers["standard"].compare == "Claude Max 5x"
    assert LENGTHS == {"day": 1, "week": 7, "month": 30}


def test_tickets_section_enables_and_overrides(tmp_path):
    cfg = load(tmp_path, """
[tickets]
how_to_buy = "Pay by bank transfer."
max_sold_pct = 60
[tickets.tiers.mini]
label = "Mini"
share_pct = 2.5
compare = "a trial"
default_usd = { day = 1, week = 4, month = 10 }
[tickets.currencies.eur]
round_to = 0.5
""")
    assert cfg.tickets.enabled is True and cfg.tickets.how_to_buy == "Pay by bank transfer."
    assert cfg.tickets.max_sold_pct == 60
    assert list(cfg.tickets.tiers) == ["mini"] and cfg.tickets.tiers["mini"].share_pct == 2.5
    assert cfg.tickets.currencies["EUR"].round_to == 0.5   # codes are upper-cased


def test_empty_tickets_section_keeps_default_tiers(tmp_path):
    cfg = load(tmp_path, "[tickets]\nhow_to_buy = 'x'\n")
    assert cfg.tickets.enabled and set(cfg.tickets.tiers) == {"lite", "standard"}


@pytest.mark.parametrize("body,needle", [
    ("[tickets]\nmax_sold_pct = 0\n", "max_sold_pct"),
    ("[tickets]\nmax_sold_pct = 101\n", "max_sold_pct"),
    ("[tickets]\nbogus = 1\n", "bogus"),
    ("[tickets.tiers.x]\nlabel='X'\nshare_pct=0\ndefault_usd={day=1,week=2,month=3}\n", "share_pct"),
    ("[tickets.tiers.x]\nlabel='X'\nshare_pct=5\ndefault_usd={day=1,week=2}\n", "default_usd"),
    ("[tickets.tiers.x]\nlabel='X'\nshare_pct=5\ndefault_usd={day=0,week=2,month=3}\n", "default_usd"),
    ("[tickets.tiers.Bad-Id]\nlabel='X'\nshare_pct=5\ndefault_usd={day=1,week=2,month=3}\n", "tier id"),
    ("[tickets.currencies.USD]\nround_to=0.01\n", "USD"),
    ("[tickets.currencies.EUR]\nround_to=0\n", "round_to"),
])
def test_bad_tickets_config_is_refused(tmp_path, body, needle):
    with pytest.raises(ConfigError) as e:
        load(tmp_path, body)
    assert needle in str(e.value)


def test_tier_dataclass_validates_directly():
    with pytest.raises(TypeError):
        Tier(label="X", share_pct=5, default_usd={"day": 1})
