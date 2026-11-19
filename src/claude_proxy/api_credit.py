"""What is left of the credit API (2026-10-21 spec) since API keys became pass app keys (2026-11-19 spec):
`refuse_extras`, which refuses features priced above plain tokens on app keys, and the per-model rate table
(`api_rates`, USD per million tokens per model glob, the longest matching glob wins), kept as seeded.
"""
from __future__ import annotations

import fnmatch
import sqlite3
import time

from .config import Config
from .limits import Decision

REJECT_EXTRA = "api_extra"
RATE_FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
# Models whose every price goes up for a whole request when its prompt (input, cache reads and cache writes) is over a
# threshold: (model glob, prompt tokens, multiplier). Anthropic's list prices, 2026-10-25.
LONG_PROMPT = (("claude-haiku-5-5*", 100_000, 5),)


def long_prompt(model: str) -> tuple[int, float] | None:
    return next(((over, x) for glob, over, x in LONG_PROMPT if fnmatch.fnmatchcase(model.lower(), glob)), None)


def _err(kind: str, message: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": message}}


# ---------- rates ----------

def _tidy(v: float) -> float:
    return round(v, 6)   # derived cache prices: 3 * 0.1 is 0.30000000000000004, which the rate editor would show

def rates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT pattern, input, output, cache_write_5m, cache_write_1h, cache_read, updated_at FROM api_rates")
    out = sorted((dict(r) for r in rows), key=lambda r: (-len(r["pattern"]), r["pattern"]))
    for r in out:   # a pattern names the models it starts with
        if lp := long_prompt(r["pattern"].rstrip("*")):
            r["long_prompt"] = {"over": lp[0], "times": lp[1]}
    return out


def rate_for(conn: sqlite3.Connection, model: str | None) -> dict | None:
    if not model:
        return None
    # SQLite's GLOB is case-sensitive like fnmatchcase; lower() matches config.Pricing.price_for.
    row = conn.execute("SELECT * FROM api_rates WHERE lower(?) GLOB lower(pattern) ORDER BY length(pattern) DESC, pattern "
                       "LIMIT 1", (model,)).fetchone()
    return dict(row) if row else None


def seed_rates(conn, cfg: Config, now=None) -> int:
    """Fill an empty rate table from the config's Claude list prices, once. Never refills: an admin who deleted every
    rate (to switch the API's models off) keeps an empty table across restarts."""
    if conn.execute("SELECT 1 FROM settings WHERE key='api_rates_seeded'").fetchone():
        return 0
    at = int(time.time() if now is None else now)
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('api_rates_seeded', ?)", (str(at),))
    if conn.execute("SELECT 1 FROM api_rates LIMIT 1").fetchone():   # seeded before this flag existed
        return 0
    n = 0
    for pattern, p in cfg.pricing.prices.items():
        if pattern.startswith("claude-"):
            conn.execute("INSERT INTO api_rates(pattern, input, output, cache_write_5m, cache_write_1h, cache_read, updated_at) "
                         "VALUES(?,?,?,?,?,?,?)", (pattern, *(_tidy(getattr(p, k)) for k in RATE_FIELDS), at))
            n += 1
    return n


# ---------- app keys ----------

def refuse_extras(data: dict | None, beta_header: str | None, app: bool = False) -> Decision | None:
    """Features Anthropic prices above plain tokens: refused on pass app keys (`app`). Without `app` the wording sends
    the user to Claude Code's switches."""
    tools = data.get("tools") if isinstance(data, dict) else None
    betas = [b.strip() for b in (beta_header or "").split(",")]
    msg = None
    if isinstance(tools, list) and any(isinstance(t, dict) and str(t.get("type", "")).startswith("web_search") for t in tools):
        msg = ("Web search is not available with pass app keys; turn web search off in the app." if app else
               "Web search is not available with API keys; turn off the WebSearch tool.")
    elif (isinstance(data, dict) and data.get("speed") not in (None, "standard")) or any(b.startswith("fast-mode") for b in betas):
        msg = "Fast mode is not available with pass app keys." if app else "Fast mode is not available with API keys; turn it off with /fast."
    elif any(b.startswith("context-1m") for b in betas):
        msg = ("The 1M-token context is not available with pass app keys; pick a standard model." if app else
               "The 1M-token context is not available with API keys; pick a model without [1m].")
    return Decision(400, REJECT_EXTRA, _err("invalid_request_error", msg)) if msg else None
