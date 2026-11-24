"""Which subscription account serves a request (2026-11-24 spec §4): its serving pass's, else the one assigned to the
user, else none and the request is refused. A request never moves to another account, whatever state its own is in."""
from __future__ import annotations

import sqlite3

from . import limits, tickets

NO_ACCOUNT = "no_account"   # rejected_by: no pass and no assigned account
NO_ACCOUNT_MSG = "No subscription is assigned to you; ask the admin."


def assigned(conn: sqlite3.Connection, user_id: int) -> str | None:
    """The account assigned to the user (users.account); None while unassigned."""
    row = conn.execute("SELECT account FROM users WHERE id=?", (user_id,)).fetchone()
    return row[0] if row else None


def account_for(conn: sqlite3.Connection, user_id: int, now: float, served: dict | None = None) -> str | None:
    """The account the user's allowed request goes to. `served`: limits.evaluate's, naming the pass it picked. Token
    counts and the model list are allowed without picking a pass; they go where the user's next message would most
    likely go, the first of their running passes in serving order. Without a running pass, the assigned account."""
    if served and served.get("account"):
        return served["account"]
    run = tickets.running(conn, user_id, now)
    if run:
        return limits.pass_order(conn, user_id, run)[0]["account"]
    return assigned(conn, user_id)
