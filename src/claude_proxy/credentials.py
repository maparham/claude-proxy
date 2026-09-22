from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet

logger = logging.getLogger("claude_proxy")


@dataclass
class BackendStatus:
    backend: str
    healthy: bool
    detail: str = ""
    expires_at: int | None = None


class CredentialBackend(Protocol):
    async def upstream_headers(self) -> dict[str, str]: ...
    async def on_unauthorized(self, failed_token: str | None) -> bool: ...
    async def poll_usage(self) -> tuple[int, dict | None]: ...
    def describe(self) -> BackendStatus: ...


class NeedsLogin(Exception):
    """The backend has no usable credential; the admin must run `claude-proxy login`."""


# --- Encryption of the stored grant (spec 5.1 Storage) ---

class CredentialKeyMissing(Exception):
    pass


KEY_HELP = ("No credential encryption key. Generate one with `claude-proxy keygen > key && chmod 600 key` "
            "and set CLAUDE_PROXY_CREDENTIAL_KEY_FILE=key (or CLAUDE_PROXY_CREDENTIAL_KEY to its contents).")


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    key = os.environ.get("CLAUDE_PROXY_CREDENTIAL_KEY")
    key_file = os.environ.get("CLAUDE_PROXY_CREDENTIAL_KEY_FILE")
    if not key and key_file:
        if not os.path.exists(key_file):
            raise CredentialKeyMissing(f"CLAUDE_PROXY_CREDENTIAL_KEY_FILE={key_file} does not exist")
        if os.stat(key_file).st_mode & 0o077:
            raise CredentialKeyMissing(f"{key_file} must not be readable by group or others (chmod 600)")
        with open(key_file) as f:
            key = f.read().strip()
    if not key:
        raise CredentialKeyMissing(KEY_HELP)
    try:
        return Fernet(key.encode())
    except ValueError as e:
        raise CredentialKeyMissing(f"credential key is not a valid Fernet key: {e}") from e


def check_key() -> None:
    _fernet()


