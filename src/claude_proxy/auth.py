from __future__ import annotations

import sqlite3

from .db import find_user_by_key


class AuthError(Exception):
    def __init__(self, status: int, error_type: str, message: str):
        self.status = status
        self.body = {"type": "error", "error": {"type": error_type, "message": message}}


# Sent via ANTHROPIC_CUSTOM_HEADERS by a machine that keeps its own claude.ai login active (so Claude in
# Chrome and claude.ai connectors work); its Authorization header then carries that login's token.
KEY_HEADER = "x-gateway-key"


def credential_from_headers(headers) -> str | None:
    """The virtual key: `x-gateway-key`, else `Authorization: Bearer` (ANTHROPIC_AUTH_TOKEN) or `x-api-key` (ANTHROPIC_API_KEY)."""
    key = (headers.get(KEY_HEADER) or "").strip()
    if key:
        return key
    auth = headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return (headers.get("x-api-key") or "").strip() or None


def client_status(headers, status: int) -> int:
    """A client signed in to claude.ai reads a 401 as its own login failing and retries it ten times; 403 fails once."""
    return 403 if status == 401 and headers.get(KEY_HEADER) else status


def authenticate(conn: sqlite3.Connection, headers) -> dict:
    """Map a virtual key to its user. Fails closed (spec 4 step 2)."""
    raw = credential_from_headers(headers)
    if not raw:
        raise AuthError(401, "authentication_error", "Missing gateway key. Set ANTHROPIC_AUTH_TOKEN to your gateway key.")
    if raw.startswith("sk-ant-"):
        raise AuthError(403, "permission_error", "This is a Claude login token, not a gateway key. Send the gateway key "
                        f"in the {KEY_HEADER} header (ANTHROPIC_CUSTOM_HEADERS) or as ANTHROPIC_AUTH_TOKEN.")
    user = find_user_by_key(conn, raw)
    if user is None or user["revoked_at"] is not None:
        raise AuthError(client_status(headers, 401), "authentication_error", "Invalid or revoked gateway key.")
    if not user["enabled"]:
        raise AuthError(403, "permission_error", "This gateway key is disabled by the admin.")
    return user
