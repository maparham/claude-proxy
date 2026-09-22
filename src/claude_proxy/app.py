from __future__ import annotations

import time
import logging

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .config import Config
from .credentials import make_backend
from .db import get_conn, init_db, insert_request
from .forwarder import filter_request_headers, filter_response_headers, should_forward
from .auth import authenticate
from .meter import SSEMeter, parse_non_streaming

logger = logging.getLogger("claude_proxy")

# Global httpx client: HTTP/1.1 only to avoid H2 stall (spec 4)
_upstream_client: httpx.AsyncClient | None = None


def get_upstream_client() -> httpx.AsyncClient:
    global _upstream_client
    if _upstream_client is None:
        _upstream_client = httpx.AsyncClient(http1=True, http2=False, timeout=300.0)
    return _upstream_client


def _extract_session_id(headers: dict[str, str]) -> str | None:
    low = {k.lower(): v for k, v in headers.items()}
    for key in ("x-session-id", "anthropic-session-id", "session-id", "x-stanza-session-id", "x-claude-session-id", "x-session_id"):
        if key in low and low[key]:
            return low[key][:128]
    for k, v in low.items():
        if "session" in k and v:
            return v[:128]
    return None


def _extract_client_version(headers: dict[str, str]) -> str | None:
    low = {k.lower(): v for k, v in headers.items()}
    for key in ("x-stanza-client-version", "anthropic-version", "x-client-version", "client-version"):
        if key in low and low[key]:
            return low[key][:128]
    ua = low.get("user-agent")
    if ua and "claude" in ua.lower():
        return ua[:128]
    return ua[:128] if ua else None


def _extract_upstream_request_id(resp_headers: dict[str, str], meter_id: str | None) -> str | None:
    if meter_id:
        return meter_id[:128]
    low = {k.lower(): v for k, v in resp_headers.items()}
    for key in ("x-request-id", "request-id", "anthropic-request-id", "x-amz-request-id", "request_id"):
        if key in low and low[key]:
            return low[key][:128]
    return None


