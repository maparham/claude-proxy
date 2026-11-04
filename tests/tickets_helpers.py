"""Shared fixtures for the paid-tickets tests."""
from claude_proxy import tickets
from claude_proxy.config import Config, Currency
from claude_proxy.db import create_user

NOW = 1_800_000_000          # 2027-01-15 08:00:00 UTC
DAY = 86400


def tcfg() -> Config:
    cfg = Config()
    cfg.tickets.enabled = True
    cfg.tickets.how_to_buy = "Send the amount by bank transfer and email the admin."
    cfg.tickets.currencies = {"EUR": Currency(round_to=0.5)}
    return cfg


def seeded(db):
    """(conn, cfg, ids): prices seeded from the default tiers, an admin and alice created, EUR at 0.92."""
    conn = db[1]
    cfg = tcfg()
    tickets.seed_prices(conn, cfg, now=NOW - DAY)
    tickets.seed_shares(conn, cfg, now=NOW - DAY)
    admin, _ = create_user(conn, "admin", role="admin")
    alice, _ = create_user(conn, "alice")
    tickets.set_rate(conn, cfg, "EUR", 0.92, admin, now=NOW - 3600)
    return conn, cfg, {"admin": admin, "alice": alice}


def user(conn, uid):
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


RESET7 = NOW + 3 * DAY       # the weekly reset the tests' snapshots report; the week is [NOW - 4d, NOW + 3d)
WEEK_START = RESET7 - 7 * DAY
PTS = 1000                   # weighted tokens per point: input tokens of claude-sonnet-5, the reference model (weight 1)


def live(conn, t=NOW, util7=0.0):
    """A 7-day snapshot 60 s before `t` reporting `util7`, its reset the first weekly reset after `t`, so a pass can be
    sold at `t` (2026-11-04 spec, §3). Insert snapshots in time order: attribution walks them sorted."""
    resets = RESET7
    while resets <= t:
        resets += 7 * DAY
    while resets - 7 * DAY > t:
        resets -= 7 * DAY
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (t - 60, "header", "7d", util7, resets))


def use(conn, uid, ticket_id, points, t):
    """`points` of use served on the pass (None: no pass), at PTS weighted tokens per point, ending at `t`."""
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens, output_tokens, "
                 "ticket_id) VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, t - 1, t, "POST", "/v1/messages", "anthropic",
                                                            "claude-sonnet-5", int(round(points * PTS)), 0, ticket_id))


def report(conn, ids, util7, t=NOW - 60):
    """A fresh 7-day snapshot at `t` reporting `util7`, with the whole rise since the previous snapshot attributed: the
    requests recorded since then are topped up with an admin request so that the observed rate stays PTS and nothing is
    unattributed (capacity 100). Snapshots must come in time order; a `t` before the newest one is a test bug."""
    last = conn.execute("SELECT observed_at, utilization_pct FROM quota_snapshots WHERE bucket='7d' ORDER BY observed_at DESC "
                        "LIMIT 1").fetchone()
    assert last is None or t > last[0], f"report() at {t} is not after the newest snapshot at {last[0]}"
    if last is None:
        live(conn, t - 3600, 0.0)
        since, prev = t - 3660, 0.0
    else:
        since, prev = last[0], last[1]
    tokens = conn.execute("SELECT COALESCE(SUM(input_tokens), 0) FROM requests WHERE provider='anthropic' AND ended_at>? "
                          "AND ended_at<=?", (since, t)).fetchone()[0]
    top = (util7 - prev) * PTS - tokens
    assert top >= -1e-6, f"report({util7}) is below what the requests since the last snapshot already used ({tokens / PTS} points)"
    if top > 0:
        use(conn, ids["admin"], None, top / PTS, t - 1)
    live(conn, t + 60, util7)
