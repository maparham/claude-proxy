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
    assert url.startswith("https://claude.com/cai/oauth/authorize?code=true&")
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
    _, key = create_user(db[1], "alice", role="admin")
    gw = make_gateway(cfg, db[1], anthropic)
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 503 and "claude-proxy login" in r.json()["error"]["message"]
    assert anthropic.calls == []


async def test_token_request_sends_json_like_claude_code(cfg):
    import json as _json
    import httpx
    from claude_proxy.credentials import token_request
    seen = []

    def handler(request):
        seen.append((request.headers["content-type"], _json.loads(request.content)))
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 60})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        r = await token_request(http, cfg, {"grant_type": "refresh_token", "refresh_token": "x"})
    assert r.status_code == 200 and len(seen) == 1
    ctype, body = seen[0]
    assert ctype == "application/json"
    assert body == {"grant_type": "refresh_token", "refresh_token": "x", "client_id": cfg.credential.client_id}


async def test_refresh_sends_refresh_scopes(cfg, db):
    import json as _json
    import httpx
    from claude_proxy.gateway import Gateway
    from tests.conftest import seed_oauth
    seed_oauth(db[1], access="old", refresh="r1")
    bodies = []

    def handler(request):
        bodies.append(_json.loads(request.content))
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 28800})

    gw = Gateway(cfg, db[1], http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await gw.backend.on_unauthorized("old")
    assert bodies[0]["scope"] == cfg.credential.refresh_scopes
    assert bodies[0]["grant_type"] == "refresh_token" and bodies[0]["refresh_token"] == "r1"


async def test_backend_picks_up_login_done_by_cli_while_running(cfg, db):
    import time as _t
    from claude_proxy.credentials import NeedsLogin
    from tests.conftest import seed_oauth
    gw = make_gateway(cfg, db[1])
    with pytest.raises(NeedsLogin):
        await gw.backend.upstream_headers()
    seed_oauth(db[1], access="new-token")
    assert (await gw.backend.upstream_headers())["authorization"] == "Bearer new-token"
    db[1].execute("UPDATE credentials SET encrypted_blob=?, updated_at=? WHERE backend='oauth'",
                  (__import__("claude_proxy.credentials", fromlist=["x"]).encrypt_blob(
                      {"access_token": "newer", "refresh_token": "r", "expires_at": int(_t.time()) + 3600}), int(_t.time()) + 5))
    assert (await gw.backend.upstream_headers())["authorization"] == "Bearer newer"


def test_example_config_loads():
    from pathlib import Path
    from claude_proxy.config import Config
    cfg = Config.load(str(Path(__file__).parent.parent / "config.example.toml"))
    assert cfg.retention_days == 180
    assert cfg.route_for("muse-spark").upstream_model("muse-spark") == "muse-spark-1.3"
    assert cfg.route_for("claude-opus-5") is None


def test_unknown_config_key_is_an_error(tmp_path):
    from claude_proxy.config import Config, ConfigError
    p = tmp_path / "c.toml"
    p.write_text("[listener]\nprot = 1\n")
    with pytest.raises(ConfigError):
        Config.load(str(p))


def test_database_files_are_private(tmp_path):
    import os, stat
    from claude_proxy.db import init_db
    p = tmp_path / "x.db"
    c = init_db(str(p))
    c.execute("CREATE TABLE IF NOT EXISTS t(x)")
    for f in tmp_path.glob("x.db*"):
        assert stat.S_IMODE(os.stat(f).st_mode) & 0o077 == 0, f.name


def test_defaults_to_home_gateway_dir_when_present(tmp_path, monkeypatch):
    import importlib
    from claude_proxy import config, credentials
    home = tmp_path / "home"
    (home / ".claude-gateway").mkdir(parents=True)
    keyfile = home / ".claude-gateway" / "gateway.key"
    keyfile.write_text(credentials.generate_key())
    keyfile.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PROXY_DB", raising=False)
    monkeypatch.delenv("CLAUDE_PROXY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_PROXY_CREDENTIAL_KEY_FILE", raising=False)
    assert config.Config().db.path == str(home / ".claude-gateway" / "claude_proxy.db")
    assert credentials.decrypt_blob(credentials.encrypt_blob({"a": 1})) == {"a": 1}
