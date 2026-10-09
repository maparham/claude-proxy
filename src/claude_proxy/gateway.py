from __future__ import annotations

import logging
import sqlite3

import httpx

from . import tickets
from .config import Config
from .credentials import OAuthBackend
from .quota import Poller
from .upstream import StatusPage

logger = logging.getLogger("claude_proxy")


class Gateway:
    """Process-wide state shared by the proxy app, the dashboard app and background tasks."""

    def __init__(self, cfg: Config, conn: sqlite3.Connection, http: httpx.AsyncClient | None = None, backend=None):
        self.cfg = cfg
        self.conn = conn
        # HTTP/1.1 connection pool: avoids the documented stall of many large uploads on one HTTP/2 connection.
        self.http = http or httpx.AsyncClient(http1=True, http2=False,
                                              timeout=httpx.Timeout(cfg.upstream.timeout_s, connect=15.0),
                                              limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
        self.backend = backend or OAuthBackend(cfg, conn, self.http)
        self.poller = Poller(conn, self.backend, cfg.quota)
        self.status_page = StatusPage(self.http)
        if cfg.tickets.enabled:
            tickets.seed_prices(conn, cfg)
        else:
            # A ticket-gated user lost their sign-up credit with their first ticket and may have no other limit, so with
            # tickets off limits.evaluate refuses every request of theirs ("Tickets are paused; ask the admin.") until
            # the admin ungates them. Say so at start, since nothing else tells the admin why they are refused.
            n = conn.execute("SELECT COUNT(DISTINCT user_id) FROM tickets WHERE ungated_at IS NULL AND user_id IS NOT NULL").fetchone()[0]
            if n:
                logger.warning("tickets are off in the config but %d users are ticket-gated; their requests are refused "
                               "until each is ungated on the Users page", n)

    async def aclose(self) -> None:
        await self.http.aclose()
