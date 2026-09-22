import httpx
import pytest

from claude_proxy import login
from claude_proxy.credentials import OAuthBackend


def token_endpoint(seen):
    def handler(request):
        seen.append(request.content.decode())
        return httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 28800,
                                         "account": {"email_address": "owner@example.com"}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_two_step_login_uses_the_stored_verifier(cfg, db):
    conn = db[1]
    url = login.start(conn, cfg)
    pending = login._pending(conn)
    assert f"state={pending['state']}" in url
    seen = []
    async with token_endpoint(seen) as http:
        record = await login.finish(conn, cfg, http, f"THECODE#{pending['state']}")
    assert record["account"] == "owner@example.com"
    import json as _json
    sent = _json.loads(seen[0])
    assert (sent["code_verifier"], sent["code"], sent["state"]) == (pending["verifier"], "THECODE", pending["state"])
    assert OAuthBackend(cfg, conn, http).current_token() == "acc"
    with pytest.raises(login.LoginError):
        async with token_endpoint([]) as http:
            await login.finish(conn, cfg, http, "THECODE")


async def test_pending_login_expires(cfg, db, monkeypatch):
    conn = db[1]
    login.start(conn, cfg)
    monkeypatch.setattr(login, "PENDING_TTL_S", -1)
    with pytest.raises(login.LoginError, match="expired"):
        async with token_endpoint([]) as http:
            await login.finish(conn, cfg, http, "X")


async def test_rate_limited_exchange_keeps_the_attempt_for_a_retry(cfg, db):
    conn = db[1]
    login.start(conn, cfg)
    state = login._pending(conn)["state"]
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"type": "rate_limit_error"}})
        return httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 60})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(login.LoginError, match="try again"):
            await login.finish(conn, cfg, http, f"C#{state}")
        record = await login.finish(conn, cfg, http, f"C#{state}")
    assert record["access_token"] == "acc"
    assert login._pending(conn) is None
