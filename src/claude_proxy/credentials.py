from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from typing import Protocol
import sqlite3

import httpx

from .db import get_conn


class BackendStatus:
    def __init__(self, backend: str, healthy: bool, detail: str = ""):
        self.backend = backend
        self.healthy = healthy
        self.detail = detail


class CredentialBackend(Protocol):
    async def upstream_headers(self) -> dict[str, str]: ...
    async def on_unauthorized(self) -> bool: ...
    async def poll_usage(self) -> dict | None: ...
    def describe(self) -> BackendStatus: ...


# --- Encryption helpers (Fernet, key from env or file 0600, spec 5.1 Storage) ---

def _get_fernet():
    # Key priority: env CLAUDE_PROXY_CREDENTIAL_KEY (base64 urlsafe 32 bytes) or file
    key_b64 = os.environ.get("CLAUDE_PROXY_CREDENTIAL_KEY")
    key_file = os.environ.get("CLAUDE_PROXY_CREDENTIAL_KEY_FILE")
    raw_key: bytes | None = None
    if key_b64:
        raw_key = base64.urlsafe_b64decode(key_b64.encode())
    elif key_file and os.path.exists(key_file):
        with open(key_file, "rb") as f:
            raw_key = base64.urlsafe_b64decode(f.read().strip())
    else:
        # Ephemeral — warn, not persisted. For dev/test.
        # Generate deterministic for tests if needed: use env to override in tests
        raw_key = hashlib.sha256(b"claude-proxy-dev-key").digest()
    # Fernet expects 32 urlsafe base64-encoded
    fernet_key = base64.urlsafe_b64encode(raw_key[:32])
    from cryptography.fernet import Fernet

    return Fernet(fernet_key)


def encrypt_blob(data: dict) -> str:
    f = _get_fernet()
    return f.encrypt(json.dumps(data).encode()).decode()


def decrypt_blob(token: str) -> dict:
    f = _get_fernet()
    return json.loads(f.decrypt(token.encode()).decode())


# --- PKCE helpers ---

