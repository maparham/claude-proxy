from __future__ import annotations

import asyncio
import copy
import logging
import secrets
import sqlite3
import threading
from collections.abc import Mapping

import httpx

from . import tickets
from .config import Config
from .credentials import OAuthBackend, list_accounts
from .dbpool import DbPool
from .quota import Poller
from .upstream import StatusPage

logger = logging.getLogger("claude_proxy")


class Gateway:
    """Process-wide state shared by the proxy app, the dashboard app and background tasks."""

    def __init__(self, cfg: Config, conn: sqlite3.Connection, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.conn = conn
        self.db = DbPool(conn)   # the proxy's per-request queries, off the event loop
        # HTTP/1.1 connection pool: avoids the documented stall of many large uploads on one HTTP/2 connection.
        self.http = http or httpx.AsyncClient(http1=True, http2=False,
                                              timeout=httpx.Timeout(cfg.upstream.timeout_s, connect=15.0),
                                              limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
        self.device_id = _device_id(conn)
        # One credential backend and one usage poller per linked subscription account, by slug. Changed in place only
        # (sync_accounts), never rebound: the dashboard's view shares the pollers and reads the backends through them.
        self.backends: dict[str, OAuthBackend] = {}
        self.pollers: dict[str, Poller] = {}
        # Syncs run on the proxy's loop (run_pollers) and on the dashboard's thread (an account linked or removed there):
        # one at a time, or both could make a backend for one account and spend its single-use refresh token twice.
        self._sync_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None   # the proxy's loop, once run_pollers runs on it
        self._wake: asyncio.Event | None = None
        self.sync_accounts()
        self.status_page = StatusPage(self.http)
        if cfg.tickets.enabled:
            tickets.seed_prices(conn, cfg)
            tickets.seed_shares(conn, cfg)
        else:
            # A ticket-gated user lost their sign-up credit with their first ticket and may have no other limit, so with
            # tickets off limits.evaluate refuses every request of theirs ("Tickets are paused; ask the admin.") until
            # the admin ungates them. Say so at start, since nothing else tells the admin why they are refused.
            n = conn.execute("SELECT COUNT(DISTINCT user_id) FROM tickets WHERE ungated_at IS NULL AND user_id IS NOT NULL").fetchone()[0]
            if n:
                logger.warning("tickets are off in the config but %d users are ticket-gated; their requests are refused "
                               "until each is ungated on the Users page", n)

    def sync_accounts(self) -> tuple[list[str], list[str]]:
        """A backend and a poller for every linked account, none for an unlinked one: (added, removed) slugs. Kept ones
        stay as they are, their locks, refresh back-off and poll state with them. Run at start, every poll interval (a
        `claude-proxy login` from another process) and when the dashboard links or removes an account."""
        return self._sync([r["account"] for r in list_accounts(self.conn)])

    def _sync(self, linked: list[str]) -> tuple[list[str], list[str]]:
        # Changed in place, key by key (each step atomic under the GIL), so a reader on the other thread sees each
        # account whole or not at all; readers iterate over a copy (list(...)).
        with self._sync_lock:
            added = [a for a in linked if a not in self.backends]
            removed = [a for a in list(self.backends) if a not in linked]
            for a in added:
                be = OAuthBackend(self.cfg, self.conn, self.http, a)
                self.pollers[a] = Poller(self.conn, be, self.cfg.quota, self.db)
                self.backends[a] = be
            for a in removed:
                self.backends.pop(a, None)
                self.pollers.pop(a, None)
        if added or removed:
            logger.info("subscription accounts: added %s, removed %s", added or "none", removed or "none")
        return added, removed

    def healthy(self) -> bool:
        """At least one account linked, and every one able to serve: an account's users are never served by another."""
        backends = list(self.backends.values())
        return bool(backends) and all(b.describe().healthy for b in backends)

    async def run_pollers(self, interval_s: float = 60.0) -> None:
        """Each account's usage poll (quota.Poller.tick) every interval, after picking up accounts linked or removed
        meanwhile. One account's failure is logged and never holds up another's."""
        self._loop, self._wake = asyncio.get_running_loop(), asyncio.Event()
        while True:
            self._wake.clear()
            try:
                self.sync_accounts()
            except Exception:
                logger.exception("could not read the linked subscription accounts")
            pollers = list(self.pollers.values())
            for p, r in zip(pollers, await asyncio.gather(*(p.tick() for p in pollers), return_exceptions=True)):
                if isinstance(r, BaseException):
                    logger.error("usage poll [%s] failed", p.account, exc_info=r)
            try:
                await asyncio.wait_for(self._wake.wait(), interval_s)
            except TimeoutError:
                pass

    def wake_pollers(self) -> None:
        """Run the polls now rather than at the next interval (an account just linked: its plan is read at once), on the
        proxy's loop, where its backend's HTTP client and lock live. From any thread."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def on_proxy_loop(self, coro):
        """Await `coro` on the proxy's loop, from the dashboard's: the backends' HTTP client and locks belong to it.
        Run here when no poll loop runs (tests, one-loop setups)."""
        loop = self._loop
        if loop is None or loop is asyncio.get_running_loop():
            return await coro
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    def view(self, conn: sqlite3.Connection, http: httpx.AsyncClient) -> Gateway:
        """This gateway for an app served on another thread's event loop (the dashboard): its own connection and
        HTTP client, the same backends' state, pollers and status page."""
        v = copy.copy(self)
        v.conn, v.http = conn, http
        v.backends = _BackendViews(self, conn)
        v.sync_accounts = lambda: self._sync([r["account"] for r in list_accounts(conn)])
        v.wake_pollers, v.on_proxy_loop = self.wake_pollers, self.on_proxy_loop   # the proxy's loop, once it is known
        return v

    async def aclose(self) -> None:
        await self.http.aclose()
        self.db.close()


class _BackendView:
    """One credential backend as another thread reads it: its live state (last_error), loaded through that thread's
    connection, since two threads must not share one."""

    def __init__(self, backend, conn: sqlite3.Connection):
        self._backend, self._conn = backend, conn
        self.account = backend.account

    def describe(self):
        b = copy.copy(self._backend)
        b.conn = self._conn
        return b.describe()


class _BackendViews(Mapping):
    """The gateway's backends by slug, each as a _BackendView on `conn`. Read through to the gateway on each access, so
    accounts the proxy's loop picks up appear here too."""

    def __init__(self, gw: Gateway, conn: sqlite3.Connection):
        self._gw, self._conn = gw, conn

    def __getitem__(self, account: str) -> _BackendView:
        return _BackendView(self._gw.backends[account], self._conn)

    def __iter__(self):
        return iter(list(self._gw.backends))

    def __len__(self) -> int:
        return len(self._gw.backends)


def _device_id(conn: sqlite3.Connection) -> str:
    """The one machine id every user's Claude Code requests carry to Anthropic, made once and kept."""
    row = conn.execute("SELECT value FROM settings WHERE key='device_id'").fetchone()
    if row:
        return row[0]
    device_id = secrets.token_hex(32)
    conn.execute("INSERT INTO settings(key, value) VALUES('device_id', ?)", (device_id,))
    return device_id
