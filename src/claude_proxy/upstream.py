"""Is Anthropic itself having trouble? Two signals for the admin's Overview banner: our own recent 529s (overloaded),
and Anthropic's public status page. Both say "upstream, not the gateway"."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

import httpx

logger = logging.getLogger("claude_proxy")

WINDOW_S = 600
MIN_FAILED = 3        # fewer is a blip, which Claude Code's own retries absorb
MIN_SHARE = 0.2
# A 529 before the stream starts, or an overloaded_error event inside a 200 stream.
OVERLOADED = "(status = 529 OR error_type = 'overloaded_error')"
STATUS_URL = "https://status.anthropic.com/api/v2/summary.json"


def overload(conn: sqlite3.Connection, now: float | None = None) -> dict:
    """Anthropic requests of the last WINDOW_S seconds, and how many came back overloaded."""
    now = time.time() if now is None else now
    r = conn.execute(f"SELECT COUNT(*) AS total, COALESCE(SUM({OVERLOADED}), 0) AS failed, "
                     f"MIN(CASE WHEN {OVERLOADED} THEN started_at END) AS since, MAX(CASE WHEN {OVERLOADED} THEN started_at END) AS last_at "
                     "FROM requests WHERE provider='anthropic' AND rejected_by IS NULL AND started_at>=?", (now - WINDOW_S,)).fetchone()
    total, failed = r["total"], r["failed"]
    return {"overloaded": failed >= MIN_FAILED and failed >= MIN_SHARE * total,
            "failed": failed, "total": total, "since": r["since"], "last_at": r["last_at"]}


def parse_summary(body: dict) -> dict:
    """The parts of a Statuspage summary.json the banner shows: the overall indicator and the open incidents."""
    status = body.get("status") or {}
    incidents = [{"name": i.get("name") or "", "impact": i.get("impact") or "none", "url": i.get("shortlink") or ""}
                 for i in body.get("incidents") or [] if i.get("status") not in ("resolved", "postmortem")]
    return {"indicator": status.get("indicator") or "none", "description": status.get("description") or "", "incidents": incidents}


class StatusPage:
    """Polls Anthropic's status page. Kept in memory only: after a restart the banner waits for the first poll."""

    def __init__(self, http: httpx.AsyncClient, url: str = STATUS_URL):
        self.http = http
        self.url = url
        self.last: dict | None = None   # parse_summary(...) | {"observed_at": ...}

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        try:
            r = await self.http.get(self.url, timeout=15.0)
            r.raise_for_status()
            self.last = parse_summary(r.json()) | {"observed_at": now}
        except Exception as e:   # unreachable or malformed: an old answer is worse than none
            logger.info("status page poll failed: %s", e)
            self.last = None

    def view(self, now: float | None = None, max_age_s: float = 900) -> dict | None:
        now = time.time() if now is None else now
        if self.last is None or now - self.last["observed_at"] > max_age_s:
            return None
        return self.last

    async def run(self, interval_s: float = 300.0) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(interval_s)
