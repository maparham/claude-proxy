import pytest

from claude_proxy.config import LENGTHS, Config, ConfigError, Tier, TicketsConfig


def load(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return Config.load(str(p))


def test_defaults_are_the_spec_tiers_and_disabled():
    t = TicketsConfig()
    assert t.enabled is False and t.max_sold_pct == 80 and t.currencies == {}
    assert t.tiers["lite"].size == 0.05 and t.tiers["lite"].default_usd == {"day": 3, "week": 8, "month": 20}
    assert t.tiers["standard"].size == 0.25 and not hasattr(t.tiers["standard"], "compare")
    assert LENGTHS == {"day": 1, "week": 7, "month": 30}


def test_tickets_section_enables_and_overrides(tmp_path):
    cfg = load(tmp_path, """
[tickets]
how_to_buy = "Pay by bank transfer."
max_sold_pct = 60
[tickets.tiers.mini]
label = "Mini"
size = 2.5
default_usd = { day = 1, week = 4, month = 10 }
[tickets.currencies.eur]
round_to = 0.5
""")
    assert cfg.tickets.enabled is True and cfg.tickets.how_to_buy == "Pay by bank transfer."
    assert cfg.tickets.max_sold_pct == 60
    assert list(cfg.tickets.tiers) == ["mini"] and cfg.tickets.tiers["mini"].size == 2.5
    assert cfg.tickets.currencies["EUR"].round_to == 0.5   # codes are upper-cased


@pytest.mark.parametrize("key", ["compare = 'Claude Pro'", "compare_ratio = 6"])
def test_plan_comparison_in_the_config_is_refused(tmp_path, key):
    with pytest.raises(ConfigError, match="Pricing tab"):
        load(tmp_path, f"[tickets]\n[tickets.tiers.lite]\nlabel = 'Lite'\nsize = 0.05\n{key}\n"
                       "default_usd = { day = 3, week = 8, month = 20 }\n")


def test_empty_tickets_section_keeps_default_tiers(tmp_path):
    cfg = load(tmp_path, "[tickets]\nhow_to_buy = 'x'\n")
    assert cfg.tickets.enabled and set(cfg.tickets.tiers) == {"lite", "standard"}


@pytest.mark.parametrize("body,needle", [
    ("[tickets]\nmax_sold_pct = 0\n", "max_sold_pct"),
    ("[tickets]\nmax_sold_pct = 101\n", "max_sold_pct"),
    ("[tickets]\nbogus = 1\n", "bogus"),
    ("[tickets.tiers.x]\nlabel='X'\nsize=0\ndefault_usd={day=1,week=2,month=3}\n", "size"),
    ("[tickets.tiers.x]\nlabel='X'\nsize=0.05\ndefault_usd={day=1,week=2}\n", "default_usd"),
    ("[tickets.tiers.x]\nlabel='X'\nsize=0.05\ndefault_usd={day=0,week=2,month=3}\n", "default_usd"),
    ("[tickets.tiers.Bad-Id]\nlabel='X'\nsize=0.05\ndefault_usd={day=1,week=2,month=3}\n", "tier id"),
    ("[tickets.tiers.x]\nlabel='X'\nshare_5h_pct=5\nsize=0.05\ndefault_usd={day=1,week=2,month=3}\n", "size_5h"),
    ("[tickets.currencies.USD]\nround_to=0.01\n", "USD"),
    ("[tickets.currencies.EUR]\nround_to=0\n", "round_to"),
    ("[tickets.currencies.EURO]\nround_to=0.5\n", "three letters"),
    ("[tickets.currencies.E1R]\nround_to=0.5\n", "three letters"),
    ("[tickets]\ncurrencies = 5\n", "bad [tickets] entry"),
    ("[tickets]\ntiers = 'lite'\n", "bad [tickets] entry"),
    ("[tickets]\nenabled = false\n", "remove the [tickets] section"),
    ("[tickets]\nbogus = 1\n", "how_to_buy, how_to_buy_fa, max_sold_pct"),
    ("[tickets]\nmax_sold_pct = 1\n", "fits no plan"),          # standard's 0.25 is 1.25% even of Max 20x
    ("retention_days = 7\n[tickets]\n", "retention_days must be at least 30"),
])
def test_bad_tickets_config_is_refused(tmp_path, body, needle):
    with pytest.raises(ConfigError) as e:
        load(tmp_path, body)
    assert needle in str(e.value)


def test_tier_dataclass_validates_directly():
    with pytest.raises(TypeError):
        Tier(label="X", size=0.05, default_usd={"day": 1})


def test_the_deploy_config_writes_out_the_default_tiers():
    from pathlib import Path
    cfg = Config.load(str(Path(__file__).resolve().parent.parent / "deploy" / "lightsail" / "config.toml"))
    assert cfg.tickets.tiers == TicketsConfig().tiers



async def test_maintenance_runs_the_estimates_even_when_cleanup_fails(monkeypatch, caplog):
    import asyncio

    from claude_proxy import cli, db, estimates
    calls = []

    def boom(*a):
        calls.append("cleanup")
        raise RuntimeError("disk full")

    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(db, "cleanup", boom)
    monkeypatch.setattr(estimates, "refresh_if_due", lambda *a: calls.append("estimates") or {})
    monkeypatch.setattr(cli.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await cli._maintenance(None, Config())
    assert calls == ["cleanup", "estimates"] and "retention cleanup failed" in caplog.text
