"""API keys on the proxy (2026-10-21 spec, section 3)."""
import asyncio
import json

import pytest
from fastapi.responses import JSONResponse, StreamingResponse

from claude_proxy import api_credit
from claude_proxy.app import create_app, scope_allows
from claude_proxy.db import create_user, set_enabled
from tests.conftest import asgi_client, make_gateway, message_json, seed_oauth, sse_response

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "stream": True,
       "tools": [{"name": "Read", "input_schema": {"type": "object"}}], "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def env(cfg, db, anthropic):
    conn = db[1]
    cfg.api.enabled = True
    cfg.api.max_inflight, cfg.api.rpm = 2, 100
    uid, full = create_user(conn, "alice")
    seed_oauth(conn, access="oauth-secret-token")
    gw = make_gateway(cfg, conn, anthropic)   # seeds the rates
    api_credit.set_rate(conn, "claude-sonnet-5*", 3, 15)
    kid, raw = api_credit.create_key(conn, uid, "laptop")
    return gw, conn, uid, kid, {"x-api-key": raw}


def last(conn):
    return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()


def ledger(conn):
    return [dict(r) for r in conn.execute("SELECT kind, usd, api_key_id, request_id, model FROM api_ledger WHERE kind='charge'")]


@pytest.mark.parametrize("method,path,routed,allowed", [
    ("POST", "/v1/messages", False, True),
    ("POST", "/v1/messages/count_tokens", False, True),
    ("GET", "/v1/models", False, True),
    ("POST", "/v1/messages", True, False),
    ("GET", "/v1/messages", False, False),
    ("POST", "/v1/files", False, False),
    ("GET", "/v1/organizations/x", False, False),
])
def test_api_scope(method, path, routed, allowed):
    assert scope_allows("api", method, path, object() if routed else None) is allowed


async def test_streamed_request_passes_through_with_tools_and_is_charged(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    anthropic.queue.append(lambda req: sse_response(model="claude-sonnet-5", input_tokens=1000, output_tokens=200))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
        assert r.status_code == 200 and b"message_stop" in r.content
    sent = anthropic.calls[-1]
    assert sent["json"]["tools"] == MSG["tools"]                       # unchanged
    assert sent["headers"]["authorization"] == "Bearer oauth-secret-token"
    assert "x-api-key" not in sent["headers"]                          # the client's key never goes upstream
    row = last(conn)
    assert (row["api_key_id"], row["input_tokens"], row["output_tokens"]) == (kid, 1000, 200)
    [ch] = ledger(conn)
    assert (ch["api_key_id"], ch["request_id"], ch["model"]) == (kid, row["id"], "claude-sonnet-5")
    assert ch["usd"] == pytest.approx(-(1000 * 3 + 200 * 15) / 1e6)


async def test_low_credit_is_refused_before_forwarding(env, anthropic):
    gw, conn, uid, kid, h = env
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 400 and r.json()["error"]["message"] == api_credit.LOW_CREDIT
    assert anthropic.calls == [] and last(conn)["rejected_by"] == "api_credit" and ledger(conn) == []


async def test_unpriced_model_is_404_and_routed_model_is_403(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    api_credit.delete_rate(conn, "claude-opus-5*", None)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "model": "claude-opus-5"}, headers=h)
        assert r.status_code == 404 and r.json()["error"]["type"] == "not_found_error"
        r = await c.post("/v1/messages", json={**MSG, "model": "muse-spark"}, headers=h)
        assert r.status_code == 403 and last(conn)["rejected_by"] == "key_scope"
    assert anthropic.calls == []


