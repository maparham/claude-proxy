"""Each pass counts only the requests it served, as if another user held it (2026-10-28): the request row names its
pass, the 5-hour attribution splits by pass, old rows are backfilled, and the user can pick which pass serves first."""
import time

from claude_proxy import db as dbm, limits, quota, tickets
from claude_proxy.app import create_app
from tests.conftest import asgi_client, make_gateway, seed_oauth
from tests.test_tickets_limits import fresh, req, snap
from tests.test_tickets_web import env, grant  # noqa: F401  (the fixture)
from tests.test_web import admin_client, bearer
from tests.tickets_helpers import NOW, seeded, user

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


def side_by_side(conn, cfg, ids, at):
    """Alice on a Lite week pass from `at`, and a Standard week pass started beside it a minute later."""
    a = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "USD", now=at, terms_version="v1")
    b = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "standard", "week", "USD", now=at + 60,
                      terms_version="v1", start_now=True)
    return a, b


def test_the_5h_attribution_splits_by_pass(db):
    conn, cfg, ids = seeded(db)
    a, b = side_by_side(conn, cfg, ids, NOW - 900)
    fresh(conn, NOW - 600)
    req(conn, ids["alice"], NOW - 500, i=3000, ticket=a["id"])
    req(conn, ids["alice"], NOW - 450, i=1000, ticket=b["id"])
    snap(conn, NOW - 400, 4, "5h", NOW + 3500)
    att = quota.attribution(conn, cfg.pricing, "5h", now=NOW - 300)
    assert att["shares"][ids["alice"]] == 4
    assert att["ticket_shares"] == {a["id"]: 3, b["id"]: 1}
    st = {s.kind: s for s in limits.pass_states(conn, cfg, tickets.get(conn, b["id"]), NOW - 300)}
    assert st["share_5h"].current == 1   # the Lite pass's use is not the Standard pass's


def test_old_rows_are_backfilled_to_the_pass_that_covered_them(db):
    conn, cfg, ids = seeded(db)
    at = time.time() - 3600
    a, b = side_by_side(conn, cfg, ids, at)
    conn.execute("DROP INDEX idx_requests_ticket")
    conn.execute("ALTER TABLE requests DROP COLUMN ticket_id")
    for t, rej in ((at - 60, None), (at + 120, None), (at + 180, "share_5h")):
        conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, rejected_by) "
                     "VALUES(?,?,?,'POST','/v1/messages','anthropic','claude-sonnet-5',?)", (ids["alice"], t, t + 1, rej))
    dbm._migrate(conn)
    assert [r[0] for r in conn.execute("SELECT ticket_id FROM requests ORDER BY started_at")] == [None, a["id"], None]


async def test_a_forwarded_request_records_its_pass_and_the_pin_moves_it(env, anthropic):
    gw, conn, cfg, ids, keys = env
    seed_oauth(conn)
    gw = make_gateway(cfg, conn, anthropic)
    now = time.time()
    snap(conn, now - 60, 0, "5h", now + 3600)        # snapshots on the wall clock the gateway runs on
    snap(conn, now - 60, 0, "7d", now + 3 * 86400)
    async with admin_client(gw) as c:
        assert (await grant(c, {"user": ids["alice"], "tier": "lite", "length": "week", "currency": "EUR"})).status_code == 200
        assert (await grant(c, {"user": ids["alice"], "tier": "standard", "length": "week", "currency": "EUR",
                                "start_now": True})).status_code == 200
    a, b = tickets.running(conn, ids["alice"], time.time())
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=bearer(keys["alice"]))
        assert r.status_code == 200, r.text
    assert conn.execute("SELECT ticket_id FROM requests ORDER BY id DESC LIMIT 1").fetchone()[0] == a["id"]   # oldest first

    from tests.test_orders_web import client
    async with client(gw) as c:
        me = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["current"]
        assert [(p["tier"], p["serving"], p["pinned"]) for p in me["passes"]] == [("lite", True, False), ("standard", False, False)]
        assert "id" not in me["passes"][0] and me["passes"][1]["pct_5h"] == 0
        r = await c.post("/api/me/tickets/pin", headers=bearer(keys["alice"]), json={"pass": 1, "starts_at": me["passes"][1]["starts_at"]})
        assert r.status_code == 200
        assert (await c.post("/api/me/tickets/pin", headers=bearer(keys["alice"]), json={"pass": 0, "starts_at": 1})).status_code == 409
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json=MSG, headers=bearer(keys["alice"]))).status_code == 200
    assert conn.execute("SELECT ticket_id FROM requests ORDER BY id DESC LIMIT 1").fetchone()[0] == b["id"]
    async with client(gw) as c:
        me = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()["current"]
        assert [(p["serving"], p["pinned"]) for p in me["passes"]] == [(False, False), (True, True)]
        assert (await c.post("/api/me/tickets/pin", headers=bearer(keys["alice"]), json={"pass": None})).status_code == 200
    assert conn.execute("SELECT pass_pin FROM users WHERE id=?", (ids["alice"],)).fetchone()[0] is None
