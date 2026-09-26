"""Clerk as the dashboard's sign-in (sign-up design section 2): it proves who someone is, nothing more.

The browser signs in with Clerk and sends its session token once, to /api/login/clerk. That token is checked here,
without Clerk's SDK: an RS256 JWT signed by one of the instance's keys (its JWKS, cached), for this instance and this
dashboard. The verified email then comes from Clerk's Backend API. Everything after that is the gateway's own session.
"""
from __future__ import annotations

import base64
import json
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

BACKEND_API = "https://api.clerk.com/v1"
LEEWAY_S = 10
JWKS_TTL_S = 3600


class ClerkError(Exception):
    pass


def frontend_api(publishable_key: str) -> str | None:
    """The instance's Frontend API host, which a publishable key encodes: pk_live_<base64("clerk.example.com$")>."""
    for prefix in ("pk_live_", "pk_test_"):
        if publishable_key.startswith(prefix):
            b = publishable_key[len(prefix):]
            try:
                host = base64.b64decode(b + "=" * (-len(b) % 4)).decode()
            except (ValueError, UnicodeDecodeError):
                return None
            return host[:-1] if host.endswith("$") and "/" not in host else None
    return None


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _public_key(jwk: dict) -> rsa.RSAPublicKey:
    n, e = (int.from_bytes(_b64(jwk[k]), "big") for k in ("n", "e"))
    return rsa.RSAPublicNumbers(e, n).public_key()


class Verifier:
    def __init__(self, publishable_key: str, secret_key: str | None, dashboard_url: str, http: httpx.AsyncClient):
        self.fapi = frontend_api(publishable_key)
        if not self.fapi:
            raise ClerkError("signup.clerk_publishable_key is not a Clerk publishable key")
        self.secret, self.origin, self.http = secret_key, dashboard_url.rstrip("/"), http
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._fetched = 0.0

    async def _key(self, kid: str) -> rsa.RSAPublicKey:
        # Refetched hourly, and at once for a key id not seen yet (Clerk rotated), at most every 30 s.
        stale = time.time() - self._fetched > JWKS_TTL_S
        if stale or (kid not in self._keys and time.time() - self._fetched > 30):
            r = await self.http.get(f"https://{self.fapi}/.well-known/jwks.json", timeout=10)
            r.raise_for_status()
            try:
                self._keys = {k["kid"]: _public_key(k) for k in r.json().get("keys", []) if k.get("kty") == "RSA" and "kid" in k}
            except (ValueError, KeyError, TypeError, AttributeError):
                raise ClerkError("Clerk's key list is unreadable") from None
            self._fetched = time.time()
        if kid not in self._keys:
            raise ClerkError("token signed by an unknown key")
        return self._keys[kid]

    async def verify(self, token: str, now: float | None = None) -> dict:
        """The token's claims, if it is a valid, current session token of this instance for this dashboard."""
        now = time.time() if now is None else now
        try:
            head_b64, body_b64, sig_b64 = token.split(".")
            head, claims = json.loads(_b64(head_b64)), json.loads(_b64(body_b64))
        except ValueError:
            raise ClerkError("not a JWT") from None
        if not isinstance(head, dict) or not isinstance(claims, dict) or head.get("alg") != "RS256":
            raise ClerkError("not an RS256 JWT")
        key = await self._key(str(head.get("kid", "")))
        try:
            key.verify(_b64(sig_b64), f"{head_b64}.{body_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError):
            raise ClerkError("bad signature") from None
        exp, nbf = claims.get("exp"), claims.get("nbf", claims.get("iat", 0))
        if not isinstance(exp, (int, float)) or exp + LEEWAY_S < now:
            raise ClerkError("token expired")
        if isinstance(nbf, (int, float)) and nbf - LEEWAY_S > now:
            raise ClerkError("token not valid yet")
        if claims.get("iss") != f"https://{self.fapi}":
            raise ClerkError("token from another Clerk instance")
        # A session token (it has a session id) made in this dashboard, not any other token the instance signs.
        if not isinstance(claims.get("azp"), str) or claims["azp"].rstrip("/") != self.origin:
            raise ClerkError("token made for another site")
        if not str(claims.get("sub", "")).startswith("user_") or not claims.get("sid"):
            raise ClerkError("not a user's session token")
        return claims

    async def email(self, user_id: str) -> str:
        """The user's verified primary email, lower-cased."""
        if not self.secret:
            raise ClerkError("CLERK_SECRET_KEY is not set")
        r = await self.http.get(f"{BACKEND_API}/users/{user_id}", headers={"Authorization": f"Bearer {self.secret}"}, timeout=10)
        if r.status_code != 200:
            raise ClerkError(f"Clerk answered {r.status_code} for the user")
        u = r.json()
        primary = u.get("primary_email_address_id")
        for e in u.get("email_addresses") or []:
            if e.get("id") == primary and (e.get("verification") or {}).get("status") == "verified":
                return str(e["email_address"]).strip().lower()
        raise ClerkError("the account has no verified email address")
