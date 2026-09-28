"""Regressions for the 2026-09-27 review: in-flight requests, the Batches API, window row sets, body size."""
import asyncio

import pytest
from fastapi.responses import StreamingResponse

from claude_proxy import app as app_mod
from claude_proxy import limits
from claude_proxy.app import inflight_of, create_app
from claude_proxy.config import Config
from claude_proxy.db import create_user
from tests.conftest import asgi_client, make_gateway, seed_oauth
from tests.test_limits import NOW, check, req, set_limit

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def env(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    return conn, Config(), uid


@pytest.fixture
def setup(cfg, db, anthropic):
    conn = db[1]
    uid, key = create_user(conn, "alice")
    seed_oauth(conn)
    return make_gateway(cfg, conn, anthropic), conn, uid, {"Authorization": f"Bearer {key}"}


def last_request(conn):
    return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()


def blocking_upstream(anthropic, release: asyncio.Event):
    """A streaming upstream that holds the response open until `release` is set."""
    async def gen():
        await release.wait()
        yield b'{"id":"msg_1","type":"message","role":"assistant","model":"claude-sonnet-5","content":[],"stop_reason":"end_turn",' \
              b'"usage":{"input_tokens":10,"output_tokens":5}}'
    anthropic.default = lambda req: StreamingResponse(gen(), media_type="application/json")


# 1. In-flight requests count toward request limits and a per-user concurrency cap

def test_inflight_requests_count_toward_request_limits(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "requests_minute", 2, "count")
    req(conn, uid, NOW - 10)
    assert limits.evaluate(conn, cfg, uid, "claude-sonnet-5", "/v1/messages", now=NOW, inflight=0) is None
    d = limits.evaluate(conn, cfg, uid, "claude-sonnet-5", "/v1/messages", now=NOW, inflight=1)
    assert d is not None and d.status == 429 and d.kind == "requests_minute"
    assert "2 of 2 requests" in d.body["error"]["message"]


def test_inflight_does_not_count_toward_token_or_cost_limits(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "cost_daily", 1.0, "usd")
    set_limit(conn, uid, "tokens_daily", 1000, "raw")
    req(conn, uid, NOW - 10, i=1)
    assert limits.evaluate(conn, cfg, uid, "claude-sonnet-5", "/v1/messages", now=NOW, inflight=5) is None


def test_max_inflight_is_a_429_with_the_limits_shape(env):
    conn, cfg, uid = env
    assert limits.evaluate(conn, cfg, uid, "claude-sonnet-5", "/v1/messages", now=NOW, inflight=7) is None
    d = limits.evaluate(conn, cfg, uid, "claude-sonnet-5", "/v1/messages", now=NOW, inflight=8)   # default cap: 8
    assert d is not None and (d.status, d.kind) == (429, "max_inflight")
    assert d.body["type"] == "error" and d.body["error"]["type"] == "rate_limit_error"
    assert d.retry_after
    cfg.limits.max_inflight = 2
    assert limits.evaluate(conn, cfg, uid, "claude-sonnet-5", "/v1/messages", now=NOW, inflight=1) is None
    assert limits.evaluate(conn, cfg, uid, "claude-sonnet-5", "/v1/messages", now=NOW, inflight=2).kind == "max_inflight"
    # count_tokens is never metered, so the cap does not hold it back either
    assert limits.evaluate(conn, cfg, uid, "claude-sonnet-5", "/v1/messages/count_tokens", now=NOW, inflight=99) is None


def test_max_inflight_config_key(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[limits]\nmax_inflight = 3\n")
    assert Config.load(str(p)).limits.max_inflight == 3
    assert Config().limits.max_inflight == 8


async def test_parallel_requests_are_seen_by_request_limits(setup, anthropic):
    gw, conn, uid, h = setup
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,'requests_minute','*','1','count',0)", (uid,))
    release = asyncio.Event()
    blocking_upstream(anthropic, release)
    async with asgi_client(create_app(gw)) as c:
        first = asyncio.create_task(c.post("/v1/messages", json=MSG, headers=h))
        await asyncio.sleep(0.05)
        second = await c.post("/v1/messages", json=MSG, headers=h)
        release.set()
        r1 = await first
    assert r1.status_code == 200
    assert second.status_code == 429 and second.json()["error"]["type"] == "rate_limit_error"
    assert len(anthropic.calls) == 1
    rows = conn.execute("SELECT status, rejected_by FROM requests ORDER BY id").fetchall()
    assert sorted(tuple(r) for r in rows) == [(200, None), (429, "requests_minute")]


async def test_concurrency_cap_holds_while_a_request_is_in_flight_and_releases_after(setup, anthropic):
    gw, conn, uid, h = setup
    gw.cfg.limits.max_inflight = 1
    release = asyncio.Event()
    blocking_upstream(anthropic, release)
    async with asgi_client(create_app(gw)) as c:
        first = asyncio.create_task(c.post("/v1/messages", json=MSG, headers=h))
        await asyncio.sleep(0.05)
        second = await c.post("/v1/messages", json=MSG, headers=h)
        release.set()
        r1 = await first
        third = await c.post("/v1/messages", json=MSG, headers=h)
    assert r1.status_code == 200
    assert second.status_code == 429 and second.headers.get("retry-after")
    assert third.status_code == 200
    assert len(anthropic.calls) == 2
    rejected = conn.execute("SELECT rejected_by FROM requests WHERE status=429").fetchall()
    assert [r[0] for r in rejected] == ["max_inflight"]


async def test_inflight_is_released_when_upstream_fails(setup, anthropic):
    gw, conn, uid, h = setup
    gw.cfg.limits.max_inflight = 1
    gw.cfg.upstream.base_url = "http://nowhere.invalid"
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
        assert r.status_code == 502
        r = await c.post("/v1/messages", json=MSG, headers=h)
        assert r.status_code == 502     # not 429: the failed request no longer counts as in flight


async def test_inflight_is_released_when_the_handler_crashes(setup, anthropic, monkeypatch):
    """An unexpected exception after the slot is taken (here: building the upstream request) must give it back,
    or the user stays at max_inflight until the process restarts."""
    gw, conn, uid, h = setup
    gw.cfg.limits.max_inflight = 1

    def boom(*a, **kw):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(gw.http, "build_request", boom)
    app = create_app(gw)
    async with asgi_client(app) as c:
        with pytest.raises(RuntimeError):
            await c.post("/v1/messages", json=MSG, headers=h)
    assert inflight_of(gw).get(uid) == 0
    monkeypatch.undo()
    async with asgi_client(app) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
        assert r.status_code == 200     # not 429: the crashed request no longer counts


# 2. The Batches API can't be metered, so it is refused before limits

@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/messages/batches"),
    ("GET", "/v1/messages/batches"),
    ("GET", "/v1/messages/batches/msgbatch_0123"),
    ("POST", "/v1/messages/batches/msgbatch_0123/cancel"),
])
async def test_batches_api_is_refused_as_unmetered(setup, anthropic, method, path):
    gw, conn, uid, h = setup
    body = {"requests": [{"custom_id": "a", "params": {**MSG, "model": "claude-opus-5"}}]} if method == "POST" else None
    async with asgi_client(create_app(gw)) as c:
        r = await c.request(method, path, json=body, headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"
    assert "Batches API" in r.json()["error"]["message"]
    assert anthropic.calls == []
    row = last_request(conn)
    assert (row["user_id"], row["status"], row["rejected_by"]) == (uid, 403, "unmetered_path")


async def test_batches_refusal_comes_before_limits(setup, anthropic):
    gw, conn, uid, h = setup
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,'requests_daily','*','0','count',0)", (uid,))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages/batches", json={"requests": []}, headers=h)
    assert r.status_code == 403 and last_request(conn)["rejected_by"] == "unmetered_path"


