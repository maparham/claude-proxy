"""Clerk's Frontend API through the dashboard (Clerk "proxy" mode, design 2026-11-14-iran-hosting).

From Iran, Clerk's own host (on Cloudflare) freezes partway through larger responses, so the browser talks to
`<dashboard>/__clerk/...` and the gateway, outside Iran, forwards to Clerk's Frontend API with the headers Clerk's
proxy mode requires. The upstream host is fixed: no part of the request chooses where it goes. The dashboard's own
session cookie and any Authorization header are not passed on."""
from __future__ import annotations

import httpx
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

UPSTREAM_HOST = "frontend-api.clerk.dev"
PREFIX = "/__clerk"
TIMEOUT_S = 30.0
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Hop-by-hop, or set here by the proxy itself.
DROP_REQUEST = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
                "proxy-connection", "content-length", "authorization", "cookie", "x-forwarded-for", "x-forwarded-host",
                "x-forwarded-proto", "x-real-ip", "forwarded", "clerk-proxy-url", "clerk-secret-key"}
DROP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length"}


def _cookies_without(header: str, name: str) -> str:
    return "; ".join(p.strip() for p in header.split(";") if p.strip() and p.strip().split("=", 1)[0] != name)


def mount(app: FastAPI, proxy_url: str, secret: str, http: httpx.AsyncClient, own_cookie: str) -> None:
    proxy_url = proxy_url.rstrip("/")

    @app.api_route(PREFIX + "/{path:path}", methods=METHODS, include_in_schema=False)
    async def clerk_frontend_api(path: str, request: Request):
        url = httpx.URL(scheme="https", host=UPSTREAM_HOST, path="/" + path.lstrip("/"),
                        query=request.url.query.encode())
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in DROP_REQUEST]
        cookie = _cookies_without(request.headers.get("cookie", ""), own_cookie)
        if cookie:
            headers.append(("cookie", cookie))
        headers += [("clerk-proxy-url", proxy_url), ("clerk-secret-key", secret),
                    ("x-forwarded-for", request.client.host if request.client else "")]
        req = http.build_request(request.method, url, headers=headers, content=await request.body(), timeout=TIMEOUT_S)
        resp = await http.send(req, stream=True)
        out = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in DROP_RESPONSE]
        r = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
        r.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in out]
        return r
