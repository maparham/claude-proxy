"""Pass app keys on the proxy (2026-11-18 spec, section 3): served and counted by the holder's running pass."""
import asyncio
import json
import pathlib
import time

import httpx
import pytest
from fastapi.responses import JSONResponse, StreamingResponse

from claude_proxy import api_credit, contents, passkeys, tickets
from claude_proxy import db as dbm
from claude_proxy.app import create_app, inflight_of, scope_allows
from claude_proxy.config import Route
from claude_proxy.forwarder import CLAUDE_CODE_IDENTITY, with_claude_code_identity
from tests.conftest import asgi_client, make_gateway, message_json, seed_oauth, sse_response
from tests.test_tickets_limits import snap
from tests.test_tickets_web import env, grant  # noqa: F401  (the fixture)
from tests.test_web import admin_client, bearer
from tests.tickets_helpers import report

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "claude_proxy"


@pytest.fixture
async def penv(env, anthropic):
    """alice with a running Lite week pass, fresh 5h/7d figures and a pass app key sent the Bearer way (Desktop)."""
    gw, conn, cfg, ids, keys = env
    seed_oauth(conn)
    gw = make_gateway(cfg, conn, anthropic)
    now = time.time()
    snap(conn, now - 120, 0, "5h", now + 3600)
    report(conn, ids, 0.0, t=now - 90)
    async with admin_client(gw) as c:
        assert (await grant(c, {"user": ids["alice"], "tier": "lite", "length": "week", "currency": "EUR"})).status_code == 200
    [t] = tickets.running(conn, ids["alice"], time.time())
    kid, raw = passkeys.create_key(conn, ids["alice"], "Desktop")
    return gw, conn, cfg, ids, keys, t, kid, {"authorization": f"Bearer {raw}"}


def last(conn):
    return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()


def slots(gw, uid):
    return inflight_of(gw).get(uid), inflight_of(gw, "pass_inflight").get(uid)


@pytest.mark.parametrize("method,path,routed,allowed", [
    ("POST", "/v1/messages", False, True),
    ("POST", "/v1/messages/count_tokens", False, True),
    ("GET", "/v1/models", False, True),
    ("POST", "/v1/messages", True, False),
    ("GET", "/v1/messages", False, False),
    ("POST", "/v1/files", False, False),
    ("GET", "/v1/organizations/x", False, False),
])
def test_pass_scope(method, path, routed, allowed):
    assert scope_allows("pass", method, path, object() if routed else None) is allowed


async def test_message_forwarded_and_counted(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 200, r.text
    row = last(conn)
    assert (row["pass_key_id"], row["api_key_id"], row["ticket_id"], row["rejected_by"]) == (kid, None, t["id"], None)
    system = anthropic.calls[-1]["json"]["system"]
    assert system[0]["text"] == CLAUDE_CODE_IDENTITY
    assert conn.execute("SELECT COUNT(*) FROM api_ledger").fetchone()[0] == 0
    assert slots(gw, ids["alice"]) == (0, 0)


async def test_no_pass_403(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    tickets.cancel(conn, cfg, ids["admin"], t["id"])
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 403 and r.json()["error"]["message"] == "This key works while you have a running pass."
    assert last(conn)["rejected_by"] == "pass_key_no_pass" and anthropic.calls == []
    assert slots(gw, ids["alice"]) == (0, 0)


async def test_web_search_refused(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    body = {**MSG, "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]}
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=body, headers=h)
    assert r.status_code == 400 and "pass app keys" in r.json()["error"]["message"]
    assert last(conn)["rejected_by"] == "api_extra" and anthropic.calls == []


async def test_routed_model_refused(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    cfg.routes.append(Route(name="meta", base_url="http://m", api_key_env="M", models=["muse-*"]))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "model": "muse-spark"}, headers=h)
    assert r.status_code == 403 and r.json()["error"]["message"] == "Pass app keys reach Claude models only, through /v1/messages."
    assert last(conn)["rejected_by"] == "key_scope" and anthropic.calls == []


async def test_works_with_api_disabled(penv):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    cfg.api.enabled = False
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json=MSG, headers=h)).status_code == 200


