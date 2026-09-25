"""The proxy listener (spec section 4). Serves only /health and /v1/*; the dashboard is a separate app."""
from __future__ import annotations

import json
import logging
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import limits, quota, titles
from .auth import AuthError, authenticate, client_status
from .config import Route
from .credentials import NeedsLogin, RefreshUnavailable
from .db import insert_request, set_session_title
from .forwarder import (filter_request_headers, filter_response_headers, merge_beta, session_id, should_forward,
                        strip_account_headers)
from .gateway import Gateway
from .meter import SSEMeter, parse_non_streaming

logger = logging.getLogger("claude_proxy")

ROUTED_PATHS = ("/v1/messages", "/v1/messages/count_tokens")
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


# A non-admin never hears about the subscription behind the gateway; these replace what would tell them.
UNAVAILABLE = "The gateway can't serve Claude requests right now. Try again later, or ask the gateway admin."


def api_error(status: int, error_type: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"type": "error", "error": {"type": error_type, "message": message}},
                        headers=headers)


def _model_of(body: bytes) -> tuple[str | None, dict | None]:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    model = data.get("model")
    return (model if isinstance(model, str) else None), data


class Upstream:
    """Where one request goes and with which credential."""

    def __init__(self, gw: Gateway, route: Route | None):
        self.gw = gw
        self.route = route
        self.provider = route.name if route else "anthropic"
        self.token: str | None = None

    async def headers(self, client_headers: dict[str, str]) -> dict[str, str]:
        h = filter_request_headers(client_headers)
        h["accept-encoding"] = "gzip"   # a coding httpx always decodes; the body is re-framed downstream
        if self.route is None:
            creds = await self.gw.backend.upstream_headers()
            self.token = creds["authorization"].split(" ", 1)[1]
            h["authorization"] = creds["authorization"]
            h["anthropic-beta"] = merge_beta(h.get("anthropic-beta"), creds["anthropic-beta"])
            return h
        for name in self.route.strip_headers:
            h.pop(name.lower(), None)
        key = self.route.api_key()
        if self.route.auth_header == "x-api-key":
            h["x-api-key"] = key
        else:
            h["authorization"] = f"Bearer {key}"
        return h

    def url(self, path: str, query: str) -> str:
        base = (self.route.base_url if self.route else self.gw.cfg.upstream.base_url).rstrip("/")
        return f"{base}{path}{'?' + query if query else ''}"


