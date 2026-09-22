from __future__ import annotations

import sqlite3

import httpx

from .config import Config
from .credentials import OAuthBackend
from .quota import Poller


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

    async def aclose(self) -> None:
        await self.http.aclose()