async def test_models_list_has_no_routes(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    cfg.routes.append(Route(name="meta", base_url="http://m", api_key_env="M", models=["muse-spark"]))
    anthropic.queue.append(lambda req: JSONResponse({"data": [{"id": "claude-sonnet-5"}, {"id": "claude-x-1"}], "has_more": False}))
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models", headers=h)
    assert r.status_code == 200 and [m["id"] for m in r.json()["data"]] == ["claude-sonnet-5"]
    assert last(conn)["pass_key_id"] == kid and slots(gw, ids["alice"]) == (0, 0)


async def test_models_list_priced_only_for_pass_key(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    assert cfg.pricing.price_for("claude-unpriced-9") is None
    listing = {"data": [{"id": "claude-sonnet-5"}, {"id": "claude-unpriced-9"}], "has_more": False}
    anthropic.queue.append(lambda req: JSONResponse(listing))
    anthropic.queue.append(lambda req: JSONResponse(listing))
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models", headers=h)
        cc = await c.get("/v1/models", headers=bearer(keys["alice"]))
    assert [m["id"] for m in r.json()["data"]] == ["claude-sonnet-5"] and r.json()["last_id"] == "claude-sonnet-5"
    assert [m["id"] for m in cc.json()["data"]][:2] == ["claude-sonnet-5", "claude-unpriced-9"]


async def test_contents_kept_without_session(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    anthropic.queue.append(lambda req: JSONResponse(message_json(model="claude-sonnet-5", text="ok")))
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json=MSG, headers=h)).status_code == 200
    d = contents.load(conn, last(conn)["id"])
    assert d["request"] == MSG and d["reply"]["content"] == [{"type": "text", "text": "ok"}]


async def test_inflight_shared(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    uid = ids["alice"]
    cc = bearer(keys["alice"])
    cfg.limits.max_inflight = 6
    gate = asyncio.Event()

    async def held_body():   # the upstream answers only once the test opens the gate
        await gate.wait()
        yield json.dumps(message_json("claude-sonnet-5")).encode()

    anthropic.default = lambda req: StreamingResponse(held_body(), media_type="application/json")
    async with asgi_client(create_app(gw)) as c:
        # Claude Code at the shared cap: an app's request is refused by it.
        busy = [asyncio.create_task(c.post("/v1/messages", json=MSG, headers=cc)) for _ in range(cfg.limits.max_inflight)]
        await asyncio.sleep(0.1)
        assert slots(gw, uid) == (cfg.limits.max_inflight, 0)
        r = await c.post("/v1/messages", json=MSG, headers=h)
        assert r.status_code == 429 and last(conn)["rejected_by"] == "max_inflight"
        gate.set()
        assert [x.status_code for x in await asyncio.gather(*busy)] == [200] * cfg.limits.max_inflight
        assert slots(gw, uid) == (0, 0)

        # Apps at their own cap: a fifth app request is refused, Claude Code still goes through.
        gate.clear()
        apps = [asyncio.create_task(c.post("/v1/messages", json=MSG, headers=h)) for _ in range(passkeys.PASS_KEY_MAX_INFLIGHT)]
        await asyncio.sleep(0.1)
        assert slots(gw, uid) == (passkeys.PASS_KEY_MAX_INFLIGHT, passkeys.PASS_KEY_MAX_INFLIGHT)
        r = await c.post("/v1/messages", json=MSG, headers=h)
        assert r.status_code == 429 and last(conn)["rejected_by"] == "pass_key_inflight"
        cc_task = asyncio.create_task(c.post("/v1/messages", json=MSG, headers=cc))
        await asyncio.sleep(0.1)
        assert slots(gw, uid) == (passkeys.PASS_KEY_MAX_INFLIGHT + 1, passkeys.PASS_KEY_MAX_INFLIGHT)
        gate.set()
        assert [x.status_code for x in await asyncio.gather(*apps, cc_task)] == [200] * (passkeys.PASS_KEY_MAX_INFLIGHT + 1)
    assert slots(gw, uid) == (0, 0) and inflight_of(gw, "pass_metered_inflight").get(uid) == 0
    rows = conn.execute("SELECT pass_key_id, ticket_id FROM requests WHERE rejected_by IS NULL AND pass_key_id IS NOT NULL").fetchall()
    assert len(rows) == passkeys.PASS_KEY_MAX_INFLIGHT and all(r["ticket_id"] == t["id"] for r in rows)


async def test_count_tokens_and_models_hold_only_an_app_slot(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    seen = []

    def answer(body):
        def fn(req):
            seen.append(slots(gw, ids["alice"]))
            return JSONResponse(body)
        return fn

    anthropic.queue.append(answer({"input_tokens": 12}))
    anthropic.queue.append(answer({"data": [], "has_more": False}))
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages/count_tokens", json={"model": "claude-sonnet-5", "messages": []}, headers=h)).status_code == 200
        assert (await c.get("/v1/models", headers=h)).status_code == 200
    assert seen == [(0, 1), (0, 1)] and slots(gw, ids["alice"]) == (0, 0)


async def test_count_tokens_do_not_use_the_apps_cap(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    uid = ids["alice"]
    gate = asyncio.Event()

    async def held_body(body):
        await gate.wait()
        yield json.dumps(body).encode()

    count = {"model": "claude-sonnet-5", "messages": []}
    anthropic.default = lambda req: StreamingResponse(
        held_body({"input_tokens": 3} if req["path"].endswith("count_tokens") else message_json("claude-sonnet-5")),
        media_type="application/json")
    n = passkeys.PASS_KEY_MAX_INFLIGHT
    async with asgi_client(create_app(gw)) as c:
        counts = [asyncio.create_task(c.post("/v1/messages/count_tokens", json=count, headers=h)) for _ in range(n)]
        await asyncio.sleep(0.1)
        assert slots(gw, uid) == (0, n) and inflight_of(gw, "pass_metered_inflight").get(uid) == 0
        msg = asyncio.create_task(c.post("/v1/messages", json=MSG, headers=h))   # not refused by the four counts
        await asyncio.sleep(0.1)
        assert inflight_of(gw, "pass_metered_inflight").get(uid) == 1
        gate.set()
        assert [x.status_code for x in await asyncio.gather(*counts, msg)] == [200] * (n + 1)
    assert slots(gw, uid) == (0, 0) and inflight_of(gw, "pass_metered_inflight").get(uid) == 0


def test_usage_queries_never_filter_pass_key():
    for name in ("limits.py", "capacity.py", "tickets.py", "quota.py", "trials.py", "usage.py", "estimates.py"):
        assert "pass_key_id" not in (SRC / name).read_text(), name
    allowed = {"passkeys.py", "db.py", "app.py", "web.py"}
    assert {p.name for p in SRC.glob("*.py") if "pass_key_id" in p.read_text()} <= allowed


async def test_slots_released_on_failure(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    anthropic.queue.append(lambda req: sse_response(model="claude-sonnet-5", input_tokens=1000, output_tokens=200, stop=False))
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json={**MSG, "stream": True}, headers=h)
    row = last(conn)
    assert row["complete"] == 0 and row["pass_key_id"] == kid and row["ticket_id"] == t["id"]
    assert slots(gw, ids["alice"]) == (0, 0)

    def unreachable(request):
        raise httpx.ConnectError("down")

    gw.http = httpx.AsyncClient(transport=httpx.MockTransport(unreachable))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 502 and last(conn)["error_type"] == "gateway_upstream_unreachable"
    assert slots(gw, ids["alice"]) == (0, 0)
    assert inflight_of(gw, "pass_metered_inflight").get(ids["alice"]) == 0


async def test_dashboard_says_whether_a_pass_runs(penv):
    """has_pass drives the API tab: a running pass opens it on the app keys, none shows the "buy a pass" line."""
    from claude_proxy.web import create_dashboard_app
    gw, conn, cfg, ids, keys, t, kid, h = penv
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/me/passkeys", headers=bearer(keys["alice"]))).json()["has_pass"] is True
        tickets.cancel(conn, cfg, None, t["id"])
        assert (await c.get("/api/me/passkeys", headers=bearer(keys["alice"]))).json()["has_pass"] is False


# ---- moved from the credit API's tests (2026-11-19: API keys are pass app keys) ----

CC = {"type": "text", "text": CLAUDE_CODE_IDENTITY}


@pytest.mark.parametrize("extra,headers,needle", [
    ({"tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]}, {}, "Web search"),
    ({"speed": "fast"}, {}, "Fast mode"),
    ({}, {"anthropic-beta": "fast-mode-2026-02-01"}, "Fast mode"),
    ({}, {"anthropic-beta": "oauth-2025-04-20, context-1m-2025-08-07"}, "1M-token context"),
])
async def test_extras_priced_above_tokens_are_refused(penv, anthropic, extra, headers, needle):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, **extra}, headers={**h, **headers})
    assert r.status_code == 400 and needle in r.json()["error"]["message"]
    assert anthropic.calls == [] and last(conn)["rejected_by"] == api_credit.REJECT_EXTRA


async def test_ordinary_tools_and_betas_still_pass(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    anthropic.queue.append(lambda req: sse_response(model="claude-sonnet-5"))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "speed": "standard", "tools": [{"name": "Read", "input_schema": {"type": "object"}}]},
                         headers={**h, "anthropic-beta": "interleaved-thinking-2025-05-14"})
    assert r.status_code == 200


@pytest.mark.parametrize("system,expected", [
    (None, [CC]),
    ("Be terse.", [CC, {"type": "text", "text": "Be terse."}]),
    ([{"type": "text", "text": "Be terse.", "cache_control": {"type": "ephemeral"}}],
     [CC, {"type": "text", "text": "Be terse.", "cache_control": {"type": "ephemeral"}}]),
])
async def test_identity_line_prepended_for_non_claude_code_clients(penv, anthropic, system, expected):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    anthropic.queue.append(lambda req: sse_response(model="claude-sonnet-5", input_tokens=10, output_tokens=2))
    body = {**MSG, **({"system": system} if system is not None else {})}
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json=body, headers=h)).status_code == 200
    assert anthropic.calls[-1]["json"]["system"] == expected


def test_claude_code_system_prompt_left_alone():
    for system in (CC["text"], [CC, {"type": "text", "text": "x"}]):
        data = {"system": system}
        assert not with_claude_code_identity(data) and data["system"] == system


def test_identity_line_sharing_a_block_still_gets_its_own():
    # Anthropic refuses the line followed by more text in the same block, so it is added as a block of its own.
    data = {"system": CC["text"] + " Be terse."}
    assert with_claude_code_identity(data) and data["system"] == [CC, {"type": "text", "text": CC["text"] + " Be terse."}]


async def test_request_scoped_429_is_not_called_a_usage_limit(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    anthropic.queue.append(lambda req: JSONResponse(status_code=429, content={"type": "error", "error": {"type": "rate_limit_error", "message": "Error"}}))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 429 and "not a usage limit" in r.json()["error"]["message"]
    assert last(conn)["error_type"] == "upstream_request_scoped"


# ---- old credit API keys (sk-proxy-a-) became pass app keys (2026-11-19 spec) ----

def add_old_api_key(conn, uid, label="ci-bot", revoked=False):
    raw = f"sk-proxy-a-{label}-{uid}-" + "x" * 20
    conn.execute("INSERT INTO api_keys(user_id, key_hash, key_prefix, label, created_at, last_used_at, revoked_at) VALUES(?,?,?,?,?,?,?)",
                 (uid, dbm.hash_key(raw), raw[:14], label, 1000, 2000, 3000 if revoked else None))
    return raw


def test_migration_turns_live_api_keys_into_pass_keys(db):
    conn = db[1]
    uid, _ = dbm.create_user(conn, "alice")
    live, gone = add_old_api_key(conn, uid), add_old_api_key(conn, uid, "old", revoked=True)
    dbm._migrate(conn)
    [p] = conn.execute("SELECT * FROM pass_keys").fetchall()
    assert (p["user_id"], p["key_hash"], p["key_prefix"], p["label"], p["created_at"], p["last_used_at"], p["revoked_at"]) == \
        (uid, dbm.hash_key(live), live[:14], "ci-bot", 1000, 2000, None)
    assert conn.execute("SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL").fetchone()[0] == 0
    u = dbm.find_user_by_key(conn, live)
    assert (u["id"], u["key_scope"], u["pass_key_id"], u["role"]) == (uid, "pass", p["id"], "user") and "api_key_id" not in u
    assert dbm.find_user_by_key(conn, gone) is None
    dbm._migrate(conn)   # a second run (every start) changes nothing
    assert conn.execute("SELECT COUNT(*) FROM pass_keys").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='api_keys_to_pass_keys'").fetchone()[0] == 1


async def test_an_old_api_key_needs_a_running_pass(penv, anthropic):
    gw, conn, cfg, ids, keys, t, kid, h = penv
    raw = add_old_api_key(conn, ids["alice"])
    dbm._migrate(conn)
    old = {"x-api-key": raw}
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json=MSG, headers=old)).status_code == 200
        row = last(conn)
        assert (row["pass_key_id"] not in (None, kid), row["api_key_id"], row["ticket_id"]) == (True, None, t["id"])
        tickets.cancel(conn, cfg, ids["admin"], t["id"])
        r = await c.post("/v1/messages", json=MSG, headers=old)
    assert r.status_code == 403 and last(conn)["rejected_by"] == "pass_key_no_pass"
    assert conn.execute("SELECT COUNT(*) FROM api_ledger").fetchone()[0] == 0
