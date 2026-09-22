from __future__ import annotations

import sqlite3

from fastapi import Header, HTTPException, Request

from .db import find_user_by_key


def extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    # Spec 4.1: Authorization: Bearer <virtual key>
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def authenticate(conn: sqlite3.Connection, authorization: str | None) -> sqlite3.Row:
    raw = extract_bearer(authorization)
    if not raw:
        raise HTTPException(
            status_code=401,
            detail={"type": "error", "error": {"type": "authentication_error", "message": "Missing bearer token"}},
        )
    row = find_user_by_key(conn, raw)
    if row is None or not row["enabled"] or row["revoked_at"] is not None:
        raise HTTPException(
            status_code=401,
            detail={"type": "error", "error": {"type": "authentication_error", "message": "Invalid or revoked virtual key"}},
        )
    return row


# FastAPI dependency helper
def require_user(request: Request):
    conn = request.app.state.db_conn
    auth = request.headers.get("authorization")
    return authenticate(conn, auth)
