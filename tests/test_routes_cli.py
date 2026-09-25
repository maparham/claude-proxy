from claude_proxy import cli
from claude_proxy.db import create_user, find_user_by_key, revoke

import pytest


def test_routes_key_issue_list_remove(db, monkeypatch, capsys):
    path, conn = db
    monkeypatch.setenv("CLAUDE_PROXY_DB", path)
    uid, _ = create_user(conn, "alice")
    cli.main(["user", "routes-key", "alice"])
    key = capsys.readouterr().out.strip().splitlines()[-1]
    assert find_user_by_key(conn, key)["key_scope"] == "routes"
    cli.main(["user", "list"])
    assert f"opencode {key[:14]}…" in capsys.readouterr().out
    cli.main(["user", "routes-key", "alice", "--remove"])
    assert "removed" in capsys.readouterr().out
    assert find_user_by_key(conn, key) is None
    cli.main(["user", "routes-key", "alice", "--remove"])
    assert "has no OpenCode key" in capsys.readouterr().out


def test_routes_key_for_a_revoked_user_exits_with_a_message(db, monkeypatch):
    path, conn = db
    monkeypatch.setenv("CLAUDE_PROXY_DB", path)
    uid, _ = create_user(conn, "alice")
    revoke(conn, uid)
    with pytest.raises(SystemExit, match="revoked"):
        cli.main(["user", "routes-key", "alice"])