async def test_upstream_error_without_usage_is_not_charged(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    anthropic.queue.append(lambda req: JSONResponse({"type": "error", "error": {"type": "overloaded_error", "message": "o"}}, status_code=529))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 529 and ledger(conn) == [] and api_credit.balance(conn, uid) == 10
    assert last(conn)["api_key_id"] == kid and last(conn)["rejected_by"] is None   # counts toward rpm


async def test_a_stream_cut_short_is_charged_for_what_was_metered(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    # No message_stop: the upstream ended the stream early (as tests/test_proxy.py simulates it).
    anthropic.queue.append(lambda req: sse_response(model="claude-sonnet-5", input_tokens=1000, output_tokens=200, stop=False))
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json=MSG, headers=h)
    [ch] = ledger(conn)
    assert ch["usd"] < 0 and last(conn)["complete"] == 0


async def test_count_tokens_is_free_and_not_counted_for_rpm(env, anthropic):
    gw, conn, uid, kid, h = env   # no credit at all
    anthropic.queue.append(lambda req: JSONResponse({"input_tokens": 12}))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages/count_tokens", json={"model": "claude-sonnet-5", "messages": []}, headers=h)
    assert r.status_code == 200 and ledger(conn) == []


async def test_an_admins_api_key_is_treated_as_a_user(cfg, db, anthropic):
    conn = db[1]
    cfg.api.enabled = True
    uid, _ = create_user(conn, "boss", role="admin")
    seed_oauth(conn)
    gw = make_gateway(cfg, conn, anthropic)
    api_credit.grant(conn, uid, 10, "x", None)
    _, raw = api_credit.create_key(conn, uid, "k")
    anthropic.queue.append(lambda req: JSONResponse(message_json("claude-sonnet-5"), headers={"anthropic-ratelimit-unified-5h-utilization": "0.5"}))
    anthropic.queue.append(lambda req: JSONResponse({"type": "error", "error": {"type": "rate_limit_error", "message": "account x"}}, status_code=429))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "stream": False}, headers={"x-api-key": raw})
        assert not any(k.startswith("anthropic-ratelimit-") for k in r.headers)
        r = await c.post("/v1/messages", json={**MSG, "stream": False}, headers={"x-api-key": raw})
        assert r.status_code == 429 and "account" not in r.json()["error"]["message"]


async def test_concurrent_requests_are_capped_so_overdraft_is_bounded(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 0.000001, "x", None)
    gate = asyncio.Event()

    async def held_body():   # the upstream answers only once the test opens the gate
        await gate.wait()
        yield json.dumps(message_json("claude-sonnet-5", input_tokens=1000, output_tokens=100)).encode()

    anthropic.default = lambda req: StreamingResponse(held_body(), media_type="application/json")
    async with asgi_client(create_app(gw)) as c:
        first = [asyncio.create_task(c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)) for _ in range(2)]
        await asyncio.sleep(0.05)
        third = await c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)
        assert third.status_code == 429 and last(conn)["rejected_by"] == "api_inflight"
        gate.set()
        assert [t.status_code for t in await asyncio.gather(*first)] == [200, 200]
        r = await c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)
    assert api_credit.balance(conn, uid) < 0                       # overdraft by at most the two in flight
    assert r.status_code == 400 and r.json()["error"]["message"] == api_credit.LOW_CREDIT


async def test_model_list_shows_only_priced_models(env, anthropic):
    gw, conn, uid, kid, h = env
    anthropic.queue.append(lambda req: JSONResponse({"data": [{"id": "claude-sonnet-5"}, {"id": "claude-x-1"}], "has_more": False}))
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models", headers=h)
    assert [m["id"] for m in r.json()["data"]] == ["claude-sonnet-5"]


async def test_api_keys_are_refused_while_the_api_is_off(env, anthropic):
    gw, conn, uid, kid, h = env
    gw.cfg.api.enabled = False
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 403 and r.json()["error"]["message"] == "The API is not enabled on this gateway."


async def test_disabled_user_and_revoked_key_are_refused_and_other_keys_still_work(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    _, other = api_credit.create_key(conn, uid, "ci")
    api_credit.revoke_key(conn, uid, kid, uid)
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)).status_code == 401
        assert (await c.post("/v1/messages", json={**MSG, "stream": False}, headers={"x-api-key": other})).status_code == 200
        set_enabled(conn, uid, False)
        assert (await c.post("/v1/messages", json={**MSG, "stream": False}, headers={"x-api-key": other})).status_code == 403


