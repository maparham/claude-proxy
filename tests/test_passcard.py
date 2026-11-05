"""Telegram bot design, section 6 (My passes): the pass card is built in one place."""
import time

from claude_proxy import passcard, tickets
from tests.test_tickets_web import env  # noqa: F401  (the fixture)
from tests.test_web import bearer
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
