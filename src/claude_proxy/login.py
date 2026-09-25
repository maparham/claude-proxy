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
from .credentials import (OAuthBackend, build_authorize_url, decrypt_blob, encrypt_blob, generate_pkce,
                          parse_pasted_code, token_record, token_request)

PENDING_KEY = "pending_login"
PENDING_TTL_S = 15 * 60


class LoginError(Exception):
    pass


def start(conn: sqlite3.Connection, cfg) -> str:
    verifier, challenge, state = generate_pkce()
    blob = encrypt_blob({"verifier": verifier, "state": state, "created": time.time()})
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (PENDING_KEY, blob))
    return build_authorize_url(cfg, challenge, state)


def _pending(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (PENDING_KEY,)).fetchone()
    return decrypt_blob(row[0]) if row else None


async def finish(conn: sqlite3.Connection, cfg, http: httpx.AsyncClient, pasted: str) -> dict:
    pending = _pending(conn)
    if pending is None:
        raise LoginError("No login in progress. Run `claude-proxy login --print-url` first.")
    if time.time() - pending["created"] > PENDING_TTL_S:
        conn.execute("DELETE FROM settings WHERE key=?", (PENDING_KEY,))
        raise LoginError("That login attempt expired. Run `claude-proxy login --print-url` again.")
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
    conn.execute("DELETE FROM settings WHERE key=?", (PENDING_KEY,))
    if resp.status_code != 200:
        raise LoginError(f"Token exchange failed: HTTP {resp.status_code} {resp.text[:500]}")
    try:
        record = token_record(resp.json())
    except (ValueError, KeyError, TypeError, AttributeError):
        raise LoginError(f"The token endpoint's answer has no access token: {resp.text[:300]}") from None
    OAuthBackend(cfg, conn, http).store(record)
    db.audit(conn, None, "login", record.get("account") or "oauth", {"scope": record.get("scope")})
    return record
