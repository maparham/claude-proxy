import pytest

from claude_proxy.app import create_app
from claude_proxy.credentials import build_authorize_url, decrypt_blob, encrypt_blob, generate_pkce, parse_pasted_code
from claude_proxy.db import create_user
from claude_proxy.forwarder import filter_request_headers, filter_response_headers, merge_beta
from tests.conftest import asgi_client, make_gateway


def test_request_headers_drop_hop_by_hop_and_client_credentials():
    out = filter_request_headers({"connection": "keep-alive, x-foo", "x-foo": "bar", "x-keep": "yes",
                                  "authorization": "Bearer xyz", "x-api-key": "abc", "host": "gw", "content-length": "5",
                                  "content-type": "application/json"})
    assert out == {"x-keep": "yes", "content-type": "application/json"}


def test_response_headers_drop_framing():
    out = filter_response_headers({"transfer-encoding": "chunked", "content-encoding": "gzip", "content-length": "9",
                                   "content-type": "application/json", "request-id": "req_1"})
    assert out == {"content-type": "application/json", "request-id": "req_1"}


def test_merge_beta():
    assert merge_beta(None, "oauth-2025-04-20") == "oauth-2025-04-20"
    assert merge_beta("a, oauth-2025-04-20", "oauth-2025-04-20") == "a, oauth-2025-04-20"
    assert merge_beta("a,b", "c") == "a, b, c"


def test_encrypt_roundtrip():
    assert decrypt_blob(encrypt_blob({"access_token": "a"}))["access_token"] == "a"


def test_pkce_and_authorize_url(cfg):
    verifier, challenge, state = generate_pkce()
    url = build_authorize_url(cfg, challenge, state)
    assert url.startswith("https://claude.ai/oauth/authorize?")
    assert f"code_challenge={challenge}" in url and "code_challenge_method=S256" in url and f"state={state}" in url


@pytest.mark.parametrize("pasted,code", [
    ("abc123", "abc123"),
    ("abc123#STATE", "abc123"),
    ("https://platform.claude.com/oauth/code/callback?code=abc123&state=STATE", "abc123"),
])
def test_parse_pasted_code(pasted, code):
    assert parse_pasted_code(pasted, "STATE") == code


def test_parse_pasted_code_rejects_wrong_state():
    with pytest.raises(ValueError):
        parse_pasted_code("abc#OTHER", "STATE")


async def test_missing_key_is_401_and_recorded(cfg, db):
    gw = make_gateway(cfg, db[1])
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models")
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
    assert db[1].execute("SELECT rejected_by FROM requests").fetchone()[0] == "auth"


async def test_unknown_path_is_404_and_never_forwarded(cfg, db, anthropic):
    _, key = create_user(db[1], "alice")
    gw = make_gateway(cfg, db[1], anthropic)
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/teamclaude/dashboard", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 404
    assert anthropic.calls == []


async def test_unlinked_credential_is_503(cfg, db, anthropic):
    _, key = create_user(db[1], "alice")
    gw = make_gateway(cfg, db[1], anthropic)
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 503 and "claude-proxy login" in r.json()["error"]["message"]
    assert anthropic.calls == []
