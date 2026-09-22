import json
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from claude_proxy.config import Config
from claude_proxy.credentials import encrypt_blob
from claude_proxy.db import init_db, create_user
from claude_proxy.app import create_app


def make_fake_upstream_oauth(expected_access="test-access-token"):
    app = FastAPI()

    @app.api_route("/v1/messages", methods=["POST"])
    async def messages(request: Request):
        auth = request.headers.get("authorization", "")
        assert auth == f"Bearer {expected_access}", f"OAuth bearer missing/wrong: {auth}"
        assert "x-api-key" not in {k.lower() for k in request.headers.keys()}, "x-api-key leaked"
        beta = request.headers.get("anthropic-beta", "")
        assert "oauth-2025-04-20" in beta, f"beta flag missing: {beta}"
        body = await request.body()
        data = json.loads(body) if body else {}
        return JSONResponse(
            content={"id": "msg_123", "type": "message", "model": data.get("model", "claude-sonnet-4-6"), "usage": {"input_tokens": 10, "output_tokens": 5}, "content": [{"type": "text", "text": "hi"}]},
            headers={"anthropic-ratelimit-unified-5h-utilization": "0.12"},
        )

    @app.get("/v1/models")
    async def list_models(request: Request):
        auth = request.headers.get("authorization", "")
        assert auth == f"Bearer {expected_access}"
        return JSONResponse(content={"data": []})

    return app


def _seed_oauth(db_path, access="test-access-token", refresh="test-refresh", expires_in=3600):
    # Seed encrypted credential row as login would
    data = {"access_token": access, "refresh_token": refresh, "expires_at": int(time.time()) + expires_in}
    blob = encrypt_blob(data)
    import sqlite3
    conn = init_db(db_path) if isinstance(db_path, str) else None
    # Use direct sqlite if db_path str
    import sqlite3 as s3
    c = s3.connect(db_path)
    c.execute("INSERT OR REPLACE INTO credentials(backend, encrypted_blob, expires_at, updated_at, state) VALUES('oauth',?,?,?,?)", (blob, data["expires_at"], int(time.time()), "active"))
    c.commit()
    c.close()


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    yield str(db_path), conn
    try:
        conn.close()
    except:
        pass


@pytest.mark.asyncio
async def test_auth_missing_returns_401(tmp_db):
    db_path, conn = tmp_db
    cfg = Config()
    cfg.db.path = db_path
    cfg.upstream.base_url = "http://fake"
    app = create_app(cfg, db_conn=conn)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/v1/models")
        assert r.status_code == 401
        assert r.json()["error"]["type"] == "authentication_error"
    row = conn.execute("SELECT * FROM requests WHERE rejected_by='auth'").fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_unknown_path_404_not_forwarded(tmp_db):
    db_path, conn = tmp_db
    uid, raw = create_user(conn, "alice")
    conn.commit()
    cfg = Config()
    cfg.db.path = db_path
    cfg.upstream.base_url = "http://fake-will-not-be-called"
    app = create_app(cfg, db_conn=conn)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/teamclaude/dashboard", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_forward_oauth_bearer_and_beta(tmp_db):
    db_path, conn = tmp_db
    uid, raw = create_user(conn, "bob")
    conn.commit()
    _seed_oauth(db_path, access="test-access-token")
    fake_app = make_fake_upstream_oauth("test-access-token")
    cfg = Config()
    cfg.db.path = db_path
    cfg.upstream.base_url = "http://fake"
    from claude_proxy import app as app_module
    app = create_app(cfg, db_conn=conn)
    fake_transport = httpx.ASGITransport(app=fake_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake", http1=True, http2=False)
    old = app_module._upstream_client
    app_module._upstream_client = fake_client
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hello"}]}
            r = await client.post("/v1/messages", json=payload, headers={"Authorization": f"Bearer {raw}", "anthropic-beta": "my-beta"})
            assert r.status_code == 200, r.text
            # beta merged
            assert r.json()["model"] == "claude-sonnet-4-6"
            assert "transfer-encoding" not in {k.lower() for k in r.headers.keys()}
        row = conn.execute("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        assert row["path"] == "/v1/messages" and row["status"] == 200
    finally:
        app_module._upstream_client = old
        await fake_client.aclose()


@pytest.mark.asyncio
async def test_no_credential_returns_503(tmp_db):
    db_path, conn = tmp_db
    uid, raw = create_user(conn, "carol")
    conn.commit()
    cfg = Config()
    cfg.db.path = db_path
    cfg.upstream.base_url = "http://fake"
    app = create_app(cfg, db_conn=conn)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 503
        assert "login" in r.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_hop_by_hop_stripped():
    from claude_proxy.forwarder import filter_request_headers, filter_response_headers
    req = {"Connection": "keep-alive, X-Foo", "X-Foo": "bar", "X-Keep": "yes", "Authorization": "Bearer xyz", "x-api-key": "abc", "Content-Type": "application/json"}
    out = filter_request_headers(req)
    assert "Authorization" not in out
    assert "X-Foo" not in out
    assert "X-Keep" in out
    resp = {"Transfer-Encoding": "chunked", "Content-Type": "application/json", "X-Custom": "hi"}
    out2 = filter_response_headers(resp)
    assert "Transfer-Encoding" not in out2
    assert "X-Custom" in out2


def test_pkce_and_encrypt():
    from claude_proxy.credentials import generate_pkce, encrypt_blob, decrypt_blob
    v, c, s = generate_pkce()
    assert len(v) > 20 and len(c) > 20
    blob = encrypt_blob({"access_token": "a", "refresh_token": "r"})
    assert decrypt_blob(blob)["access_token"] == "a"