# 3. _window_start and _window_state look at the same rows

def test_model_less_rows_neither_open_nor_fill_a_window(env):
    conn, cfg, uid = env
    set_limit(conn, uid, "requests_daily", 2, "count")
    req(conn, uid, NOW - 7200, model=None, path="/v1/models")      # before any model request: opens nothing
    assert limits.states(conn, cfg, uid, now=NOW)[0].current == 0
    req(conn, uid, NOW - 3600)                                     # opens the window
    req(conn, uid, NOW - 60, model=None, path="/v1/models")        # inside the window: still not a metered request
    s = limits.states(conn, cfg, uid, now=NOW)[0]
    assert (s.current, s.reset_in, s.exceeded) == (1, 86400 - 3600, False)
    assert check(conn, cfg, uid) is None


# 4. Request bodies are capped at Anthropic's documented 32 MiB

async def test_oversized_content_length_is_refused_before_reading(setup, anthropic):
    gw, conn, uid, h = setup
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", content=b"{}", headers={**h, "content-length": str(32 * 1024 * 1024 + 1)})
    assert r.status_code == 413 and r.json()["error"]["type"] == "request_too_large"
    assert anthropic.calls == []
    row = last_request(conn)
    assert (row["status"], row["error_type"], row["rejected_by"]) == (413, "request_too_large", "body_size")


async def test_oversized_body_is_refused_while_reading(setup, anthropic, monkeypatch):
    gw, conn, uid, h = setup
    monkeypatch.setattr(app_mod, "MAX_BODY_BYTES", 1024)

    async def chunks():
        for _ in range(4):
            yield b"x" * 512
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", content=chunks(), headers=h)    # chunked: no Content-Length to check
    assert r.status_code == 413 and r.json()["error"]["type"] == "request_too_large"
    assert anthropic.calls == []
    assert last_request(conn)["rejected_by"] == "body_size"


async def test_body_at_the_cap_is_forwarded(setup, anthropic, monkeypatch):
    gw, conn, uid, h = setup
    monkeypatch.setattr(app_mod, "MAX_BODY_BYTES", 4096)
    body = b'{"model":"claude-sonnet-5","max_tokens":1,"messages":[],"pad":"' + b"x" * 3000 + b'"}'
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", content=body, headers={**h, "content-type": "application/json"})
    assert r.status_code == 200 and anthropic.calls[0]["body"] == body
