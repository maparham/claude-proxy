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
    assert f"code_verifier={pending['verifier']}" in seen[0] and "code=THECODE" in seen[0]
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
