import time

import httpx
from argon2 import PasswordHasher
from fastapi.responses import JSONResponse

from claude_proxy import upstream
from claude_proxy.db import create_user, insert_request
from claude_proxy.web import create_dashboard_app
from tests.conftest import FakeUpstream, asgi_client, make_gateway, mounted_client, seed_oauth

PW = "correct horse battery"


def add(conn, ago, status=200, error_type=None, provider="anthropic", rejected_by=None, now=None):
    now = now or time.time()
    insert_request(conn, user_id=None, started_at=now - ago, ended_at=now - ago, method="POST", path="/v1/messages",
                   provider=provider, status=status, error_type=error_type, rejected_by=rejected_by)


def test_quiet_window_is_not_overloaded(db):
    conn = db[1]
    now = time.time()
    for a in range(10):
        add(conn, a, now=now)
    add(conn, 5, 529, now=now)
    add(conn, 6, 529, now=now)
    o = upstream.overload(conn, now)
    assert o["failed"] == 2 and o["total"] == 12 and not o["overloaded"]


def test_529s_and_stream_overloaded_errors_count(db):
    conn = db[1]
    now = time.time()
    add(conn, 300, 529, now=now)
    add(conn, 200, 200, "overloaded_error", now=now)
    add(conn, 100, 529, now=now)
    add(conn, 50, now=now)
    o = upstream.overload(conn, now)
    assert o == {"overloaded": True, "failed": 3, "total": 4, "since": now - 300, "last_at": now - 100}


def test_low_share_old_rows_other_providers_and_gateway_rejections_are_ignored(db):
    conn = db[1]
    now = time.time()
    for a in range(3):
        add(conn, 1000 + a, 529, now=now)                       # outside the window
        add(conn, 10 + a, 529, provider="meta", now=now)        # another provider
        add(conn, 20 + a, 429, rejected_by="tokens_5h", now=now)
    assert upstream.overload(conn, now)["total"] == 0
    for a in range(3):
        add(conn, a, 529, now=now)
    for a in range(20):
        add(conn, a, now=now)
    o = upstream.overload(conn, now)
    assert o["failed"] == 3 and not o["overloaded"]   # 3 of 23 is under 20%


SUMMARY = {"status": {"indicator": "minor", "description": "Minor Service Outage"},
           "incidents": [{"name": "Elevated errors on Opus", "status": "investigating", "impact": "minor", "shortlink": "https://stspg.io/x"},
                         {"name": "Old", "status": "resolved", "impact": "major", "shortlink": "https://stspg.io/y"}]}


def test_parse_summary_keeps_open_incidents_only():
    assert upstream.parse_summary(SUMMARY) == {"indicator": "minor", "description": "Minor Service Outage",
                                               "incidents": [{"name": "Elevated errors on Opus", "impact": "minor", "url": "https://stspg.io/x"}]}


async def test_status_page_poll_and_failure():
    up = FakeUpstream("status")
    up.default = lambda req: JSONResponse(SUMMARY)
    sp = upstream.StatusPage(mounted_client(up), url="http://status/api/v2/summary.json")
    await sp.tick(now=1000)
    assert sp.view(now=1100)["indicator"] == "minor"
    assert sp.view(now=1000 + 901) is None   # too old to show
    up.default = lambda req: JSONResponse({}, status_code=500)
    await sp.tick(now=1200)
    assert sp.view(now=1200) is None


async def test_overview_upstream_is_admin_only(cfg, db):
    conn = db[1]
    create_user(conn, "admin", role="admin", password_hash=PasswordHasher().hash(PW))
    _, alice_key = create_user(conn, "alice")
    seed_oauth(conn)
    for a in range(3):
        add(conn, a, 529)
    gw = make_gateway(cfg, conn)
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert "upstream" not in (await c.get("/api/overview", headers={"Authorization": f"Bearer {alice_key}"})).json()
        r = await c.post("/api/login", json={"username": "admin", "password": PW})
        c.headers["x-csrf-token"] = r.json()["csrf"]
        u = (await c.get("/api/overview")).json()["upstream"]
    assert u["overloaded"] and u["failed"] == 3 and u["status_page"] is None