def encrypt_blob(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_blob(token: str) -> dict:
    return json.loads(_fernet().decrypt(token.encode()).decode())


# --- PKCE (spec 5.1 Own grant) ---

def generate_pkce() -> tuple[str, str, str]:
    """Returns (verifier, challenge, state)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    return verifier, challenge, state


def build_authorize_url(cfg, challenge: str, state: str) -> str:
    params = {
        "code": "true",
        "client_id": cfg.credential.client_id,
        "response_type": "code",
        "redirect_uri": cfg.credential.redirect_uri,
        "scope": cfg.credential.scopes,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{cfg.credential.authorize_url}?{urlencode(params)}"


def parse_pasted_code(pasted: str, expected_state: str) -> str:
    """Accept a bare code, `code#state`, or the full callback URL; verify state when present."""
    from urllib.parse import parse_qs, urlparse
    pasted = pasted.strip()
    state = None
    if pasted.startswith("http"):
        u = urlparse(pasted)
        qs = parse_qs(u.query)
        code = qs.get("code", [""])[0]
        state = qs.get("state", [None])[0] or (u.fragment or None)
    else:
        code = pasted
    if "#" in code:
        code, state = code.split("#", 1)
    if state is not None and state != expected_state:
        raise ValueError("state mismatch — start `claude-proxy login` again")
    if not code:
        raise ValueError("no authorization code found")
    return code


def token_record(tok: dict, previous_refresh: str | None = None) -> dict:
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token") or previous_refresh,
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
        "scope": tok.get("scope"),
        "account": (tok.get("account") or {}).get("email_address"),
    }


# --- OAuth backend ---

class OAuthBackend:
    """Holds the gateway's own grant. Sole refresher, single-flight (spec 5.1)."""

    def __init__(self, cfg, conn: sqlite3.Connection, http: httpx.AsyncClient):
        self.cfg = cfg
        self.conn = conn
        self.http = http
        self._lock = asyncio.Lock()
        self._cache: dict | None = None
        self._state: str | None = None
        self.last_error: str | None = None

    def _load(self) -> tuple[dict | None, str | None]:
        if self._cache is not None:
            return self._cache, self._state
        row = self.conn.execute("SELECT encrypted_blob, state FROM credentials WHERE backend='oauth'").fetchone()
        if not row or not row["encrypted_blob"]:
            return None, None
        try:
            data = decrypt_blob(row["encrypted_blob"])
        except CredentialKeyMissing:
            raise
        except Exception:
            return None, "decrypt_failed"
        self._cache, self._state = data, row["state"]
        return data, row["state"]

    def store(self, data: dict, state: str = "active") -> None:
        # One statement replaces the row atomically, so the new refresh token is durable
        # before the old one is gone.
        self.conn.execute(
            "INSERT OR REPLACE INTO credentials(backend, encrypted_blob, expires_at, updated_at, state) VALUES('oauth',?,?,?,?)",
            (encrypt_blob(data), data["expires_at"], int(time.time()), state),
        )
        self._cache, self._state = data, state

    def _mark_needs_login(self, reason: str) -> None:
        self.conn.execute("UPDATE credentials SET state='needs_login', updated_at=? WHERE backend='oauth'", (int(time.time()),))
        self._state = "needs_login"
        self.last_error = reason
        logger.error("oauth grant needs login: %s", reason)

    async def _refresh(self, failed_token: str | None, force: bool) -> bool:
        async with self._lock:
            data, state = self._load()
            if data is None or state == "needs_login":
                return False
            # Another caller refreshed while we waited for the lock.
            if failed_token is not None and data["access_token"] != failed_token:
                return True
            if not force and data["expires_at"] - time.time() >= 300:
                return True
            refresh_token = data.get("refresh_token")
            if not refresh_token:
                self._mark_needs_login("no refresh token stored")
                return False
            try:
                resp = await self.http.post(
                    self.cfg.credential.token_url,
                    data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": self.cfg.credential.client_id},
                    headers={"User-Agent": self.cfg.credential.user_agent},
                    timeout=30,
                )
            except httpx.HTTPError as e:
                self.last_error = f"refresh transport error: {e}"
                logger.warning(self.last_error)
                return False
            if resp.status_code != 200:
                body = resp.text[:300]
                if resp.status_code in (400, 401) or "invalid_grant" in body:
                    self._mark_needs_login(f"refresh rejected {resp.status_code}: {body}")
                else:
                    self.last_error = f"refresh failed {resp.status_code}: {body}"
                    logger.warning(self.last_error)
                return False
            self.store(token_record(resp.json(), refresh_token))
            self.last_error = None
            logger.info("oauth access token refreshed")
            return True

    async def upstream_headers(self) -> dict[str, str]:
        data, state = self._load()
        if data is None or state in ("needs_login", "decrypt_failed"):
            raise NeedsLogin(state or "no credential")
        if data["expires_at"] - time.time() < 300:
            if not await self._refresh(None, force=False):
                data, state = self._load()
                if state == "needs_login" or data["expires_at"] <= time.time():
                    raise NeedsLogin(self.last_error or "refresh failed")
            data, _ = self._load()
        return {"authorization": f"Bearer {data['access_token']}", "anthropic-beta": self.cfg.credential.beta_flag}

    async def on_unauthorized(self, failed_token: str | None) -> bool:
        return await self._refresh(failed_token, force=True)

    def current_token(self) -> str | None:
        data, _ = self._load()
        return data["access_token"] if data else None

    async def poll_usage(self) -> tuple[int, dict | None]:
        """Returns (http_status, body). Status 0 means no request was made."""
        try:
            headers = await self.upstream_headers()
        except NeedsLogin:
            return 0, None
        headers["user-agent"] = self.cfg.credential.user_agent
        headers["content-type"] = "application/json"
        try:
            resp = await self.http.get(self.cfg.credential.usage_url, headers=headers, timeout=20)
        except httpx.HTTPError:
            return 0, None
        if resp.status_code != 200:
            return resp.status_code, None
        try:
            return 200, resp.json()
        except ValueError:
            return 200, None

    def describe(self) -> BackendStatus:
        try:
            data, state = self._load()
        except CredentialKeyMissing as e:
            return BackendStatus("oauth", False, str(e))
        if data is None:
            return BackendStatus("oauth", False, "not linked — run `claude-proxy login`")
        if state in ("needs_login", "decrypt_failed"):
            return BackendStatus("oauth", False, f"{state} — run `claude-proxy login`. {self.last_error or ''}".strip(), data.get("expires_at"))
        detail = f"linked{' as ' + data['account'] if data.get('account') else ''}"
        if self.last_error:
            detail += f"; last error: {self.last_error}"
        return BackendStatus("oauth", True, detail, data.get("expires_at"))
