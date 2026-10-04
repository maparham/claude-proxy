import time

import pytest

from claude_proxy import cli, db as dbm, tickets
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client, make_gateway
from tests.test_web import PW, admin_client
from tests.tickets_helpers import DAY, NOW, seeded, user


def test_delete_refused_with_a_live_ticket_and_keeps_records_after(db):
    conn, cfg, ids = seeded(db)
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=NOW)
    dbm.revoke(conn, ids["alice"])
    with pytest.raises(ValueError, match="active or queued ticket"):
        dbm.delete_user(conn, ids["alice"], ids["admin"], now=NOW + 10)
    dbm.delete_user(conn, ids["alice"], ids["admin"], now=NOW + 2 * DAY)         # ended: allowed
    row = conn.execute("SELECT user_id, user_name FROM tickets WHERE id=?", (t["id"],)).fetchone()
    assert tuple(row) == (None, "alice")


async def test_web_delete_needs_the_typed_name(db, cfg):
    conn, tcfg_, ids = seeded(db)
    from argon2 import PasswordHasher
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (PasswordHasher().hash(PW), ids["admin"]))
    cfg.tickets = tcfg_.tickets
    gw = make_gateway(cfg, conn)
    # the endpoint uses the wall clock, so the ticket must have ended in real time
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "USD", now=int(time.time()) - 2 * DAY)
    dbm.revoke(conn, ids["alice"])
    async with admin_client(gw) as c:
        r = await c.post(f"/api/admin/users/{ids['alice']}/delete", json={})
        assert r.status_code == 400 and "Type the user's name" in r.json()["error"]
        r = await c.post(f"/api/admin/users/{ids['alice']}/delete", json={"confirm": "Alice"})
        assert r.status_code == 400
        r = await c.post(f"/api/admin/users/{ids['alice']}/delete", json={"confirm": "alice"})
        assert r.status_code == 200
    assert conn.execute("SELECT user_name FROM tickets WHERE id=?", (t["id"],)).fetchone()[0] == "alice"


def test_cli_delete_asks_for_the_name_unless_yes(db, monkeypatch, capsys):
    conn, cfg, ids = seeded(db)
    dbm.revoke(conn, ids["alice"])
    monkeypatch.setattr(cli, "_conn", lambda cfg: conn)
    monkeypatch.setattr("builtins.input", lambda prompt="": "nope")
    with pytest.raises(SystemExit):
        cli.main(["user", "delete", "alice"])
    assert conn.execute("SELECT 1 FROM users WHERE name='alice'").fetchone()
    monkeypatch.setattr("builtins.input", lambda prompt="": "alice")
    cli.main(["user", "delete", "alice"])
    assert conn.execute("SELECT 1 FROM users WHERE name='alice'").fetchone() is None
    bob, _ = dbm.create_user(conn, "bob")
    dbm.revoke(conn, bob)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("--yes must not prompt"))
    cli.main(["user", "delete", "bob", "--yes"])
    assert "Deleted bob" in capsys.readouterr().out