def create_app(config: Config | None = None, db_conn=None) -> FastAPI:
    cfg = config or Config.load()
    app = FastAPI(title="claude-proxy")

    # DB
    if db_conn is None:
        db_conn = init_db(cfg.db.path)
    app.state.db_conn = db_conn
    app.state.config = cfg

    # Backend — OAuth only (spec 5.1, api_key removed per user request)
    try:
        backend = make_backend(cfg, cfg.db.path)
    except Exception as e:
        backend = None
        logger.error("backend init failed: %s", e)
    app.state.backend = backend

    @app.get("/health")
    async def health():
        return {"ok": True}

    # Web dashboard + JSON API (Phase 7 MVP, spec 10)
    try:
        from .web import register_web
        register_web(app, cfg)
    except Exception as e:
        logger.warning("web register failed: %s", e)

    # Catch-all proxy handler for /v1/* (spec 4) — must run after auth/limits
    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def proxy_v1(request: Request, path: str):
        return await handle_proxy(request, f"/v1/{path}")

    # Also handle count_tokens and other non-streaming under /v1
    # Any non-/v1 path on proxy port is 404 and never upstream (spec 4)
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def catch_all(request: Request, path: str):
        full = "/" + path
        if should_forward(full):
            return await handle_proxy(request, full)
        return JSONResponse(status_code=404, content={"type": "error", "error": {"type": "not_found_error", "message": f"Not found: {full}"}})

    async def handle_proxy(request: Request, full_path: str):
        started_at = int(time.time())
        # 1. Auth — spec 4.2
        try:
            user = authenticate(request.app.state.db_conn, request.headers.get("authorization"))
        except Exception as e:
            # FastAPI HTTPException handling
            from fastapi import HTTPException

            if isinstance(e, HTTPException):
                # Record rejection
                try:
                    insert_request(
                        request.app.state.db_conn,
                        user_id=None,
                        started_at=started_at,
                        ended_at=int(time.time()),
                        method=request.method,
                        path=full_path,
                        status=e.status_code,
                        stream=0,
                        complete=1,
                        error_type="authentication_error",
                        rejected_by="auth",
                    )
                except Exception:
                    pass
                detail = e.detail if isinstance(e.detail, dict) else {"type": "error", "error": {"type": "authentication_error", "message": str(e.detail)}}
                return JSONResponse(status_code=e.status_code, content=detail)
            raise

        # 2. Limits — Phase 5 (spec 8)
        # Read body early for limit checks (model extraction, count_tokens bypass)
        try:
            from .limits import check_limits
        except Exception as e:
            logger.warning("limits import failed: %s", e)
            check_limits = None

        # Body is needed for limits; read now (also used for upstream)
        body_for_limits = await request.body()
        # Use same body for upstream to avoid double read (request.body() is cached after first call)
        body = body_for_limits
        # Filter headers first for session extraction but defer upstream send until after limits
        # Evaluate limits before forwarding
        if check_limits is not None:
            allowed, status_code, error_body, retry_after, rejected_by = check_limits(
                request.app.state.db_conn, cfg, dict(user) if hasattr(user, "keys") else user, body_for_limits, full_path
            )
            if not allowed:
                try:
                    insert_request(
                        request.app.state.db_conn,
                        user_id=user["id"],
                        started_at=started_at,
                        ended_at=int(time.time()),
                        method=request.method,
                        path=full_path,
                        status=status_code,
                        stream=0,
                        complete=1,
                        error_type="rate_limit_error" if status_code == 429 else "permission_error",
                        rejected_by=rejected_by,
                        input_tokens=0,
                        output_tokens=0,
                    )
                except Exception:
                    pass
                headers = {}
                if retry_after is not None:
                    headers["retry-after"] = str(retry_after)
                    headers["Retry-After"] = str(retry_after)
                return JSONResponse(status_code=status_code, content=error_body, headers=headers)
        else:
            # Fallback body already read
            body = body_for_limits
        # 3. Build upstream request (spec 4.4)
        # If body was already read via limits path, reuse; otherwise ensure body exists
        if 'body' not in locals() or body is None:
            body = await request.body()
        backend = request.app.state.backend
        if backend is None:
            return JSONResponse(
                status_code=503,
                content={"type": "error", "error": {"type": "api_error", "message": "Credential not linked. Admin must run `claude-proxy login`."}},
            )

        # body already read for limits (cached by Starlette)
        # Filter headers
        upstream_headers = filter_request_headers(dict(request.headers))
        # Inject backend auth — merge anthropic-beta correctly (spec 5.1)
        try:
            bh = await backend.upstream_headers()
        except RuntimeError as e:
            if "needs_login" in str(e) or "no credential" in str(e):
                return JSONResponse(
                    status_code=503,
                    content={"type": "error", "error": {"type": "api_error", "message": "OAuth needs re-login. Admin must run `claude-proxy login`."}},
                )
            logger.error("upstream_headers failed: %s", e)
            return JSONResponse(status_code=503, content={"type": "error", "error": {"type": "api_error", "message": "Credential backend error"}})
        except Exception as e:
            logger.error("upstream_headers failed: %s", e)
            return JSONResponse(status_code=503, content={"type": "error", "error": {"type": "api_error", "message": "Credential backend error"}})
        # bh contains Authorization + anthropic-beta; merge beta with client's value
        beta_flag = bh.pop("anthropic-beta", None)
        upstream_headers.update(bh)
        if beta_flag:
            existing = upstream_headers.get("anthropic-beta") or upstream_headers.get("Anthropic-Beta")
            # Normalize header name to anthropic-beta
            upstream_headers.pop("Anthropic-Beta", None)
            if existing and beta_flag not in existing:
                upstream_headers["anthropic-beta"] = f"{existing}, {beta_flag}"
            elif not existing:
                upstream_headers["anthropic-beta"] = beta_flag
        # Ensure host is upstream host, not proxy host; httpx sets it from URL
        upstream_headers.pop("host", None)
        upstream_headers.pop("Host", None)

        upstream_base = cfg.upstream.base_url.rstrip("/")
        query = f"?{request.url.query}" if request.url.query else ""
        upstream_url = f"{upstream_base}{full_path}{query}"

        # Log with prefix only, never raw key
        logger.info("proxy %s %s user=%s prefix=%s upstream=%s", request.method, full_path, user["name"], user["key_prefix"], upstream_base)

        client = get_upstream_client()
        # Forward
        try:
            upstream_req = client.build_request(
                method=request.method,
                url=upstream_url,
                headers=upstream_headers,
                content=body,
            )
            # Stream response back to client without buffering (spec 4.5)
            upstream_resp = await client.send(upstream_req, stream=True)
        except httpx.ConnectError as e:
            try:
                insert_request(
                    request.app.state.db_conn,
                    user_id=user["id"],
                    started_at=started_at,
                    ended_at=int(time.time()),
                    method=request.method,
                    path=full_path,
                    status=502,
                    stream=0,
                    complete=0,
                    error_type="api_error",
                )
            except Exception:
                pass
            return JSONResponse(status_code=502, content={"type": "error", "error": {"type": "api_error", "message": "Upstream unreachable"}})
        except Exception as e:
            logger.error("upstream build/send failed: %s", e)
            return JSONResponse(status_code=502, content={"type": "error", "error": {"type": "api_error", "message": "Upstream error"}})

        # Handle 401 retry once before first byte — Phase 3 will implement single-flight; for now just pass through if api_key backend
        if upstream_resp.status_code == 401 and backend is not None:
            try:
                should_retry = await backend.on_unauthorized()
            except Exception:
                should_retry = False
            if should_retry:
                await upstream_resp.aclose()
                # Retry once — re-inject fresh headers (single-flight already done in on_unauthorized)
                try:
                    bh2 = await backend.upstream_headers()
                except Exception:
                    bh2 = {}
                beta2 = bh2.pop("anthropic-beta", None)
                upstream_headers.update(bh2)
                if beta2:
                    existing = upstream_headers.get("anthropic-beta")
                    if existing and beta2 not in existing:
                        upstream_headers["anthropic-beta"] = f"{existing}, {beta2}"
                    elif not existing:
                        upstream_headers["anthropic-beta"] = beta2
                upstream_req = client.build_request(method=request.method, url=upstream_url, headers=upstream_headers, content=body)
                upstream_resp = await client.send(upstream_req, stream=True)

        # Capture headers for quota snapshots (Phase 4 will persist)
        # Currently just pass through

        # Extract per-request metadata bevor streaming
        session_id = _extract_session_id(dict(request.headers))
        client_version = _extract_client_version(dict(request.headers))

        # Stream body to client with metering (spec 6)
        from fastapi.responses import StreamingResponse

        status = upstream_resp.status_code
        raw_resp_headers = dict(upstream_resp.headers)
        resp_headers = filter_response_headers(raw_resp_headers)

        is_stream = "text/event-stream" in resp_headers.get("content-type", "").lower()
        # count_tokens is never metered for tokens per spec 8 — but we still record path with zeros
        is_count_tokens = "count_tokens" in full_path

        async def stream_gen():
            meter = SSEMeter() if is_stream and not is_count_tokens else None
            non_stream_buf = bytearray() if not is_stream and not is_count_tokens else None
            stream_complete = True
            # For non-stream json, we still stream but accumulate for metering
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    # Meter observes copy in parallel, never delays (spec 4.5)
                    if meter is not None and not meter.result.meter_error:
                        try:
                            meter.feed(chunk)
                        except Exception as e:
                            logger.warning("meter feed failed: %s", e)
                            meter.result.meter_error = True
                            meter.result.meter_error_detail = str(e)[:200]
                    if non_stream_buf is not None:
                        # bounded accumulate for non-stream json (typically < 100KB)
                        if len(non_stream_buf) < 5 * 1024 * 1024:
                            non_stream_buf.extend(chunk)
                    yield chunk
            except Exception as e:
                stream_complete = False
                logger.warning("stream interrupted: %s", e)
            finally:
                ended = int(time.time())
                # Build meter result
                try:
                    if is_count_tokens:
                        # spec 6.2: count_tokens -> zero tokens, complete true, stream flag as detected
                        mr = None
                        input_t = output_t = cc_t = cc5 = cc1 = cr_t = 0
                        model = None
                        up_id = _extract_upstream_request_id(raw_resp_headers, None)
                        complete_flag = 1 if stream_complete else 0
                        meter_err = 0
                    elif meter is not None:
                        mr = meter.finalize()
                        input_t = mr.input_tokens
                        output_t = mr.output_tokens
                        cc_t = mr.cache_creation_tokens
                        cc5 = mr.cache_creation_5m
                        cc1 = mr.cache_creation_1h
                        cr_t = mr.cache_read_tokens
                        model = mr.model
                        up_id = _extract_upstream_request_id(raw_resp_headers, mr.upstream_request_id)
                        # complete flag: only true if message_stop seen AND stream not interrupted
                        complete_flag = 1 if (mr.complete and stream_complete) else 0
                        meter_err = 1 if mr.meter_error else 0
                        if mr.meter_error:
                            logger.info("meter_error path=%s detail=%s", full_path, mr.meter_error_detail)
                    elif non_stream_buf is not None:
                        mr = parse_non_streaming(bytes(non_stream_buf), full_path)
                        input_t = mr.input_tokens
                        output_t = mr.output_tokens
                        cc_t = mr.cache_creation_tokens
                        cc5 = mr.cache_creation_5m
                        cc1 = mr.cache_creation_1h
                        cr_t = mr.cache_read_tokens
                        model = mr.model
                        up_id = _extract_upstream_request_id(raw_resp_headers, mr.upstream_request_id)
                        complete_flag = 1 if (mr.complete and stream_complete) else 0
                        meter_err = 1 if mr.meter_error else 0
                    else:
                        # fallback (e.g., stream but empty)
                        input_t = output_t = cc_t = cc5 = cc1 = cr_t = 0
                        model = None
                        up_id = _extract_upstream_request_id(raw_resp_headers, None)
                        complete_flag = 1 if stream_complete else 0
                        meter_err = 0

                    # For 4xx/5xx responses there is no usage — still record with zeros and error_type
                    error_type = None if status < 400 else f"upstream_{status}"
                    # If stream response had error and no metering, still mark complete accordingly

                    insert_request(
                        request.app.state.db_conn,
                        user_id=user["id"],
                        started_at=started_at,
                        ended_at=ended,
                        method=request.method,
                        path=full_path,
                        model=model,
                        status=status,
                        stream=1 if is_stream else 0,
                        complete=complete_flag,
                        input_tokens=input_t,
                        output_tokens=output_t,
                        cache_creation_tokens=cc_t,
                        cache_creation_5m=cc5,
                        cache_creation_1h=cc1,
                        cache_read_tokens=cr_t,
                        upstream_request_id=up_id,
                        session_id=session_id,
                        client_version=client_version,
                        error_type=error_type,
                        meter_error=meter_err,
                        rejected_by=None,
                    )
                except Exception as e:
                    logger.error("insert_request failed: %s", e)
                await upstream_resp.aclose()

        # Do not forward content-length/content-encoding (stripped in filter_response_headers); StreamingResponse will use chunked
        return StreamingResponse(stream_gen(), status_code=status, headers=resp_headers, media_type=resp_headers.get("content-type"))

    return app
