"""Multiple subscription accounts (spec 2026-10-13, step 1: storage + login)."""
import argparse
import base64
import os
import sqlite3

import httpx
import pytest

from claude_proxy import db, login
from claude_proxy.config import Config
from claude_proxy.credentials import OAuthBackend, encrypt_blob, list_accounts
from tests.conftest import FakeUpstream, mounted_client, seed_oauth


def mock_token(account: str, sub: str):
    def handler(request):
        return httpx.Response(200, json={"access_token": f"acc-{account}", "refresh_token": f"ref-{account}",
                                         "expires_in": 3600,
                                         "account": {"id": sub, "email_address": f"{account}@x.com"}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_two_accounts_login_side_by_side(cfg, db):
    conn = db[1]
    url_a = login.start(conn, cfg, "default")
    url_b = login.start(conn, cfg, "second")
    assert url_a != url_b  # distinct PKCE states
    assert login._pending(conn, "default")["state"] in url_a
    assert login._pending(conn, "second")["state"] in url_b

    async with mock_token("default", "sub-1") as http:
        await login.finish(conn, cfg, http, f"C1#{login._pending(conn, 'default')['state']}", "default")
    # The second pending survived the first finish.
    assert login._pending(conn, "second") is not None
    async with mock_token("second", "sub-2") as http:
        await login.finish(conn, cfg, http, f"C2#{login._pending(conn, 'second')['state']}", "second")
    assert OAuthBackend(cfg, conn, None, "default").current_token() == "acc-default"
    assert OAuthBackend(cfg, conn, None, "second").current_token() == "acc-second"
    assert {r["account"] for r in list_accounts(conn)} == {"default", "second"}


async def test_duplicate_subscription_refused(cfg, db):
    conn = db[1]
    login.start(conn, cfg, "default")
    login.start(conn, cfg, "second")
    async with mock_token("default", "same-sub") as http:
        await login.finish(conn, cfg, http, f"C#{login._pending(conn, 'default')['state']}", "default")
    async with mock_token("second", "same-sub") as http:
        with pytest.raises(login.LoginError, match="already linked"):
            await login.finish(conn, cfg, http, f"C#{login._pending(conn, 'second')['state']}", "second")


async def test_legacy_single_row_migrates_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROXY_CREDENTIAL_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    path = str(tmp_path / "legacy.db")
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE credentials (backend TEXT PRIMARY KEY, encrypted_blob TEXT, expires_at INTEGER, "
                   "updated_at INTEGER, state TEXT)")
    blob = encrypt_blob({"access_token": "old-acc", "refresh_token": "old-ref", "expires_at": 999})
    legacy.execute("INSERT INTO credentials VALUES('oauth',?,?,?, 'active')", (blob, 999, 1))
    legacy.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    legacy.execute("INSERT INTO settings VALUES('pending_login', 'stale')")
    legacy.commit()
    legacy.close()
    conn = db.init_db(path)
    try:
        row = conn.execute("SELECT account, state FROM credentials").fetchone()
        assert (row["account"], row["state"]) == ("default", "active")
        assert OAuthBackend(Config(), conn, None).current_token() == "old-acc"
        assert conn.execute("SELECT 1 FROM settings WHERE key='pending_login'").fetchone() is None
    finally:
        conn.close()


async def test_refresh_on_one_account_leaves_the_other_alone(cfg, db):
    conn = db[1]
    seed_oauth(conn, access="old-a", refresh="ref-a", expires_in=-10, account="default")
    seed_oauth(conn, access="old-b", refresh="ref-b", expires_in=-10, account="second")
    seen = []
    auth = FakeUpstream("auth.fake")

    def handler(req):
        from fastapi.responses import JSONResponse
        seen.append(req["json"]["refresh_token"])
        tok = "new-a" if seen[-1] == "ref-a" else "new-b"
        return JSONResponse({"access_token": tok, "refresh_token": seen[-1], "expires_in": 3600})

    auth.default = handler
    cfg.credential.token_url = "http://auth.fake/token"
    from claude_proxy.gateway import Gateway
    gw = Gateway(cfg, conn, http=mounted_client(auth))
    try:
        headers = await OAuthBackend(cfg, conn, gw.http, "default").upstream_headers()
    finally:
        await gw.aclose()
    assert headers["authorization"] == "Bearer new-a"
    assert OAuthBackend(cfg, conn, gw.http, "second").current_token() == "old-b"
    assert seen == ["ref-a"]


def test_account_remove_refuses_the_last_row(cfg, db):
    from claude_proxy import cli
    seed_oauth(db[1], access="a", refresh="r", account="second")
    with pytest.raises(SystemExit, match="last linked"):
        cli.cmd_account_remove(argparse.Namespace(account="second"), cfg)
    seed_oauth(db[1], access="b", refresh="r2", account="third")
    cli.cmd_account_remove(argparse.Namespace(account="third"), cfg)
    assert [r["account"] for r in list_accounts(db[1])] == ["second"]


def test_account_remove_refuses_default(cfg, db):
    from claude_proxy import cli
    seed_oauth(db[1], access="a", refresh="r")
    seed_oauth(db[1], access="b", refresh="r2", account="second")
    with pytest.raises(SystemExit, match="serves all traffic"):
        cli.cmd_account_remove(argparse.Namespace(account="default"), cfg)
    assert {r["account"] for r in list_accounts(db[1])} == {"default", "second"}


async def test_refresh_without_account_keeps_identity(cfg, db):
    conn = db[1]
    async with mock_token("default", "sub-1") as http:
        login.start(conn, cfg, "default")
        await login.finish(conn, cfg, http, f"C1#{login._pending(conn, 'default')['state']}", "default")
    conn.execute("UPDATE credentials SET expires_at=0")
    be = OAuthBackend(cfg, conn, None, "default")
    be._cache = None
    data, _ = be._load()
    data["expires_at"] = 0
    be.store(data)

    def handler(request):  # a refresh answer with no `account` block
        return httpx.Response(200, json={"access_token": "acc-new", "refresh_token": "ref-new", "expires_in": 3600})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        be.http = http
        await be.upstream_headers()
    assert be.current_token() == "acc-new"
    row = conn.execute("SELECT anthropic_sub, email FROM credentials WHERE account='default'").fetchone()
    assert (row["anthropic_sub"], row["email"]) == ("sub-1", "default@x.com")
    assert be._load()[0]["anthropic_sub"] == "sub-1"
    # The kept sub still blocks linking the same subscription under another slug.
    async with mock_token("second", "sub-1") as http:
        login.start(conn, cfg, "second")
        with pytest.raises(login.LoginError, match="already linked"):
            await login.finish(conn, cfg, http, f"C2#{login._pending(conn, 'second')['state']}", "second")


def test_unique_subscription_enforced_by_schema(cfg, db):
    seed_oauth(db[1], access="a", refresh="r")
    db[1].execute("UPDATE credentials SET anthropic_sub='sub-1'")
    be = OAuthBackend(cfg, db[1], None, "second")
    with pytest.raises(sqlite3.IntegrityError):
        be.store({"access_token": "b", "refresh_token": "r2", "expires_at": 9e9, "anthropic_sub": "sub-1"})


def test_status_flags_missing_default(cfg, db, capsys):
    from claude_proxy import cli
    seed_oauth(db[1], access="a", refresh="r", account="second")
    cli.cmd_status(argparse.Namespace(), cfg)
    out = capsys.readouterr().out
    assert "credential [default]: NOT OK" in out and "credential [second]: OK" in out


def test_invalid_account_slug_rejected(cfg, db):
    with pytest.raises(ValueError, match="account"):
        login.start(db[1], cfg, "NOPE CAPS")
