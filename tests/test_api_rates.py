"""What is left of the credit API (2026-11-19 spec): the seeded rate table and the [api] section still loading."""
import pytest

from claude_proxy import api_credit
from claude_proxy.config import Config, ConfigError


def add_rate(conn, pattern, inp, out, cache_read=None):
    conn.execute("INSERT INTO api_rates(pattern, input, output, cache_write_5m, cache_write_1h, cache_read, updated_at) "
                 "VALUES(?,?,?,?,?,?,0)", (pattern, inp, out, inp * 1.25, inp * 2, inp * 0.1 if cache_read is None else cache_read))


def test_an_old_api_section_still_loads(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[api]\nmax_inflight = 2\nrpm = 30\n")
    Config.load(str(p))


def test_a_bad_api_section_is_still_refused(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[api]\nfoo = 1\n")
    with pytest.raises(ConfigError, match=r"unknown key \[api\]\.foo"):
        Config.load(str(p))


def test_seed_copies_claude_list_prices_once(db, cfg):
    conn = db[1]
    n = api_credit.seed_rates(conn, cfg)
    pats = [r["pattern"] for r in api_credit.rates(conn)]
    assert n == len(pats) and all(p.startswith("claude-") for p in pats) and "muse-spark*" not in pats
    conn.execute("DELETE FROM api_rates WHERE pattern='claude-opus-5*'")
    assert api_credit.seed_rates(conn, cfg) == 0    # never refills an edited table


def test_the_gateway_seeds_the_rates_without_an_api_section(db, cfg):
    from tests.conftest import make_gateway
    conn = db[1]
    make_gateway(cfg, conn)
    assert api_credit.rates(conn) != []


def test_longest_matching_pattern_wins(db):
    conn = db[1]
    add_rate(conn, "claude-opus-5*", 5, 25)
    add_rate(conn, "claude-opus-5-5*", 4, 20, cache_read=0.2)
    assert api_credit.rate_for(conn, "claude-opus-5-5-20261001")["input"] == 4
    assert api_credit.rate_for(conn, "claude-opus-5-1")["input"] == 5
    assert api_credit.rate_for(conn, "claude-haiku-4") is None


def test_seeded_rates_are_tidy_and_price_opus_4_and_4_1_at_their_own_list_price(db, cfg):
    conn = db[1]
    api_credit.seed_rates(conn, cfg)
    assert all(r[f] == round(r[f], 6) for r in api_credit.rates(conn) for f in api_credit.RATE_FIELDS)
    assert api_credit.rate_for(conn, "claude-opus-4-1-20250805")["input"] == 15
    assert api_credit.rate_for(conn, "claude-opus-4-20250514")["output"] == 75
    assert api_credit.rate_for(conn, "claude-opus-4-5-20251101")["input"] == 5


def test_a_table_seeded_before_the_flag_existed_is_not_refilled(db, cfg):
    conn = db[1]
    add_rate(conn, "claude-x*", 1, 1)   # an older gateway seeded the table: no flag yet
    assert api_credit.seed_rates(conn, cfg) == 0
    conn.execute("DELETE FROM api_rates")
    assert api_credit.seed_rates(conn, cfg) == 0
