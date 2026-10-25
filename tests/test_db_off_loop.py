"""The proxy's SQLite work runs off the event loop: a slow limit check for one user delays no one else, concurrent
requests still see each other's slots, and a cancelled request is still recorded."""
import asyncio
import threading
import time

import httpx
import pytest

from claude_proxy import app as app_mod
from claude_proxy import limits
from claude_proxy.app import create_app, inflight_of
from claude_proxy.db import create_user, get_conn
from claude_proxy.gateway import Gateway
from claude_proxy.dbpool import DbPool
from tests.conftest import asgi_client, make_gateway, mounted_client, seed_oauth
from tests.test_review_limits import MSG


@pytest.fixture
def setup(cfg, db, anthropic):
    conn = db[1]
    seed_oauth(conn)
    keys = {}
    for name in ("alice", "bob"):
        uid, key = create_user(conn, name)
        keys[name] = (uid, {"Authorization": f"Bearer {key}"})
    return make_gateway(cfg, conn, anthropic), conn, keys


async def test_a_slow_limit_check_for_one_user_does_not_hold_up_another(setup, monkeypatch):
    gw, conn, keys = setup
    alice = keys["alice"][0]
    real = limits.evaluate

    def slow_for_alice(conn, cfg, user_id, *a, **kw):
        if user_id == alice:
            time.sleep(1.0)   # a synchronous query that takes a second
        return real(conn, cfg, user_id, *a, **kw)

    monkeypatch.setattr(limits, "evaluate", slow_for_alice)
    async with asgi_client(create_app(gw)) as c:
        slow = asyncio.create_task(c.post("/v1/messages", json=MSG, headers=keys["alice"][1]))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        fast = await c.post("/v1/messages", json=MSG, headers=keys["bob"][1])
        took = time.monotonic() - t0
        assert not slow.done()
        assert (await slow).status_code == 200
    assert fast.status_code == 200 and took < 0.5


async def test_the_loop_keeps_running_while_a_query_runs(setup, monkeypatch):
    gw, conn, keys = setup
    monkeypatch.setattr(limits, "evaluate", lambda *a, **kw: time.sleep(0.5))
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(ticker())
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=keys["alice"][1])
    t.cancel()
    assert r.status_code == 200 and ticks >= 20


async def test_concurrent_requests_count_each_other_while_their_checks_run(setup, monkeypatch):
    gw, conn, keys = setup
    gw.cfg.limits.max_inflight = 1
    real = limits.evaluate
    monkeypatch.setattr(limits, "evaluate", lambda *a, **kw: (time.sleep(0.2), real(*a, **kw))[1])
    async with asgi_client(create_app(gw)) as c:
        rs = await asyncio.gather(*(c.post("/v1/messages", json=MSG, headers=keys["alice"][1]) for _ in range(2)))
    assert sorted(r.status_code for r in rs) == [200, 429]
    assert inflight_of(gw).get(keys["alice"][0]) == 0


async def test_a_cancelled_request_is_still_recorded_and_frees_its_slot(setup, monkeypatch):
    gw, conn, keys = setup
    uid, h = keys["alice"]

    def unreachable(request):
        raise httpx.ConnectError("down")

    gw.http = httpx.AsyncClient(transport=httpx.MockTransport(unreachable))   # a 502, recorded while holding its slot
    real = app_mod._save
    started = threading.Event()

    def slow_save(*a):
        started.set()
        time.sleep(0.3)
        real(*a)

    monkeypatch.setattr(app_mod, "_save", slow_save)
    async with asgi_client(create_app(gw)) as c:
        task = asyncio.create_task(c.post("/v1/messages", json=MSG, headers=h))
        while not started.is_set():
            await asyncio.sleep(0.01)
        assert inflight_of(gw).get(uid) == 1
        task.cancel()   # the client went away while the row is being written
        with pytest.raises(asyncio.CancelledError):
            await task
    for _ in range(100):
        if inflight_of(gw).get(uid) == 0:
            break
        await asyncio.sleep(0.01)
    assert inflight_of(gw).get(uid) == 0
    assert conn.execute("SELECT status, error_type FROM requests WHERE user_id=?", (uid,)).fetchone()[:] == \
        (502, "gateway_upstream_unreachable")


async def test_a_refused_request_gives_its_slot_back_before_its_row_is_written(setup, monkeypatch):
    gw, conn, keys = setup
    uid, h = keys["alice"]
    gw.cfg.limits.max_inflight = 0
    real = app_mod._save
    during = []
    monkeypatch.setattr(app_mod, "_save", lambda *a: (during.append(inflight_of(gw).get(uid)), real(*a)))
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=h)
    assert r.status_code == 429 and during == [0]


async def test_pool_threads_use_their_own_connections(db):
    pool = DbPool(db[1])
    seen = await asyncio.gather(*(pool.run(lambda c: (threading.get_ident(), id(c))) for _ in range(8)))
    pool.close()
    assert all(cid != id(db[1]) for _, cid in seen)
    assert len({tid for tid, _ in seen}) == len({cid for _, cid in seen})


async def test_an_in_memory_database_runs_on_its_one_connection(cfg):
    conn = get_conn(":memory:")
    pool = DbPool(conn)
    assert pool.path == ""
    assert await pool.run(lambda c: c is conn)


async def test_the_dashboard_view_has_its_own_connection_and_client(cfg, db, anthropic):
    gw = Gateway(cfg, db[1], http=mounted_client(anthropic))
    other = get_conn(db[0])
    v = gw.view(other, mounted_client(anthropic))
    assert v.conn is other and v.http is not gw.http and v.cfg is gw.cfg and v.poller is gw.poller
    assert gw.conn is db[1]   # the proxy's own is untouched
    gw.backend.last_error = "boom"
    assert v.backend.describe().detail == gw.backend.describe().detail