def create_app(gw: Gateway) -> FastAPI:
    app = FastAPI(title="claude-proxy", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gw = gw

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.api_route("/{path:path}", methods=METHODS)
    async def proxy(request: Request, path: str):
        return await handle(gw, request, "/" + path)

    return app


async def handle(gw: Gateway, request: Request, path: str) -> Response:
    conn, cfg = gw.conn, gw.cfg
    started = time.time()
    if not should_forward(path):
        return api_error(404, "not_found_error", f"Not found: {path}")

    base = {"started_at": started, "method": request.method, "path": path}

    def record(**kw) -> None:
        try:
            insert_request(conn, **{**base, "ended_at": time.time(), **kw})
        except Exception:
            logger.exception("could not record request")

    try:
        user = authenticate(conn, request.headers)
    except AuthError as e:
        record(user_id=None, status=e.status, stream=0, complete=1, error_type=e.body["error"]["type"], rejected_by="auth")
        return JSONResponse(status_code=e.status, content=e.body)
    base["user_id"] = user["id"]
    is_admin = user["role"] == "admin"
    base["session_id"] = session_id(request.headers)
    base["client_version"] = (request.headers.get("user-agent") or "")[:128] or None

    body = await request.body()
    model, data = _model_of(body) if body else (None, None)
    route = cfg.route_for(model) if request.method == "POST" and path in ROUTED_PATHS else None
    upstream = Upstream(gw, route)
    base["provider"] = upstream.provider
    base["model"] = model
    base["requested_model"] = model
    wants_title = bool(base["session_id"]) and path == "/v1/messages" and titles.is_title_request(data)

    if request.method == "POST" and path in ROUTED_PATHS and model is None:
        # Model allow-lists, scoped limits and routing all need the model; never forward a body the gateway can't read.
        record(status=400, stream=0, complete=1, error_type="gateway_bad_request", rejected_by="request")
        return api_error(400, "invalid_request_error", "The gateway could not read a string `model` from the JSON request body.")

    decision = limits.evaluate(conn, cfg, user["id"], model, path)
    if decision:
        record(status=decision.status, stream=0, complete=1, error_type=decision.body["error"]["type"], rejected_by=decision.kind)
        logger.info("limit reject user=%s kind=%s", user["name"], decision.kind)
        return JSONResponse(status_code=decision.status, content=decision.body,
                            headers={"retry-after": str(decision.retry_after)} if decision.retry_after else None)

    if route is not None:
        if not route.api_key():
            record(status=503, stream=0, complete=1, error_type="gateway_route_unconfigured")
            return api_error(503, "api_error", f"Model {model!r} is routed to {route.name}, but {route.api_key_env} is not set on the gateway.")
        dropped = [f for f in route.drop_body_fields if f in data]
        if route.upstream_model(model) != model or dropped:
            data["model"] = route.upstream_model(model)
            for f in dropped:
                del data[f]
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()

    client_headers = dict(request.headers)
    query = request.url.query

    async def send() -> httpx.Response:
        headers = await upstream.headers(client_headers)
        req = gw.http.build_request(request.method, upstream.url(path, query), headers=headers, content=body)
        return await gw.http.send(req, stream=True)

    try:
        resp = await send()
        # Refresh-and-retry once on 401. Nothing has been sent to the client yet (spec 5.1).
        if resp.status_code == 401 and route is None and await gw.backend.on_unauthorized(upstream.token):
            await resp.aclose()
            resp = await send()
    except RefreshUnavailable:
        record(status=503, stream=0, complete=1, error_type="gateway_refresh_unavailable")
        if not is_admin:
            return api_error(503, "api_error", UNAVAILABLE)
        return api_error(503, "api_error", "The gateway could not renew its Claude subscription token just now "
                         "(a temporary error at Anthropic's sign-in service). It retries automatically; try again in a minute.")
    except NeedsLogin as e:
        record(status=503, stream=0, complete=1, error_type="gateway_needs_login")
        if not is_admin:
            return api_error(503, "api_error", UNAVAILABLE)
        return api_error(503, "api_error", f"The gateway's Claude subscription login needs renewing: the admin must run `claude-proxy login` ({e}).")
    except httpx.HTTPError as e:
        logger.warning("upstream %s unreachable: %s", upstream.provider, e)
        record(status=502, stream=0, complete=0, error_type="gateway_upstream_unreachable")
        return api_error(502, "api_error", f"The gateway could not reach {upstream.provider}.")

    if route is None:
        try:
            quota.record(conn, quota.parse_headers(resp.headers))
        except Exception:
            logger.exception("could not record quota headers")

    status = client_status(request.headers, resp.status_code)
    resp_headers = filter_response_headers(dict(resp.headers))
    error_type = quota.classify_429(resp.headers) if status == 429 else None

    if not is_admin:
        resp_headers = strip_account_headers(resp_headers)
        # Anthropic's own 429/401/403 bodies speak of the account and its login; say it the gateway's way.
        if route is None and resp.status_code in (401, 403, 429):
            await resp.aclose()
            record(status=resp.status_code, stream=0, complete=1, upstream_request_id=resp.headers.get("request-id"),
                   error_type=error_type or f"upstream_{resp.status_code}")
            if resp.status_code != 429:
                return api_error(503, "api_error", UNAVAILABLE)
            retry = resp.headers.get("retry-after")
            wait = f" Try again in {limits.human(int(retry))}." if retry and retry.isdigit() else " Try again later."
            return api_error(429, "rate_limit_error", "Usage limit reached." + wait, headers={"retry-after": retry} if retry else None)

    if request.method == "GET" and path == "/v1/models" and status == 200 and route is None:
        return await _models_with_routes(gw, resp, resp_headers, record)

    is_stream = "text/event-stream" in resp.headers.get("content-type", "")

    async def relay():
        meter = SSEMeter(collect_text=wants_title) if is_stream else None
        buf = bytearray() if not is_stream else None
        finished = False
        try:
            async for chunk in resp.aiter_bytes():
                if meter is not None:
                    try:
                        meter.feed(chunk)
                    except Exception as e:   # metering never affects forwarding
                        meter.result.meter_error = True
                        meter.result.meter_error_detail = str(e)[:200]
                elif len(buf) < 8 * 1024 * 1024:
                    buf.extend(chunk)
                yield chunk
            finished = True
        finally:
            await resp.aclose()
            mr = meter.finalize() if meter is not None else parse_non_streaming(bytes(buf), path, collect_text=wants_title)
            record(
                model=mr.model or base["model"], status=resp.status_code, stream=1 if is_stream else 0,
                complete=1 if finished and (mr.complete or not is_stream) else 0,
                input_tokens=mr.input_tokens, output_tokens=mr.output_tokens,
                cache_creation_tokens=mr.cache_creation_tokens, cache_creation_5m=mr.cache_creation_5m,
                cache_creation_1h=mr.cache_creation_1h, cache_read_tokens=mr.cache_read_tokens,
                upstream_request_id=mr.upstream_request_id or resp.headers.get("request-id"),
                error_type=error_type or mr.error_type or (f"upstream_{resp.status_code}" if resp.status_code >= 400 else None),
                meter_error=1 if mr.meter_error else 0,
            )
            title = titles.parse_title(mr.text) if wants_title and finished and resp.status_code == 200 else None
            if title:
                try:
                    set_session_title(conn, user["id"], base["session_id"], title)
                except Exception:
                    logger.exception("could not record session title")

    return StreamingResponse(relay(), status_code=status, headers=resp_headers)


async def _models_with_routes(gw: Gateway, resp: httpx.Response, headers: dict, record) -> Response:
    raw = await resp.aread()
    await resp.aclose()
    try:
        data = json.loads(raw)
        ids = {m.get("id") for m in data.get("data", [])}
        for route in gw.cfg.routes:
            for name in route.model_map:
                if name not in ids:
                    data["data"].append({"type": "model", "id": name, "display_name": f"{name} (via {route.name})",
                                         "created_at": "2026-01-01T00:00:00Z"})
        raw = json.dumps(data).encode()
    except (ValueError, AttributeError, TypeError):
        pass
    record(status=200, stream=0, complete=1)
    headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
    return Response(raw, status_code=200, headers=headers, media_type="application/json")
