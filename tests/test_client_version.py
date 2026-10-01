"""gclaude's statusline sends its version (X-Gclaude-Version) with /api/me/status; the dashboard lists it with the
computer's key."""
import pytest

from claude_proxy.db import add_machine_key, machine_keys
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import bearer, env  # noqa: F401  (fixture)

pytestmark = pytest.mark.anyio


async def test_a_computers_reported_version_is_listed_with_its_key(env):  # noqa: F811
    gw, conn, ids, keys = env
    mac = add_machine_key(conn, ids["alice"], "MacBook")
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.get("/api/me/status", headers=bearer(mac))).status_code == 200   # an older gclaude: no header
        assert machine_keys(conn, ids["alice"])[0]["client_version"] is None
        for sent, kept in (("1.0.7", "1.0.7"), ("", "1.0.7"), ("1.0.8; drop table", "1.0.7"), ("1.0.8", "1.0.8")):
            r = await c.get("/api/me/status", headers={**bearer(mac), "X-Gclaude-Version": sent})
            assert r.status_code == 200
            assert machine_keys(conn, ids["alice"])[0]["client_version"] == kept, sent
        # The first key isn't a computer's: nothing to record, and no error.
        r = await c.get("/api/me/status", headers={**bearer(keys["alice"]), "X-Gclaude-Version": "1.0.8"})
        assert r.status_code == 200
        r = await c.get("/api/keys", headers=bearer(mac))
        assert r.json()["keys"][0]["client_version"] == "1.0.8"
