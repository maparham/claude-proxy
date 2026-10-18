"""The gateway's own OAuth grant, in two steps so the code can be supplied by a separate command.

`start` stores the PKCE verifier and state (encrypted, short-lived) and returns the authorize URL;
`finish` exchanges the pasted code with that verifier and stores the grant.
"""
from __future__ import annotations

import json
import sqlite3
import time

import httpx

from . import db
from .credentials import (DEFAULT_ACCOUNT, OAuthBackend, build_authorize_url, decrypt_blob, encrypt_blob,
                           generate_pkce, parse_pasted_code, token_record, token_request, validate_account)

PENDING_KEY = "pending_login"
PENDING_TTL_S = 15 * 60


class LoginError(Exception):
    pass


def pending_key(account: str) -> str:
    return f"{PENDING_KEY}:{validate_account(account)}"


def start(conn: sqlite3.Connection, cfg, account: str = DEFAULT_ACCOUNT) -> str:
    verifier, challenge, state = generate_pkce()
    blob = encrypt_blob({"verifier": verifier, "state": state, "created": time.time()})
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (pending_key(account), blob))
    return build_authorize_url(cfg, challenge, state)


def _pending(conn: sqlite3.Connection, account: str = DEFAULT_ACCOUNT) -> dict | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (pending_key(account),)).fetchone()
    return decrypt_blob(row[0]) if row else None


async def finish(conn: sqlite3.Connection, cfg, http: httpx.AsyncClient, pasted: str,
                 account: str = DEFAULT_ACCOUNT, actor_id: int | None = None) -> dict:
    account = validate_account(account)
    pending = _pending(conn, account)
    if pending is None:
        raise LoginError(f"No login in progress for account {account!r}. "
                         f"Run `claude-proxy login --print-url --account {account}` first.")
    if time.time() - pending["created"] > PENDING_TTL_S:
        conn.execute("DELETE FROM settings WHERE key=?", (pending_key(account),))
        raise LoginError(f"That login attempt expired. Run `claude-proxy login --print-url --account {account}` again.")
    try:
        code = parse_pasted_code(pasted, pending["state"])
    except ValueError as e:
        raise LoginError(str(e)) from None
    resp = await token_request(http, cfg, {"grant_type": "authorization_code", "code": code,
                                           "redirect_uri": cfg.credential.redirect_uri,
                                           "code_verifier": pending["verifier"], "state": pending["state"]})
    if resp.status_code == 429 or resp.status_code >= 500:
        # Nothing was exchanged; keep the attempt so the same code can be retried.
        raise LoginError(f"The token endpoint answered HTTP {resp.status_code}; wait a minute and try again "
                         f"with the same code. {resp.text[:300]}")
    conn.execute("DELETE FROM settings WHERE key=?", (pending_key(account),))
    if resp.status_code != 200:
        raise LoginError(f"Token exchange failed: HTTP {resp.status_code} {resp.text[:500]}")
    try:
        record = token_record(resp.json())
    except (ValueError, KeyError, TypeError, AttributeError):
        raise LoginError(f"The token endpoint's answer has no access token: {resp.text[:300]}") from None
    if record.get("anthropic_sub"):
        dupe = conn.execute("SELECT account FROM credentials WHERE backend='oauth' AND account!=? AND anthropic_sub=?",
                            (account, record["anthropic_sub"])).fetchone()
        if dupe:
            raise LoginError(f"This subscription is already linked as account {dupe['account']!r}. "
                             f"Remove that account first, or start over with "
                             f"`claude-proxy login --print-url --account {dupe['account']}`.")
    try:
        OAuthBackend(cfg, conn, http, account).store(record)
    except sqlite3.IntegrityError:  # a concurrent login linked the same subscription first
        raise LoginError("This subscription was just linked under another account.") from None
    db.audit(conn, actor_id, "login", f"oauth:{account}",
             {"email": record.get("account"), "scope": record.get("scope")})
    return record


def remove_account(conn: sqlite3.Connection, account: str, actor_id: int | None = None) -> None:
    """Unlink one subscription account (CLI `account remove` and the dashboard's Accounts tab)."""
    try:
        account = validate_account(account)
    except ValueError as e:
        raise LoginError(str(e)) from None
    if account == DEFAULT_ACCOUNT:
        # The gateway serves every request from the default account until routing lands (spec section 4).
        raise LoginError(f"Refusing to remove {account!r}: the gateway serves all traffic from it. "
                         f"Re-link it with `claude-proxy login --account {account}` instead.")
    if conn.execute("SELECT 1 FROM credentials WHERE backend='oauth' AND account=?", (account,)).fetchone() is None:
        raise LoginError(f"No subscription account {account!r}.")
    if conn.execute("SELECT COUNT(*) FROM credentials WHERE backend='oauth'").fetchone()[0] <= 1:
        raise LoginError(f"Refusing to remove {account!r}: it is the last linked subscription account.")
    conn.execute("DELETE FROM settings WHERE key=?", (pending_key(account),))
    conn.execute("DELETE FROM credentials WHERE backend='oauth' AND account=?", (account,))
    db.audit(conn, actor_id, "account_remove", f"oauth:{account}")
