from __future__ import annotations

import sqlite3

from .db import find_user_by_key


class AuthError(Exception):
    def __init__(self, status: int, error_type: str, message: str):
        self.status = status
        self.body = {"type": "error", "error": {"type": error_type, "message": message}}


def credential_from_headers(headers) -> str | None:
    """The virtual key: `Authorization: Bearer` (ANTHROPIC_AUTH_TOKEN) or `x-api-key` (ANTHROPIC_API_KEY)."""
    auth = headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return (headers.get("x-api-key") or "").strip() or None


def authenticate(conn: sqlite3.Connection, headers) -> sqlite3.Row:
    """Map a virtual key to its user. Fails closed (spec 4 step 2)."""
    raw = credential_from_headers(headers)
    if not raw:
        raise AuthError(401, "authentication_error", "Missing gateway key. Set ANTHROPIC_AUTH_TOKEN to your gateway key.")
    user = find_user_by_key(conn, raw)
    if user is None or user["revoked_at"] is not None:
        raise AuthError(401, "authentication_error", "Invalid or revoked gateway key.")
    if not user["enabled"]:
        raise AuthError(403, "permission_error", "This gateway key is disabled by the admin.")
    return user
