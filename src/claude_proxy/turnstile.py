"""Cloudflare Turnstile check for visitor orders (order requests design, section 4)."""
from __future__ import annotations

import httpx

SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT_S = 10


class TurnstileUnavailable(Exception):
    """Cloudflare could not be asked (network error, timeout, 5xx or an unreadable answer): try again later."""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S)


async def verify(secret: str, token: str, ip: str | None) -> bool:
    """Whether Cloudflare accepts the widget's token. Only a literal `"success": true` counts."""
    form = {"secret": secret, "response": token} | ({"remoteip": ip} if ip else {})
    try:
        async with _client() as c:
            r = await c.post(SITEVERIFY, data=form)
        if r.status_code >= 500:
            raise TurnstileUnavailable(f"siteverify answered {r.status_code}")
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise TurnstileUnavailable(str(e)) from e
    return isinstance(body, dict) and body.get("success") is True
