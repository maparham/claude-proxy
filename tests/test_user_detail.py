"""The admin's per-user page: every read endpoint takes `user_id`, which only admins may point at someone else."""
import time

import pytest

from claude_proxy.db import set_session_title
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import admin_client, bearer, env  # noqa: F401  (fixture)


@pytest.fixture
def seeded(env):  # noqa: F811
    gw, conn, ids, keys = env
    conn.execute("INSERT INTO requests(user_id, started_at, method, path, model, status, rejected_by) VALUES(?,?,?,?,?,?,?)",
                 (ids["bob"], time.time() - 60, "POST", "/v1/messages", "claude-sonnet-5", 429, "requests_daily"))
    set_session_title(conn, ids["alice"], f"s-{ids['alice']}", "Fix the login page")
    return env


async def fetch_all(c, uid, headers=None):
    q = f"user_id={uid}"
    get = lambda p: c.get(p, headers=headers or {})  # noqa: E731
    return {
        "overview": (await get(f"/api/overview?{q}")).json(),
        "series": (await get(f"/api/series?range=1d&granularity=hour&split=model&{q}")).json(),
        "models": (await get(f"/api/models?range=7d&{q}")).json(),
        "heatmap": (await get(f"/api/heatmap?range=7d&{q}")).json(),
        "sessions": (await get(f"/api/sessions?range=7d&{q}")).json(),
        "errors": (await get(f"/api/errors?range=7d&{q}")).json(),
        "requests": (await get(f"/api/requests?{q}")).json(),
    }


async def test_admin_scopes_every_read_endpoint_to_one_user(seeded):
    gw, conn, ids, keys = seeded
    async with admin_client(gw) as c:
        d = await fetch_all(c, ids["alice"])
    assert d["overview"]["totals"]["24h"]["requests"] == 2
    assert {p["key"] for p in d["series"]["points"]} == {"claude-opus-5", "muse-spark-1.3"}
    assert {m["model"] for m in d["models"]["models"]} == {"claude-opus-5", "muse-spark-1.3"}
    assert sum(cell[2] for cell in d["heatmap"]["cells"]) == 2
    assert [s["session_id"] for s in d["sessions"]["sessions"]] == [f"s-{ids['alice']}"]
    assert d["errors"]["recent"] == []
    assert {r["model"] for r in d["requests"]["requests"]} == {"claude-opus-5", "muse-spark-1.3"}


async def test_admin_sees_the_users_errors(seeded):
    gw, conn, ids, keys = seeded
    async with admin_client(gw) as c:
        d = (await c.get(f"/api/errors?range=7d&user_id={ids['bob']}")).json()
    assert [(r["k"], r["rejected_by"]) for r in d["recent"]] == [("gateway_limit", "requests_daily")]


async def test_non_admin_cannot_look_at_another_user(seeded):
    gw, conn, ids, keys = seeded
    async with asgi_client(create_dashboard_app(gw)) as c:
        d = await fetch_all(c, ids["alice"], headers=bearer(keys["bob"]))
    assert d["overview"]["totals"]["24h"]["requests"] == 1
    assert {p["key"] for p in d["series"]["points"]} == {"claude-sonnet-5"}
    assert [s["session_id"] for s in d["sessions"]["sessions"]] == [f"s-{ids['bob']}"]
    assert {r["model"] for r in d["requests"]["requests"]} == {"claude-sonnet-5"}
    assert "Fix the login page" not in str(d)


async def test_unknown_user_is_404(seeded):
    gw, *_ = seeded
    async with admin_client(gw) as c:
        assert (await c.get("/api/overview?user_id=9999")).status_code == 404
        assert (await c.get("/api/requests?user_id=9999")).status_code == 404


async def test_recent_requests_are_newest_first_priced_and_titled(seeded):
    gw, conn, ids, keys = seeded
    async with admin_client(gw) as c:
        rows = (await c.get(f"/api/requests?user_id={ids['bob']}")).json()["requests"]
        everyone = (await c.get("/api/requests?limit=2")).json()["requests"]
    refused, served = rows
    assert refused["status"] == 429 and refused["kind"] == "gateway_limit" and refused["weighted"] == 0
    assert served["status"] == 200 and served["kind"] is None
    assert (served["input"], served["output"]) == (300, 50) and served["weighted"] > 0 and served["cost_usd"] > 0
    assert served["duration_s"] == pytest.approx(1)
    assert len(everyone) == 2 and all(r["user"] for r in everyone)
    async with admin_client(gw) as c:
        alice = (await c.get(f"/api/requests?user_id={ids['alice']}")).json()["requests"]
    assert {r["title"] for r in alice} == {"Fix the login page"}
