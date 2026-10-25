"""SQLite off the event loop. Every query is synchronous; on the loop, one slow query (a limit check, a write waiting
out another writer's lock) would stall every request in flight, streams included. The pool runs them on worker
threads, each with its own connection: in WAL mode readers never wait for the writer, so one user's slow check
delays no one else."""
from __future__ import annotations

import asyncio
import functools
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from .db import get_conn

WORKERS = 4


def db_path(conn: sqlite3.Connection) -> str:
    """The file behind `conn`; "" for an in-memory database."""
    return next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "") or ""


class DbPool:
    def __init__(self, conn: sqlite3.Connection, workers: int = WORKERS):
        self.conn = conn
        self.path = db_path(conn)
        self._local = threading.local()
        self._executor: ThreadPoolExecutor | None = None
        self._workers = workers
        self._lock = threading.Lock()

    def _thread_conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = get_conn(self.path)
        return c

    def _call(self, fn, args, kwargs):
        return fn(self._thread_conn(), *args, **kwargs)

    def run(self, fn, *args, **kwargs) -> asyncio.Future:
        """fn(conn, *args, **kwargs) on a worker thread, as a future of the running loop. Await it through
        asyncio.shield where cancelling the caller must not drop the work (the request log)."""
        loop = asyncio.get_running_loop()
        if not self.path:   # an in-memory database exists on its one connection only: run there
            fut = loop.create_future()
            try:
                fut.set_result(fn(self.conn, *args, **kwargs))
            except Exception as e:
                fut.set_exception(e)
            return fut
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(self._workers, thread_name_prefix="db")
        return loop.run_in_executor(self._executor, functools.partial(self._call, fn, args, kwargs))

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
