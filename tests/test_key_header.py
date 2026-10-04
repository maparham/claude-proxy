import time
"""A machine that keeps its own claude.ai login active sends the gateway key in `x-gateway-key`
(ANTHROPIC_CUSTOM_HEADERS); its Authorization header then carries that login's own OAuth token."""
import json

import pytest
from fastapi.responses import JSONResponse

from claude_proxy.app import create_app
from claude_proxy.db import create_user, revoke
from tests.conftest import asgi_client, make_gateway, seed_oauth, sse_response

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
OWN_LOGIN = "sk-ant-oat01-the-machines-own-claude-login"


@pytest.fixture
def setup(cfg, db, anthropic, meta, monkeypatch):
    conn = db[1]
    uid, key = create_user(conn, "owner")
    seed_oauth(conn, access="oauth-secret-token")
    cfg.routes[0].base_url = "http://meta.fake"
    monkeypatch.setenv("META_API_KEY", "meta-key-123")
    gw = make_gateway(cfg, conn, anthropic, meta)
    headers = {"Authorization": f"Bearer {OWN_LOGIN}", "x-gateway-key": key,
               "anthropic-beta": "oauth-2025-04-20,claude-code-20250219"}
    return gw, conn, uid, key, headers


def last_request(conn):
    return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()


async def test_key_header_authenticates_and_neither_client_credential_goes_upstream(setup, anthropic):
    gw, conn, uid, key, h = setup
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 200
    assert last_request(conn)["user_id"] == uid
    call = anthropic.calls[0]
    assert call["headers"]["authorization"] == "Bearer oauth-secret-token"
    assert "x-gateway-key" not in call["headers"]
    everything = json.dumps(call["headers"])
    assert OWN_LOGIN not in everything and key not in everything
    assert call["headers"]["anthropic-beta"] == "oauth-2025-04-20, claude-code-20250219"


async def test_key_header_on_muse_route_sends_only_the_meta_key(setup, anthropic, meta):
    gw, conn, uid, key, h = setup
    meta.default = lambda req: sse_response(model="muse-spark-1.3", input_tokens=5, output_tokens=2)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MSG, "model": "muse-spark", "stream": True}, headers=h)
    assert r.status_code == 200
    call = meta.calls[0]
    assert call["headers"]["authorization"] == "Bearer meta-key-123"
    everything = json.dumps(call["headers"]) + call["body"].decode()
    for secret in (OWN_LOGIN, key, "oauth-secret-token"):
        assert secret not in everything


async def test_claude_login_token_without_key_is_refused_without_echoing_it(setup, anthropic):
    gw, conn, uid, key, h = setup
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers={"Authorization": f"Bearer {OWN_LOGIN}"})
    assert r.status_code == 403
    assert "x-gateway-key" in r.json()["error"]["message"]
    assert OWN_LOGIN not in r.text
    assert anthropic.calls == []
    row = last_request(conn)
    assert (row["user_id"], row["rejected_by"]) == (None, "auth")


async def test_bad_key_in_header_is_403_not_401(setup, anthropic):
    """A client signed in to claude.ai reads 401 as its own login failing and retries ten times."""
    gw, conn, uid, key, h = setup
    revoke(conn, uid)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
        r2 = await c.post("/v1/messages", json=MSG, headers={**h, "x-gateway-key": "sk-proxy-nope"})
    assert (r.status_code, r2.status_code) == (403, 403)
    assert anthropic.calls == []


async def test_upstream_401_after_refresh_reaches_key_header_client_as_403(setup, anthropic):
    gw, conn, uid, key, h = setup
    conn.execute("UPDATE users SET role='admin' WHERE id=?", (uid,))   # a non-admin gets a 503 instead (test_privacy)
    unauthorized = lambda req: JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "x"}},
                                            status_code=401)
    anthropic.default = unauthorized
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 403
    assert last_request(conn)["status"] == 401


async def test_bearer_key_clients_still_get_401(setup, anthropic):
    gw, conn, uid, key, h = setup
    revoke(conn, uid)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401


async def test_health_reports_only_whether_the_login_works_and_usage_is_fresh(setup, db):
    gw, conn, *_ = setup
    qc = gw.cfg.quota
    limit = max(qc.stale_after_s, qc.poll_max_backoff_s) + 600   # an idle gateway backing off after 429s is not stale yet

    def snap(bucket, age):
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct) VALUES(?,?,?,?)",
                     (time.time() - age, "header", bucket, 10))

    async with asgi_client(create_app(gw)) as c:
        assert (await c.get("/health")).json() == {"ok": True, "credential": True, "usage_fresh": False}   # no figures yet
        snap("5h", 60)
        snap("7d_opus", 60)                                                # other buckets don't count
        assert (await c.get("/health")).json()["usage_fresh"] is False     # no weekly figure yet
        snap("7d", limit - 60)
        assert (await c.get("/health")).json() == {"ok": True, "credential": True, "usage_fresh": True}
        conn.execute("DELETE FROM quota_snapshots WHERE bucket='7d'")
        snap("7d", limit + 60)                                             # the older of the two newest decides
        snap("7d_opus", 1)
        assert (await c.get("/health")).json()["usage_fresh"] is False
        conn.execute("DELETE FROM credentials")
        assert (await c.get("/health")).json()["credential"] is False


def test_health_freshness_query_uses_the_bucket_index(db):
    from claude_proxy.app import USAGE_FRESH_SQL
    plan = " ".join(r[3] for r in db[1].execute("EXPLAIN QUERY PLAN " + USAGE_FRESH_SQL))
    assert "idx_quota_bucket_time" in plan and "SCAN quota_snapshots" not in plan