def generate_pkce() -> tuple[str, str, str]:
    """Returns (verifier, challenge, state)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
    return verifier, challenge, state


def build_authorize_url(cfg, verifier_challenge: str, state: str) -> str:
    # Spec 5.1: prints authorization URL, admin completes in browser, pastes code
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": cfg.credential.client_id,
        "redirect_uri": cfg.credential.redirect_uri,
        "scope": cfg.credential.scopes,
        "state": state,
        "code_challenge": verifier_challenge,
        "code_challenge_method": "S256",
    }
    return f"{cfg.credential.authorize_url}?{urlencode(params)}"


# --- OAuth backend ---

class OAuthBackend:
    """sole refresher, single-flight, proactive <5m, reactive 401, encrypted storage (spec 5.1)."""

    def __init__(self, cfg, db_path: str):
        self.cfg = cfg
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._needs_login = False

    def _conn(self) -> sqlite3.Connection:
        conn = get_conn(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_row(self) -> tuple[dict | None, int | None, str | None]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT encrypted_blob, expires_at, state FROM credentials WHERE backend='oauth'").fetchone()
            if not row or not row["encrypted_blob"]:
                return None, None, None
            try:
                data = decrypt_blob(row["encrypted_blob"])
            except Exception:
                return None, None, "decrypt_failed"
            return data, row["expires_at"], row["state"]
        finally:
            conn.close()

    def _save_tokens(self, data: dict, expires_at: int, state: str = "active"):
        # Write new refresh before discarding old — crash-safe: INSERT OR REPLACE
        blob = encrypt_blob(data)
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO credentials(backend, encrypted_blob, expires_at, updated_at, state) VALUES('oauth',?,?,?,?)",
                (blob, expires_at, int(time.time()), state),
            )
            conn.commit()
        finally:
            conn.close()

    def _is_expiring(self, expires_at: int | None) -> bool:
        if not expires_at:
            return True
        return (expires_at - int(time.time())) < 300  # 5m (spec 5.1)

    async def _refresh(self) -> bool:
        async with self._lock:
            data, expires_at, state = self._load_row()
            if data is None:
                self._needs_login = True
                return False
            # If another coroutine already refreshed while we waited, check again
            if not self._is_expiring(expires_at) and state == "active":
                return True
            refresh_token = data.get("refresh_token") or data.get("refreshToken")
            if not refresh_token:
                self._needs_login = True
                return False
            # Exchange refresh_token
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    self.cfg.credential.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self.cfg.credential.client_id,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code != 200:
                    # invalid_grant -> needs_login (spec 5.1 Failure)
                    try:
                        body = resp.json()
                        err = body.get("error") or body.get("error_description", "")
                    except Exception:
                        err = resp.text
                    if "invalid_grant" in err or resp.status_code in (400, 401):
                        conn = self._conn()
                        try:
                            conn.execute("UPDATE credentials SET state='needs_login', updated_at=? WHERE backend='oauth'", (int(time.time()),))
                            conn.commit()
                        finally:
                            conn.close()
                        self._needs_login = True
                    return False
                tok = resp.json()
                new_data = {
                    "access_token": tok["access_token"],
                    "refresh_token": tok.get("refresh_token", refresh_token),
                    "expires_at": int(time.time()) + int(tok.get("expires_in", 28800)),
                    "scope": tok.get("scope"),
                    "raw": tok,
                }
                # Write new before discarding old — already done via save
                self._save_tokens(new_data, new_data["expires_at"], "active")
                self._needs_login = False
                return True

    async def upstream_headers(self) -> dict[str, str]:
        data, expires_at, state = self._load_row()
        if state == "needs_login" or self._needs_login:
            raise RuntimeError("needs_login")
        if data is None:
            raise RuntimeError("no credential — run `claude-proxy login`")
        if self._is_expiring(expires_at):
            ok = await self._refresh()
            if not ok:
                raise RuntimeError("needs_login")
            data, _, _ = self._load_row()
            if data is None:
                raise RuntimeError("needs_login")
        access = data.get("access_token") or data.get("accessToken")
        if not access:
            raise RuntimeError("no access_token")
        headers = {"Authorization": f"Bearer {access}"}
        # Spec 5.1: append oauth-2025-04-20 to anthropic-beta if absent — forwarder will merge
        # We return it separately; app merges with client's beta
        headers["anthropic-beta"] = self.cfg.credential.beta_flag
        return headers

    async def on_unauthorized(self) -> bool:
        # Reactive once on 401 (spec 5.1 When to refresh)
        # Single-flight lock ensures concurrent callers wait
        return await self._refresh()

    async def poll_usage(self) -> dict | None:
        # Call usage endpoint with same bearer + beta + User-Agent claude-cli
        data, _, state = self._load_row()
        if not data or state == "needs_login":
            return None
        access = data.get("access_token") or data.get("accessToken")
        if not access:
            return None
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.get(
                    self.cfg.credential.usage_url,
                    headers={
                        "Authorization": f"Bearer {access}",
                        "anthropic-beta": self.cfg.credential.beta_flag,
                        "User-Agent": "claude-cli/1.0.60 (external, cli)",
                    },
                )
                if resp.status_code == 429:
                    return None  # backoff handled by quota module
                if resp.status_code != 200:
                    return None
                return resp.json()
            except Exception:
                return None

    def describe(self) -> BackendStatus:
        _, _, state = self._load_row()
        if state == "needs_login" or self._needs_login:
            return BackendStatus("oauth", False, "needs_login — run `claude-proxy login`")
        data, expires_at, _ = self._load_row()
        if not data:
            return BackendStatus("oauth", False, "no credential")
        return BackendStatus("oauth", True, f"expires_at={expires_at}")


def make_backend(cfg, db_path: str | None = None) -> CredentialBackend:
    # OAuth-only per user request
    path = db_path or cfg.db.path
    return OAuthBackend(cfg, path)
