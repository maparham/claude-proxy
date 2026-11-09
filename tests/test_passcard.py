"""Telegram bot design, section 6 (My passes): the pass card is built in one place."""
import time

from claude_proxy import passcard, tickets
from tests.test_tickets_web import env  # noqa: F401  (the fixture)
from tests.test_web import admin_client, bearer
from tests.conftest import asgi_client
from claude_proxy.web import create_dashboard_app
from tests.tickets_helpers import live, user


async def test_route_and_build_agree(env):
    gw, conn, cfg, ids, keys = env
    now = time.time()
    live(conn, now)
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "EUR", now=now, terms_version="v1")
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()
    b = passcard.build(conn, cfg, ids["alice"], r["now"])
    assert r["current"]["tier"] == "lite"
    assert {k: v for k, v in b.items() if k != "now"} == {k: v for k, v in r.items() if k != "now"}


async def test_pass_tokens_on_card_and_admin_list(env):
    """Each pass shows the tokens it served, weighted and raw, to its holder and to the admin: only its own served
    requests, and nothing (None) for a pass older than the oldest request kept, whose history retention cut."""
    gw, conn, cfg, ids, keys = env
    now = time.time()
    live(conn, now)
    alice = user(conn, ids["alice"])
    old = tickets.grant(conn, cfg, ids["admin"], alice, "lite", "day", "EUR", now=now - 3 * 86400, terms_version="v1")
    tid = tickets.grant(conn, cfg, ids["admin"], alice, "lite", "week", "EUR", now=now, terms_version="v1")["id"]
    other = tickets.grant(conn, cfg, ids["admin"], alice, "lite", "day", "EUR", now=now, start_now=True, terms_version="v1")["id"]
    row = ("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status, input_tokens, output_tokens, "
           "ticket_id, rejected_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")
    m = "claude-sonnet-4-5"
    conn.execute(row, (ids["alice"], now - 2 * 86400, now - 2 * 86400, "POST", "/v1/messages", "anthropic", m, 200, 1, 1, None, None))   # the oldest kept
    conn.execute(row, (ids["alice"], now, now, "POST", "/v1/messages", "anthropic", m, 200, 1000, 100, tid, None))
    conn.execute(row, (ids["alice"], now, now, "POST", "/v1/messages", "anthropic", m, 429, 5000, 0, tid, "ticket"))   # refused: not served
    conn.execute(row, (ids["alice"], now, now, "POST", "/v1/messages/count_tokens", "anthropic", m, 200, 7000, 0, tid, None))
    conn.execute(row, (ids["alice"], now, now, "POST", "/v1/messages", "anthropic", m, 200, 300, 30, other, None))   # the other pass's
    async with asgi_client(create_dashboard_app(gw)) as c:
        me = (await c.get("/api/me/tickets", headers=bearer(keys["alice"]))).json()
    async with admin_client(gw) as c:
        adm = {x["id"]: x["tokens"] for x in (await c.get("/api/admin/tickets")).json()["tickets"]}
    p = cfg.pricing.price_for(m)
    assert adm[tid] == {"raw": 1100, "weighted": (1000 * p.input + 100 * p.output) / cfg.pricing.reference_input()}
    assert adm[other]["raw"] == 330
    assert adm[old["id"]] is None   # it began before the oldest request kept: its history may be cut
    assert sorted(x["tokens"]["raw"] for x in me["current"]["passes"] if not x.get("queued")) == [330, 1100]
    assert [x["tokens"] for x in me["past"]] == [None]
