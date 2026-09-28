"""Review fixes for machine keys: rotating the first key ends every machine key too, and a computer's key may
only sign itself out, not the owner's other computers."""
import pytest

from claude_proxy.db import (add_machine_key, create_session, create_user, find_session, find_user_by_key, machine_keys,
                             rotate_key)
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import admin_client, bearer, env  # noqa: F401  (fixture)

pytestmark = pytest.mark.anyio


def key_id(conn, raw):
    return find_user_by_key(conn, raw)["machine_key_id"]


def test_rotate_key_revokes_machine_keys_and_their_sessions(db):
    """Shared by the web route and `claude-gateway user rotate`."""
    conn = db[1]
    uid, first = create_user(conn, "alice")
    machine = add_machine_key(conn, uid, "MacBook")
    sess, _ = create_session(conn, uid, key_id=key_id(conn, machine))
    rotate_key(conn, uid)
    assert find_user_by_key(conn, first) is None
    assert find_user_by_key(conn, machine) is None
    assert find_session(conn, sess) is None
    assert machine_keys(conn, uid) == []


async def test_admin_rotate_ends_machine_keys_minted_from_the_leaked_first_key(env):  # noqa: F811
    gw, conn, ids, keys = env
    machine = add_machine_key(conn, ids["alice"], "MacBook")
    async with asgi_client(create_dashboard_app(gw)) as mc:
        assert (await mc.post("/api/login/key", json={"key": machine})).status_code == 200
        assert (await mc.get("/api/me/status")).status_code == 200
        async with admin_client(gw) as c:
            r = await c.post(f"/api/admin/users/{ids['alice']}/rotate")
            assert r.status_code == 200 and r.json()["key"] != keys["alice"]
        assert (await mc.get("/api/me/status", headers=bearer(machine))).status_code == 401
        assert (await mc.get("/api/me/status")).status_code == 401   # the session made from it ended with it
        assert (await mc.get("/api/me/status", headers=bearer(keys["alice"]))).status_code == 401
        assert (await mc.get("/api/me/status", headers=bearer(r.json()["key"]))).status_code == 200


async def test_a_machine_key_may_only_remove_itself(env):  # noqa: F811
    gw, conn, ids, keys = env
    mac = add_machine_key(conn, ids["alice"], "MacBook")
    desk = add_machine_key(conn, ids["alice"], "Desktop")
    mac_id, desk_id = key_id(conn, mac), key_id(conn, desk)
    async with asgi_client(create_dashboard_app(gw)) as c:
        # As a Bearer key: a leaked laptop key cannot sign the desktop out.
        assert (await c.post(f"/api/keys/{desk_id}/remove", headers=bearer(mac))).status_code == 403
        assert find_user_by_key(conn, desk) is not None
        # Nor may a dashboard session made from that key.
        r = await c.post("/api/login/key", json={"key": mac})
        assert r.status_code == 200
        c.headers["x-csrf-token"] = r.json()["csrf"]
        assert (await c.post(f"/api/keys/{desk_id}/remove")).status_code == 403
        assert find_user_by_key(conn, desk) is not None
        # It may remove itself.
        r = await c.post(f"/api/keys/{mac_id}/remove")
        assert r.status_code == 200 and r.json() == {"ok": True, "removed": True}
        assert find_user_by_key(conn, mac) is None


async def test_the_first_key_session_removes_any_of_its_machines(env):  # noqa: F811
    gw, conn, ids, keys = env
    mac = add_machine_key(conn, ids["alice"], "MacBook")
    desk = add_machine_key(conn, ids["alice"], "Desktop")
    mac_id, desk_id = key_id(conn, mac), key_id(conn, desk)
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/api/login/key", json={"key": keys["alice"]})
        assert r.status_code == 200
        c.headers["x-csrf-token"] = r.json()["csrf"]
        assert (await c.post(f"/api/keys/{desk_id}/remove")).status_code == 200
        assert (await c.post(f"/api/keys/{mac_id}/remove", headers=bearer(keys["alice"]))).status_code == 200
    assert machine_keys(conn, ids["alice"]) == []
    # Another user's machine stays unknown, not forbidden: the status gives nothing away.
    other = add_machine_key(conn, ids["bob"], "Bob's laptop")
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.post(f"/api/keys/{key_id(conn, other)}/remove", headers=bearer(keys["alice"]))).status_code == 404
