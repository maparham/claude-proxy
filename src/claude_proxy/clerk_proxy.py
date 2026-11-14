"""Clerk's Frontend API through the dashboard (Clerk "proxy" mode, design 2026-11-14-iran-hosting).

From Iran, Clerk's own host (on Cloudflare) freezes partway through larger responses, so the browser talks to
`<dashboard>/__clerk/...` and the gateway, outside Iran, forwards to Clerk's Frontend API with the headers Clerk's
proxy mode requires. The upstream host is fixed: no part of the request chooses where it goes. Only Clerk's own
cookies are passed on, never the dashboard's session or an Authorization header."""
from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI, Request
from starlette.responses import PlainTextResponse, StreamingResponse

logger = logging.getLogger(__name__)

UPSTREAM_HOST = "frontend-api.clerk.dev"
PREFIX = "/__clerk"
TIMEOUT_S = 30.0
MAX_BODY = 1_000_000
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Hop-by-hop, or set here by the proxy itself.
DROP_REQUEST = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
                "proxy-connection", "content-length", "authorization", "cookie", "x-forwarded-for", "x-forwarded-host",
                "x-forwarded-proto", "x-real-ip", "forwarded", "clerk-proxy-url", "clerk-secret-key"}
DROP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length"}


def _clerk_cookies(header: str) -> str:
    """Only Clerk's own cookies (they all start with "__"); the dashboard's session, the referral code and the rest stay."""
    return "; ".join(p.strip() for p in header.split(";") if p.strip().startswith("__"))


async def _body(request: Request) -> bytes | None:
    """The request body, or None when it is larger than MAX_BODY (a sign-in form is a few hundred bytes)."""
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _relay(resp: httpx.Response):
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
    finally:
        await resp.aclose()


def mount(app: FastAPI, proxy_url: str, secret: str, http: httpx.AsyncClient) -> None:
    proxy_url = proxy_url.rstrip("/")

    @app.api_route(PREFIX + "/{path:path}", methods=METHODS, include_in_schema=False)
    async def clerk_frontend_api(path: str, request: Request):
        # The path as the browser sent it (still percent-encoded), on Clerk's host; the host itself is fixed.
        raw = request.scope.get("raw_path") or request.url.path.encode()
        rest = raw[len(PREFIX):] if raw.startswith(PREFIX.encode()) else b"/" + path.encode()
        query = request.scope.get("query_string") or b""
        url = httpx.URL(f"https://{UPSTREAM_HOST}").copy_with(
            raw_path=(rest if rest.startswith(b"/") else b"/" + rest) + (b"?" + query if query else b""))
        # Cloudflare's own headers (cf-*, cdn-loop) arrive when the dashboard is reached through cloudflared; Clerk's host
        # is on Cloudflare too and must not see them.
        headers = [(k, v) for k, v in request.headers.items()
                   if k.lower() not in DROP_REQUEST and not k.lower().startswith("cf-") and k.lower() != "cdn-loop"]
        cookie = _clerk_cookies(request.headers.get("cookie", ""))
        if cookie:
            headers.append(("cookie", cookie))
        headers += [("clerk-proxy-url", proxy_url), ("clerk-secret-key", secret),
                    ("x-forwarded-for", request.client.host if request.client else "")]
        body = await _body(request)
        if body is None:
            return PlainTextResponse("Request too large.", status_code=413)
        req = http.build_request(request.method, url, headers=headers, content=body, timeout=TIMEOUT_S)
        try:
            resp = await http.send(req, stream=True)
        except httpx.TimeoutException:
            logger.warning("Clerk's Frontend API timed out for %s %s", request.method, url.path)
            return PlainTextResponse("Clerk did not answer in time.", status_code=504)
        except httpx.HTTPError as e:
            logger.warning("Clerk's Frontend API unreachable for %s %s: %s", request.method, url.path, e)
            return PlainTextResponse("Clerk is unreachable.", status_code=502)
        out = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in DROP_RESPONSE]
        r = StreamingResponse(_relay(resp), status_code=resp.status_code)
        r.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in out]
        return r
