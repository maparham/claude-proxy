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
    admin, _ = create_user(conn, "admin", role="admin")
    alice, _ = create_user(conn, "alice")
    tickets.set_rate(conn, cfg, "EUR", 0.92, admin, now=NOW - 3600)
    return conn, cfg, {"admin": admin, "alice": alice}


def user(conn, uid):
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