async def test_api_traffic_does_not_count_against_limit_rows(env, anthropic):
    gw, conn, uid, kid, h = env
    from claude_proxy import limits
    api_credit.grant(conn, uid, 10, "x", None)
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,?)",
                 (uid, "requests_daily", "*", "1", "count", 0))
    async with asgi_client(create_app(gw)) as c:
        for _ in range(2):
            assert (await c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)).status_code == 200
    assert limits.evaluate(conn, gw.cfg, uid, "claude-sonnet-5", "/v1/messages") is None


async def test_no_credit_cannot_flood_the_subscription_with_token_counts_or_model_lists(env, anthropic):
    gw, conn, uid, kid, h = env   # no credit; rpm 3
    gw.cfg.api.rpm = 3
    gate = asyncio.Event()

    async def held_body():
        await gate.wait()
        yield b'{"input_tokens": 12}'

    anthropic.default = lambda req: StreamingResponse(held_body(), media_type="application/json")
    async with asgi_client(create_app(gw)) as c:
        body = {"model": "claude-sonnet-5", "messages": []}
        tasks = [asyncio.create_task(c.post("/v1/messages/count_tokens", json=body, headers=h)) for _ in range(6)]
        await asyncio.sleep(0.05)
        gate.set()
        codes = sorted(r.status_code for r in await asyncio.gather(*tasks))
        assert codes == [200, 200, 200, 429, 429, 429] and len(anthropic.calls) == 3   # in-flight counts toward rpm
        anthropic.default = lambda req: JSONResponse({"data": [{"id": "claude-sonnet-5"}], "has_more": False})
        r = await c.get("/v1/models", headers=h)
        assert r.status_code == 429 and len(anthropic.calls) == 3   # the model list is rate-limited too


async def test_a_non_streamed_reply_over_the_buffer_cap_is_still_charged(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    big = message_json("claude-sonnet-5", input_tokens=1000, output_tokens=200, cc5=0, cc1=0, text="x" * (9 * 1024 * 1024))
    raw = json.dumps(big).encode()

    async def chunks():   # as a real upstream sends it: many reads, not one
        for i in range(0, len(raw), 1024 * 1024):
            yield raw[i:i + 1024 * 1024]

    anthropic.queue.append(lambda req: StreamingResponse(chunks(), media_type="application/json"))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "stream": False}, headers=h)
        assert r.status_code == 200 and len(r.content) > 9 * 1024 * 1024    # forwarded whole
    row = last(conn)
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 200)       # usage read from the tail
    [ch] = ledger(conn)
    assert ch["usd"] == pytest.approx(-(1000 * 3 + 200 * 15) / 1e6)


@pytest.mark.parametrize("extra,headers,needle", [
    ({"tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]}, {}, "Web search"),
    ({"speed": "fast"}, {}, "Fast mode"),
    ({}, {"anthropic-beta": "fast-mode-2026-02-01"}, "Fast mode"),
    ({}, {"anthropic-beta": "oauth-2025-04-20, context-1m-2025-08-07"}, "1M-token context"),
])
async def test_extras_priced_above_tokens_are_refused_on_api_keys(env, anthropic, extra, headers, needle):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, **extra}, headers={**h, **headers})
    assert r.status_code == 400 and needle in r.json()["error"]["message"]
    assert anthropic.calls == [] and last(conn)["rejected_by"] == api_credit.REJECT_EXTRA


async def test_ordinary_tools_and_betas_still_pass(env, anthropic):
    gw, conn, uid, kid, h = env
    api_credit.grant(conn, uid, 10, "x", None)
    anthropic.queue.append(lambda req: sse_response(model="claude-sonnet-5"))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "speed": "standard"},
                         headers={**h, "anthropic-beta": "interleaved-thinking-2025-05-14"})
    assert r.status_code == 200
