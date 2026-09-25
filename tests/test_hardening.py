"""Regressions for the 2026-09-25 review: path traversal, fail-open model checks, metering gaps."""
import gzip

import httpx
import pytest

from claude_proxy import limits
from claude_proxy.app import create_app
from claude_proxy.credentials import OAuthBackend
from claude_proxy.db import create_session, create_user, find_session, rotate_key
from claude_proxy.forwarder import should_forward
from claude_proxy.gateway import Gateway
from claude_proxy.meter import SSEMeter
from tests.conftest import asgi_client, make_gateway, seed_oauth, sse_events

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def setup(cfg, db, anthropic):
    conn = db[1]
    uid, key = create_user(conn, "alice")
    seed_oauth(conn)
    return make_gateway(cfg, conn, anthropic), conn, uid, {"Authorization": f"Bearer {key}"}


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages/count_tokens", "/v1/models", "/v1/models/claude-sonnet-5"])
def test_plain_v1_paths_are_forwarded(path):
    assert should_forward(path)


@pytest.mark.parametrize("path", [
    "/v1/../api/oauth/usage",                        # leaves /v1/ once the HTTP client collapses `..`
    "/v1/messages/count_tokens/../../messages",      # a message dressed as count_tokens, to dodge limits
    "/v1/./messages", "/v1//messages", "/v1/", "/v1",
    "/v1/%2e%2e/api",                                # still encoded after one decode: an upstream proxy might decode it
    "/v1/messages\\..\\..\\api",
])
def test_traversal_and_odd_paths_are_not_forwarded(path):
    assert not should_forward(path)


async def test_encoded_traversal_never_reaches_upstream(setup, anthropic):
    gw, conn, uid, h = setup
    async with asgi_client(create_app(gw)) as c:
        for path in ("/v1/%2e%2e/api/oauth/usage", "/v1/messages/count_tokens/%2E%2E/%2e%2e/messages"):
            r = await c.post(path, json=MSG, headers=h)
            assert r.status_code == 404, path
    assert anthropic.calls == []


def test_only_the_exact_count_tokens_path_skips_limits(db):
    conn = db[1]
    uid, _ = create_user(conn, "bob")
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,'requests_daily','*','0','count',0)", (uid,))
    assert limits.evaluate(conn, gw_cfg(), uid, "claude-sonnet-5", "/v1/messages/count_tokens") is None
    assert limits.evaluate(conn, gw_cfg(), uid, "claude-sonnet-5", "/v1/messages/count_tokens/../../messages") is not None


def gw_cfg():
    from claude_proxy.config import Config
    return Config()


@pytest.mark.parametrize("body", [b"not json", b'{"model": ["claude-opus-5-5"], "messages": []}',
                                  gzip.compress(b'{"model": "claude-opus-5-5", "messages": []}')])
async def test_unreadable_model_is_refused_not_forwarded(setup, anthropic, body):
    gw, conn, uid, h = setup
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,'allowed_models','*','claude-haiku-*','list',0)", (uid,))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", content=body, headers={**h, "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
    assert anthropic.calls == []
    row = conn.execute("SELECT rejected_by, error_type FROM requests ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("request", "gateway_bad_request")


def test_stream_cut_short_estimates_output_from_streamed_text():
    text = "x" * 3500
    events = sse_events(output_tokens=1000, text=text, stop=False)
    cut = events[:events.index(b"event: message_delta")]   # the client left before the final counts
    m = SSEMeter()
    m.feed(cut)
    r = m.finalize()
    assert not r.complete and r.output_tokens == 1000   # 3500 chars / 3.5


def test_complete_stream_keeps_the_reported_output():
    m = SSEMeter()
    m.feed(sse_events(output_tokens=7, text="x" * 3500))
    assert m.finalize().output_tokens == 7


def test_multibyte_character_split_across_chunks_is_kept():
    events = sse_events(text="Grüße, 日本語")
    m = SSEMeter(collect_text=True)
    for i in range(len(events)):   # one byte at a time splits every multi-byte character
        m.feed(events[i:i + 1])
    assert m.finalize().text == "Grüße, 日本語"


def test_rotating_a_key_ends_its_dashboard_sessions(db):
    conn = db[1]
    uid, _ = create_user(conn, "carol")
    token, _ = create_session(conn, uid)
    assert find_session(conn, token) is not None
    rotate_key(conn, uid)
    assert find_session(conn, token) is None


def test_backend_sees_a_login_stored_in_the_same_second(cfg, db):
    conn = db[1]
    seed_oauth(conn, access="first")
    running = OAuthBackend(cfg, conn, httpx.AsyncClient())
    assert running.current_token() == "first"
    # `claude-proxy login` in another process, within the same second as the running server's last read.
    OAuthBackend(cfg, conn, httpx.AsyncClient()).store({"access_token": "second", "refresh_token": "r", "expires_at": 2**31})
    assert running.current_token() == "second"


async def test_refresh_answer_without_access_token_is_a_temporary_failure(cfg, db):
    seed_oauth(db[1], access="old", refresh="r1")
    gw = Gateway(cfg, db[1], http=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"oops": 1}))))
    assert await gw.backend.on_unauthorized("old") is False
    assert gw.backend.describe().healthy   # not marked as needing a login; retried after the backoff


async def test_two_users_sharing_a_session_id_stay_separate(cfg, db):
    from argon2 import PasswordHasher
    from claude_proxy.web import create_dashboard_app
    conn = db[1]
    create_user(conn, "admin", role="admin", password_hash=PasswordHasher().hash("correct horse battery"))
    a, _ = create_user(conn, "ann")
    b, _ = create_user(conn, "ben")
    import time
    for uid, tokens in ((a, 100), (b, 900)):
        conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, model, input_tokens, session_id) "
                     "VALUES(?,?,?,?,?,?,?,?)", (uid, time.time() - 60, time.time() - 50, "POST", "/v1/messages", "claude-sonnet-5", tokens, "same"))
    gw = make_gateway(cfg, conn)
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "correct horse battery"})
        c.headers["x-csrf-token"] = r.json()["csrf"]
        sessions = (await c.get("/api/sessions")).json()["sessions"]
    assert sorted((s["user"], s["raw"]) for s in sessions) == [("ann", 100), ("ben", 900)]
