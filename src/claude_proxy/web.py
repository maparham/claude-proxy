"""Dashboard listener: static page plus a JSON API (spec section 10).

Access (spec 10.1):
- admin: username + password (argon2id) -> session cookie (HttpOnly, SameSite=Strict, Secure behind TLS);
- user: their virtual key, either exchanged for a session cookie or sent as a Bearer token (statusline), or a Clerk
  sign-in (Google, GitHub, email code) exchanged for a session cookie, which can also create the account (sign-up design);
- cookie sessions must send the session's CSRF token in `x-csrf-token` on every state change;
- no loopback or IP-based exemptions; failed logins are rate limited per client address.
Non-admins only ever see their own usage, their own limits and their own estimated share.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import secrets
import shlex
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

import segno
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

import httpx

from . import clerk, db, limits, quota, usage
from .auth import AuthError, authenticate
from .gateway import Gateway

logger = logging.getLogger("claude_proxy")

STATIC = Path(__file__).parent / "static"
COOKIE = "cp_session"
RANGES = {"1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400}
GRANULARITY = {"hour": 3600, "day": 86400, "week": 7 * 86400}
BUCKETS = ("5h", "7d")
IS_ERROR = "(rejected_by IS NOT NULL OR status >= 400 OR error_type IS NOT NULL)"
ERROR_KIND = ("CASE WHEN rejected_by = 'auth' THEN 'gateway_auth' WHEN rejected_by = 'request' THEN 'gateway_bad_request' "
              "WHEN rejected_by = 'key_scope' THEN 'gateway_key_scope' "
              "WHEN rejected_by IS NOT NULL THEN 'gateway_limit' ELSE COALESCE(error_type, 'http_' || status) END")
_ph = PasswordHasher()
# Verified against when the user does not exist, so timing does not reveal valid usernames.
_DUMMY_HASH = _ph.hash("not-a-real-password")


# no-transform: Cloudflare leaves the page alone, so it doesn't inject its analytics script, which the CSP would block.
PAGE_HEADERS = {"Cache-Control": "no-cache, no-transform"}
USER_CODE_LETTERS = "BCDFGHJKLMNPQRSTVWXZ"   # no vowels (no words), no look-alikes of digits
DEVICE_TTL_S, DEVICE_INTERVAL_S = 600, 3


def fail(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)


class LoginLimiter:
    def __init__(self, max_failures: int = 5, window_s: int = 300, what: str = "failed logins"):
        self.max_failures = max_failures
        self.window_s = window_s
        self.what = what
        self.failures: dict[str, deque] = defaultdict(deque)

    def check(self, ip: str) -> None:
        now = time.time()
        q = self.failures.get(ip)
        while q and now - q[0] > self.window_s:
            q.popleft()
        if q is not None and not q:
            del self.failures[ip]   # keep the table to addresses with recent failures
        elif q and len(q) >= self.max_failures:
            fail(429, f"Too many {self.what}. Try again in {int(self.window_s - (now - q[0])) + 1}s.")

    def failed(self, ip: str) -> None:
        now = time.time()
        if len(self.failures) > 10_000:
            for k in [k for k, q in self.failures.items() if not q or now - q[-1] > self.window_s]:
                del self.failures[k]
        self.failures[ip].append(now)


class SecurityHeaders(BaseHTTPMiddleware):
    def __init__(self, app, clerk_host: str | None = None):
        super().__init__(app)
        # Clerk's sign-in runs from its Frontend API host, with Cloudflare's bot check in a frame.
        clerk, bot = (f" https://{clerk_host}", " https://challenges.cloudflare.com") if clerk_host else ("", "")
        self.csp = (f"default-src 'self'; script-src 'self' https://cdn.jsdelivr.net/npm/echarts@5.6.0/{clerk}{bot}; "
                    f"style-src 'self' 'unsafe-inline'; img-src 'self' data:{clerk}{' https://img.clerk.com' if clerk else ''}; "
                    f"connect-src 'self'{clerk}; frame-src{bot or ' ' + repr('none')}; worker-src 'self' blob:; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = self.csp
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp


def create_dashboard_app(gw: Gateway) -> FastAPI:
    app = FastAPI(title="claude-proxy dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    conn, cfg = gw.conn, gw.cfg
    verifier = None
    if cfg.signup.clerk_publishable_key:
        if not cfg.listener.dashboard_url:
            logger.error("Clerk sign-in is off: it needs [listener] dashboard_url, the origin its tokens are made for.")
        else:
            verifier = clerk.Verifier(cfg.signup.clerk_publishable_key, cfg.signup.clerk_secret(), cfg.listener.dashboard_url, gw.http)
    app.add_middleware(SecurityHeaders, clerk_host=verifier.fapi if verifier else None)
    app.state.gw = gw
    app.state.clerk = verifier
    limiter = LoginLimiter()
    starts = LoginLimiter(max_failures=10, what="authorization requests from this address")

    @app.exception_handler(HTTPException)
    async def _http_error(request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    # ---------- auth ----------

    def principal(request: Request, write: bool = False, routes_ok: bool = False):
        if request.headers.get("authorization") or request.headers.get("x-api-key"):
            try:
                user = authenticate(conn, request.headers)
            except AuthError as e:
                fail(e.status, e.body["error"]["message"])
            # A routes-only key (OpenCode) may read its own status and nothing else, and never acts as an admin.
            if user["key_scope"] != "full" and not (routes_ok and user["key_scope"] == "routes"):
                fail(403, "This key only works for third-party models; use your Claude Code key for the dashboard.")
            return user
        user = db.find_session(conn, request.cookies.get(COOKIE, ""))
        if user is None:
            fail(401, "Not signed in.")
        if write and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), user["csrf_token"]):
            fail(403, "Missing or wrong CSRF token.")
        return user

    def admin(request: Request, write: bool = False):
        user = principal(request, write)
        if user["role"] != "admin":
            fail(403, "Admin only.")
        return user

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def start_session(user, key_id: int | None = None) -> JSONResponse:
        raw, csrf = db.create_session(conn, user["id"], key_id=key_id)
        resp = JSONResponse({"ok": True, "csrf": csrf, "user": _public_user(user)})
        resp.set_cookie(COOKIE, raw, max_age=7 * 86400, httponly=True, samesite="strict",
                        secure=cfg.listener.secure_cookies, path="/")
        return resp

    @app.post("/api/login")
    async def login(request: Request):
        ip = client_ip(request)
        limiter.check(ip)
        body = await _json(request)
        user = conn.execute("SELECT * FROM users WHERE name=? AND role='admin' AND enabled=1 AND revoked_at IS NULL",
                            (str(body.get("username", "")),)).fetchone()
        try:
            # argon2 takes tens of milliseconds; off the event loop, which also carries every proxied stream.
            await asyncio.to_thread(_ph.verify, user["password_hash"] if user and user["password_hash"] else _DUMMY_HASH,
                                    str(body.get("password", "")))
            ok = user is not None and bool(user["password_hash"])
        except (VerificationError, InvalidHashError):
            ok = False
        if not ok:
            limiter.failed(ip)
            fail(401, "Wrong username or password.")
        db.audit(conn, user["id"], "login", user["name"])
        return start_session(user)

    @app.post("/api/login/key")
    async def login_key(request: Request):
        ip = client_ip(request)
        limiter.check(ip)
        body = await _json(request)
        try:
            user = authenticate(conn, {"authorization": f"Bearer {body.get('key', '')}"})
        except AuthError:
            limiter.failed(ip)
            fail(401, "Unknown, disabled or revoked key.")
        if user["key_scope"] != "full":   # a valid key, so not counted as a failed attempt
            fail(403, "This key only works for third-party models; sign in with your Claude Code key.")
        return start_session(user, key_id=user.get("machine_key_id"))

    @app.get("/api/auth-config")
    async def auth_config():
        # Before sign-in: whether to offer Clerk's sign-in, and with which (public) key; the install commands, for the how-to-join steps.
        return {"clerk": {"publishable_key": cfg.signup.clerk_publishable_key, "frontend_api": verifier.fapi} if verifier else None,
                "signup": bool(verifier and cfg.signup.enabled),
                "install": install_command(), "install_windows": install_command(windows=True)}

    @app.post("/api/login/clerk")
    async def login_clerk(request: Request):
        ip = client_ip(request)
        limiter.check(ip)
        if verifier is None:
            fail(404, "Sign-in with Clerk is not set up on this gateway.")
        body = await _json(request)
        try:
            claims = await verifier.verify(str(body.get("token", "")))
            user = conn.execute("SELECT * FROM users WHERE clerk_id=?", (claims["sub"],)).fetchone()
            email = None if user else await verifier.email(claims["sub"])
            if user is None:   # another sign-in of the same person may have linked it meanwhile
                user = conn.execute("SELECT * FROM users WHERE clerk_id=?", (claims["sub"],)).fetchone()
        except clerk.ClerkError as e:
            limiter.failed(ip)
            fail(401, f"Sign-in failed: {e}.")
        except httpx.HTTPError:
            fail(503, "Could not reach Clerk to check the sign-in; try again.")
        if user is None:
            user = clerk_account(claims["sub"], email)
        if user["revoked_at"] is not None or not user["enabled"]:
            fail(403, "This account is disabled. Ask the gateway admin.")
        db.audit(conn, user["id"], "login", user["name"], {"via": "clerk"})
        return start_session(user)

    def clerk_account(clerk_id: str, email: str):
        """The gateway user for a first Clerk sign-in: an account named by (or holding) that email, else a new one."""
        # Admins keep signing in with their password; only a user account can be taken over by an email.
        user = conn.execute("SELECT * FROM users WHERE role='user' AND (lower(email)=? OR lower(name)=?) ORDER BY id LIMIT 1",
                            (email, email)).fetchone()
        if user is not None:
            if user["clerk_id"]:
                fail(409, f"{email} is linked to another sign-in. Ask the gateway admin.")
            if user["revoked_at"] is not None:
                fail(403, "This account was removed. Ask the gateway admin.")
            conn.execute("UPDATE users SET clerk_id=?, email=? WHERE id=?", (clerk_id, email, user["id"]))
            db.audit(conn, user["id"], "clerk_link", user["name"], {"email": email})
            return conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        if not cfg.signup.enabled:
            fail(403, "Sign-ups are closed. Ask the gateway admin for an account.")
        # A deleted account's email stays in the audit log: signing up again never brings a new credit.
        if conn.execute("SELECT 1 FROM audit_log WHERE action='signup' AND lower(target)=?", (email,)).fetchone():
            fail(403, "This account was removed. Ask the gateway admin.")
        if conn.execute("SELECT 1 FROM users WHERE lower(name)=?", (email,)).fetchone():
            fail(409, f"The name {email} is taken. Ask the gateway admin.")
        conn.execute("BEGIN IMMEDIATE")
        try:
            uid, _ = db.create_user(conn, email)   # its own key is never shown; machines get theirs in the browser
            conn.execute("UPDATE users SET clerk_id=?, email=? WHERE id=?", (clerk_id, email, uid))
            conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,?)",
                         (uid, "cost_total", "*", f"{cfg.signup.credit_usd:g}", "usd", int(time.time())))
            db.audit(conn, uid, "signup", email, {"credit_usd": cfg.signup.credit_usd})
            conn.execute("COMMIT")
        except BaseException as e:
            conn.execute("ROLLBACK")
            if isinstance(e, sqlite3.IntegrityError):   # the same person signing in twice at once
                fail(409, "That account was just created; sign in again.")
            raise
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    @app.post("/api/logout")
    async def logout(request: Request):
        db.delete_session(conn, request.cookies.get(COOKIE, ""))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/session")
    async def session(request: Request):
        user = principal(request)
        out = {"user": _public_user(user), "csrf": user["csrf_token"] if "csrf_token" in user.keys() else None,
               # Configurable values the dashboard's explanations quote.
               "settings": {"reference_model": cfg.pricing.reference_model},
               "install": install_command(), "install_windows": install_command(windows=True)}
        if is_admin(user):
            be = gw.backend.describe()
            out["credential"] = {"healthy": be.healthy, "detail": be.detail}
            out["settings"]["stale_after_s"] = cfg.quota.stale_after_s
        return out

    # ---------- helpers ----------

    names = lambda: {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM users")}  # noqa: E731

    # Non-admins never learn about the subscription behind the gateway: no account quota, credential state
    # or Anthropic rate-limit detail. Their own limits are all they have.
    is_admin = lambda user: user["role"] == "admin"  # noqa: E731

    def limit_views(user, states) -> list[dict]:
        return [s.to_dict() if is_admin(user) else limits.user_view(s) for s in states]

    def scope_where(user, user_id: int | None = None) -> tuple[str, tuple]:
        """Non-admins always see only themselves; an admin sees everyone, or one user when `user_id` is given."""
        if user["role"] != "admin":
            return "user_id=?", (user["id"],)
        if user_id is None:
            return "1=1", ()
        if conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
            fail(404, "No such user.")
        return "user_id=?", (user_id,)

    def quota_view(now: float) -> list[dict]:
        out = []
        n = names()
        for b in sorted(set(BUCKETS) | set(quota.buckets(conn, now - 8 * 86400))):
            att = quota.attribution(conn, cfg.pricing, b, now=now, stale_after_s=cfg.quota.stale_after_s)
            if att["utilization_pct"] is None and b not in BUCKETS:
                continue
            shares = {n.get(uid, f"#{uid}"): v for uid, v in att["shares"].items()}
            out.append({k: att[k] for k in ("bucket", "utilization_pct", "resets_at", "observed_at", "stale", "unattributed")}
                       | {"shares": shares})
        return out

    # ---------- read API ----------

    @app.get("/api/overview")
    async def overview(request: Request, user_id: int | None = None):
        user = principal(request)
        now = time.time()
        where, params = scope_where(user, user_id)
        totals = {k: usage.total(conn, cfg.pricing, f"{where} AND started_at>=?", (*params, now - s)).to_dict()
                  for k, s in (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400))}
        recent = usage.total(conn, cfg.pricing, f"{where} AND started_at>=? AND provider='anthropic'", (*params, now - 900))
        active = conn.execute(f"SELECT COUNT(DISTINCT user_id) FROM requests WHERE {where} AND started_at>=? AND rejected_by IS NULL",
                              (*params, now - 86400)).fetchone()[0]
        out = {"scope": "self" if not is_admin(user) else "account" if user_id is None else "user",
               "totals": totals, "burn_rate_weighted_per_min": recent.weighted / 15.0}
        if not is_admin(user):
            return out
        be = gw.backend.describe()
        return out | {
            "active_users_24h": active,
            "quota": quota_view(now),
            "exhaustion": _exhaustion(conn, now),
            "credential": {"healthy": be.healthy, "detail": be.detail, "expires_at": be.expires_at},
            "poll": {"last_status": gw.poller.last_status},
        }

    @app.get("/api/series")
    async def series(request: Request, range: str = "7d", granularity: str = "day", split: str = "user",
                     tz_offset: int = 0, provider: str | None = None, user_id: int | None = None):
        user = principal(request)
        if range not in RANGES or granularity not in GRANULARITY or split not in ("user", "model", "provider", "none"):
            fail(400, "bad range, granularity or split")
        where, params = scope_where(user, user_id)
        if provider:
            where, params = f"{where} AND provider=?", (*params, provider)
        pts = usage.series(conn, cfg.pricing, time.time() - RANGES[range], GRANULARITY[granularity],
                           None if split == "none" else split, where, params, tz_offset_s=tz_offset)
        if split == "user":
            n = names()
            for p in pts:
                p["key"] = n.get(p["key"], f"#{p['key']}")
        return {"range": range, "granularity": granularity, "split": split, "points": pts}

    @app.get("/api/models")
    async def models(request: Request, range: str = "30d", tz_offset: int = 0, user_id: int | None = None):
        user = principal(request)
        where, params = scope_where(user, user_id)
        since = time.time() - RANGES.get(range, RANGES["30d"])
        by_model = usage.grouped(conn, cfg.pricing, f"{where} AND started_at>=?", (*params, since), group="model")
        by_provider = usage.grouped(conn, cfg.pricing, f"{where} AND started_at>=?", (*params, since), group="provider")
        cache = usage.series(conn, cfg.pricing, since, 86400, None, where, params, tz_offset_s=tz_offset)
        return {
            "models": sorted(({"model": m, **t.to_dict()} for m, t in by_model.items()), key=lambda r: -r["cost_usd"]),
            "providers": {p: t.to_dict() for p, t in by_provider.items()},
            "cache_ratio": [{"t": p["t"], "ratio": p["cache_hit_ratio"]} for p in cache],
            "formula": "cache_read / (input + cache_creation + cache_read)",
        }

    @app.get("/api/heatmap")
    async def heatmap(request: Request, range: str = "30d", tz_offset: int = 0, user_id: int | None = None):
        user = principal(request)
        where, params = scope_where(user, user_id)
        off = int(tz_offset)
        rows = conn.execute(
            f"SELECT CAST(strftime('%w', started_at + {off}, 'unixepoch') AS INTEGER) AS dow, "
            f"CAST(strftime('%H', started_at + {off}, 'unixepoch') AS INTEGER) AS hour, COUNT(*) AS n "
            f"FROM requests WHERE rejected_by IS NULL AND {where} AND started_at>=? GROUP BY dow, hour",
            (*params, time.time() - RANGES.get(range, RANGES["30d"]))).fetchall()
        return {"cells": [[r["hour"], r["dow"], r["n"]] for r in rows]}

    @app.get("/api/sessions")
    async def sessions(request: Request, range: str = "7d", user_id: int | None = None):
        user = principal(request)
        where, params = scope_where(user, user_id)
        since = time.time() - RANGES.get(range, RANGES["7d"])
        meta = conn.execute(
            f"SELECT session_id, user_id, MIN(started_at) AS first, MAX(COALESCE(ended_at, started_at)) AS last, "
            f"COUNT(*) AS n, GROUP_CONCAT(DISTINCT model) AS models FROM requests WHERE session_id IS NOT NULL "
            f"AND rejected_by IS NULL AND {where} AND started_at>=? GROUP BY user_id, session_id ORDER BY last DESC LIMIT 200",
            (*params, since)).fetchall()
        # Session ids come from clients, so two users' sessions may share one; each stays its own row.
        tot = usage.grouped(conn, cfg.pricing, f"session_id IS NOT NULL AND {where} AND started_at>=?", (*params, since),
                            group="user_id || '|' || session_id")
        n = names()
        ids = [r["session_id"] for r in meta]
        titled = {(t["user_id"], t["session_id"]): t["title"] for t in conn.execute(
            f"SELECT user_id, session_id, title FROM session_titles WHERE {where} "
            f"AND session_id IN ({','.join('?' * len(ids))})", (*params, *ids))}
        return {"sessions": [{"session_id": r["session_id"], "title": titled.get((r["user_id"], r["session_id"])),
                              "user": n.get(r["user_id"]), "first": r["first"], "last": r["last"],
                              "duration_s": r["last"] - r["first"], "requests": r["n"], "models": (r["models"] or "").split(","),
                              **{k: v for k, v in tot.get(f"{r['user_id']}|{r['session_id']}", usage.Totals()).to_dict().items() if k in ("raw", "weighted", "cost_usd")}}
                             for r in meta]}

    @app.get("/api/errors")
    async def errors(request: Request, range: str = "7d", tz_offset: int = 0, user_id: int | None = None):
        user = principal(request)
        where, params = scope_where(user, user_id)
        since = time.time() - RANGES.get(range, RANGES["7d"])
        b = 3600 if range in ("1d", "7d") else 86400
        off = int(tz_offset)
        kind = ERROR_KIND
        cond = f"{IS_ERROR} AND {where} AND started_at>=?"
        rows = conn.execute(f"SELECT (CAST((started_at + {off}) / {b} AS INTEGER) * {b} - {off}) AS t, {kind} AS k, COUNT(*) AS n "
                            f"FROM requests WHERE {cond} GROUP BY t, k ORDER BY t", (*params, since)).fetchall()
        recent = conn.execute(f"SELECT started_at, user_id, path, model, status, {kind} AS k, rejected_by FROM requests "
                              f"WHERE {cond} ORDER BY started_at DESC LIMIT 50", (*params, since)).fetchall()
        n = names()
        points = [dict(r) for r in rows]
        recent = [{**dict(r), "user": n.get(r["user_id"])} for r in recent]
        if not is_admin(user):
            merged: dict = {}
            for p in points:
                key = (p["t"], _user_error_kind(p["k"]))
                merged[key] = merged.get(key, 0) + p["n"]
            points = [{"t": t, "k": k, "n": v} for (t, k), v in merged.items()]
            recent = [r | {"k": _user_error_kind(r["k"]), "rejected_by": _user_rejected_by(r["rejected_by"])} for r in recent]
        return {"bucket_s": b, "points": points, "recent": recent}

    @app.get("/api/quota/timeline")
    async def quota_timeline(request: Request, bucket: str = "5h", range: str = "7d"):
        admin(request)
        now = time.time()
        snaps = conn.execute("SELECT observed_at, utilization_pct, resets_at, source FROM quota_snapshots WHERE bucket=? "
                             "AND observed_at>=? ORDER BY observed_at", (bucket, now - RANGES.get(range, RANGES["7d"]))).fetchall()
        att = quota.attribution(conn, cfg.pricing, bucket, now=now, stale_after_s=cfg.quota.stale_after_s)
        n = names()
        history = []
        for h in att["history"]:
            shares = {n.get(uid, f"#{uid}"): v for uid, v in h["shares"].items()}
            history.append({"t": h["t"], "utilization_pct": h["utilization_pct"], "shares": shares})
        return {"bucket": bucket, "snapshots": [dict(r) for r in snaps], "window_history": history,
                "buckets": sorted(set(BUCKETS) | set(quota.buckets(conn, now - 8 * 86400)))}

    @app.get("/api/requests")
    async def recent_requests(request: Request, user_id: int | None = None, limit: int = 100):
        user = principal(request)
        where, params = scope_where(user, user_id)
        rows = conn.execute(
            f"SELECT id, user_id, started_at, ended_at, provider, model, status, stream, session_id, rejected_by, "
            f"CASE WHEN {IS_ERROR} THEN {ERROR_KIND} END AS kind FROM requests WHERE {where} "
            f"ORDER BY started_at DESC, id DESC LIMIT ?", (*params, min(max(limit, 1), 500))).fetchall()
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        tot = usage.grouped(conn, cfg.pricing, f"id IN ({marks})", tuple(ids), group="id") if ids else {}
        sessions = {(r["user_id"], r["session_id"]) for r in rows if r["session_id"]}
        titled = {(t["user_id"], t["session_id"]): t["title"] for t in conn.execute(
            f"SELECT user_id, session_id, title FROM session_titles WHERE {where} "
            f"AND session_id IN ({','.join('?' * len(sessions))})", (*params, *(s for _, s in sessions)))} if sessions else {}
        n = names()
        out = []
        for r in rows:
            t = tot.get(r["id"], usage.Totals()).to_dict()
            out.append({"id": r["id"], "user": n.get(r["user_id"]), "started_at": r["started_at"],
                        "duration_s": r["ended_at"] - r["started_at"] if r["ended_at"] else None,
                        "provider": r["provider"], "model": r["model"], "status": r["status"], "stream": bool(r["stream"]),
                        "kind": r["kind"] if is_admin(user) else _user_error_kind(r["kind"]),
                        "rejected_by": r["rejected_by"] if is_admin(user) else _user_rejected_by(r["rejected_by"]),
                        "session_id": r["session_id"],
                        "title": titled.get((r["user_id"], r["session_id"])),
                        **{k: t[k] for k in ("input", "output", "cache_read", "cache_write", "raw", "weighted", "cost_usd")}})
        return {"requests": out}

    @app.get("/api/users")
    async def users(request: Request):
        admin(request)
        now = time.time()
        periods = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
        tot = {k: usage.grouped(conn, cfg.pricing, "started_at>=?", (now - s,), group="user_id") for k, s in periods.items()}
        atts = {b: quota.attribution(conn, cfg.pricing, b, now=now, stale_after_s=cfg.quota.stale_after_s) for b in BUCKETS}
        out = []
        for u in conn.execute("SELECT * FROM users ORDER BY name").fetchall():
            last = conn.execute("SELECT MAX(started_at) FROM requests WHERE user_id=?", (u["id"],)).fetchone()[0]
            out.append({**_public_user(u), "enabled": bool(u["enabled"]), "revoked": u["revoked_at"] is not None,
                        "created_at": u["created_at"], "last_seen": last,
                        "usage": {k: tot[k].get(u["id"], usage.Totals()).to_dict() for k in periods},
                        "share": {b: (None if atts[b]["stale"] else atts[b]["shares"].get(u["id"], 0.0)) for b in BUCKETS},
                        "limits": [s.to_dict() for s in limits.states(conn, cfg, u["id"], now=now)]})
        return {"users": out}

    @app.get("/api/limits")
    async def limits_view(request: Request):
        user = principal(request)
        ids = [r["id"] for r in conn.execute("SELECT id FROM users")] if is_admin(user) else [user["id"]]
        n = names()
        out = {"limits": [{"user": n[i], "user_id": i, **d} for i in ids for d in limit_views(user, limits.states(conn, cfg, i))]}
        return out | {"kinds": {k: list(v) for k, v in limits.UNITS.items()}} if is_admin(user) else out

    @app.get("/api/audit")
    async def audit(request: Request, limit: int = 200):
        admin(request)
        n = names()
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(limit, 1000),)).fetchall()
        # Older entries named the user by id for these actions; show the name, or say the user is gone.
        by_id = ("rotate_key", "enable", "disable", "revoke")
        target = lambda r: (n.get(int(r["target"]), f"user #{r['target']} (deleted)")  # noqa: E731
                            if r["action"] in by_id and (r["target"] or "").isdigit() else r["target"])
        return {"entries": [{**dict(r), "target": target(r),
                             "actor": n.get(r["actor_user_id"], "cli" if r["actor_user_id"] is None else None)} for r in rows]}

    @app.get("/api/me/status")
    async def me_status(request: Request, format: str = "json"):
        user = principal(request, routes_ok=True)
        now = time.time()
        states = limits.states(conn, cfg, user["id"], now=now)
        account = None
        if is_admin(user):
            account = {}
            for b in BUCKETS:
                att = quota.attribution(conn, cfg.pricing, b, now=now, stale_after_s=cfg.quota.stale_after_s)
                account[b] = {"utilization_pct": att["utilization_pct"], "resets_at": att["resets_at"], "stale": att["stale"],
                              "your_estimated_share": att["shares"].get(user["id"], 0.0) if att["utilization_pct"] is not None else None}
        if format == "account":   # gclaude's /account (statusline.sh --warn)
            return PlainTextResponse(_account_line(conn, user) + "\n")
        line = _status_line(user, states, account)
        if format == "text":
            return PlainTextResponse(line + "\n")
        out = {"user": _public_user(user), "limits": limit_views(user, states), "line": line}
        if account is not None:
            out |= {"account": account, "credential_healthy": gw.backend.describe().healthy}
        return out

    def set_name(user_id: int, name, actor: int) -> str:
        try:
            return db.rename_user(conn, user_id, str(name or ""), actor)
        except ValueError as e:
            fail(400, str(e))
        except LookupError as e:
            fail(409, str(e))

    @app.post("/api/me/name")
    async def me_name(request: Request):
        """The name the dashboard and gclaude's status line show for this user."""
        user = principal(request, write=True)
        return {"ok": True, "name": set_name(user["id"], (await _json(request)).get("name"), user["id"])}

    @app.post("/api/me/logout")
    async def me_logout(request: Request):
        """gclaude's /logout: revokes the key that asked when it is one computer's own (authorized from the browser).
        The first key is left alone, since other computers may use it; the client drops its copy either way."""
        if not (request.headers.get("authorization") or request.headers.get("x-api-key")):
            fail(401, "Send the gateway key to sign it out.")
        user = principal(request)
        revoked = bool(user.get("machine_key_id")) and db.remove_machine_key(conn, user["id"], user["machine_key_id"], user["id"])
        return {"ok": True, "revoked": revoked}

    # ---------- browser authorization for `claude-gateway on` (sign-up design section 4) ----------

    def install_command(windows: bool = False) -> str | None:
        if not (cfg.listener.public_url and cfg.listener.dashboard_url):
            return None
        dash = cfg.listener.dashboard_url.rstrip("/")
        return f"irm {dash}/install.ps1 | iex" if windows else f"curl -fsSL {dash}/install | sh"

    def need_urls():
        if not (cfg.listener.public_url and cfg.listener.dashboard_url):
            fail(503, "This gateway has no [listener] public_url and dashboard_url set, so it can't authorize in the browser.")

    @app.get("/install")
    async def install():
        need_urls()
        return PlainTextResponse(install_sh(cfg), media_type="text/x-shellscript", headers={"Cache-Control": "no-cache"})

    @app.get("/install.ps1")
    async def install_ps1_():
        need_urls()
        return PlainTextResponse(install_ps1(cfg), headers={"Cache-Control": "no-cache"})

    def browser_user(request: Request, write: bool = False):
        """Someone signed in to this dashboard in the browser, the only one who may authorize a computer. Not a key
        sent as Bearer, and not a session made from a computer's own key: a leaked key must not mint more."""
        if request.headers.get("authorization") or request.headers.get("x-api-key"):
            fail(403, "Authorize computers in the browser, signed in to the dashboard.")
        user = principal(request, write)
        if user.get("session_key_id"):
            fail(403, "You signed in with a computer's key, which can't authorize another computer. Sign in with "
                      "Google, GitHub, your email or the gateway key the admin gave you.")
        return user

    @app.post("/api/device/start")
    async def device_start(request: Request):
        need_urls()
        ip = client_ip(request)
        starts.check(ip)
        starts.failed(ip)   # every start counts: anyone may make one
        label = " ".join(str((await _json(request)).get("label") or "").split())[:64] or "a computer"
        now = int(time.time())
        conn.execute("DELETE FROM device_requests WHERE expires_at<?", (now,))
        device_code = secrets.token_urlsafe(32)
        while True:
            code = "".join(secrets.choice(USER_CODE_LETTERS) for _ in range(8))
            code = f"{code[:4]}-{code[4:]}"
            try:
                conn.execute("INSERT INTO device_requests(device_hash, user_code, label, created_at, expires_at, ip) VALUES(?,?,?,?,?,?)",
                             (db.hash_key(device_code), code, label, now, now + DEVICE_TTL_S, ip))
                break
            except sqlite3.IntegrityError:
                continue
        page = f"{cfg.listener.dashboard_url.rstrip('/')}/dashboard"
        link = f"{page}#authorize/{code}"
        # For a phone, a QR code of a short link to it (/d/CODE below), all capitals where it can be: QR's
        # alphanumeric mode takes only those, and the code comes out a size or two smaller than the link's.
        # Its rows, "1" a dark module, quiet zone included. Plain ASCII, so the clients draw it in whatever
        # characters their terminal has, whatever the response is decoded as.
        u = urlsplit(cfg.listener.dashboard_url.rstrip("/"))
        short = f"{u.scheme.upper()}://{u.netloc.upper()}{u.path}/D/{code}"
        qr = ["".join("1" if m else "0" for m in row) for row in segno.make(short, error="l", micro=False).matrix_iter(border=2)]
        return {"device_code": device_code, "user_code": code, "verification_uri": f"{page}#authorize",
                "verification_uri_complete": link, "qr": qr, "interval": DEVICE_INTERVAL_S, "expires_in": DEVICE_TTL_S}

    def pending(user_code: str):
        code = "".join(c for c in user_code.upper() if c.isalpha())
        row = conn.execute("SELECT * FROM device_requests WHERE user_code=? AND expires_at>=? AND decision IS NULL",
                           (f"{code[:4]}-{code[4:]}", int(time.time()))).fetchone()
        if row is None:
            fail(404, "No such request waiting: it may have expired (after 10 minutes) or been answered already. "
                      "Run gclaude again for a new code (the first time, the install command).")
        return row

    @app.get("/api/device/{user_code}")
    async def device_view(request: Request, user_code: str):
        browser_user(request)
        row = pending(user_code)
        # The address lets someone spot a request that isn't theirs: the label is whatever the computer says.
        return {"user_code": row["user_code"], "label": row["label"], "created_at": row["created_at"],
                "expires_in": row["expires_at"] - int(time.time()), "ip": row["ip"], "your_ip": client_ip(request)}

    @app.post("/api/device/{user_code}/{decision}")
    async def device_decide(request: Request, user_code: str, decision: str):
        user = browser_user(request, write=True)
        if decision not in ("approve", "deny"):
            fail(404, "Unknown action.")
        row = pending(user_code)
        if conn.execute("UPDATE device_requests SET user_id=?, decision=? WHERE id=? AND decision IS NULL",
                        (user["id"], "approved" if decision == "approve" else "denied", row["id"])).rowcount != 1:
            fail(404, "That request was answered already.")
        db.audit(conn, user["id"], f"device_{decision}", user["name"], {"label": row["label"], "ip": row["ip"]})
        return {"ok": True}

    @app.post("/api/device/token")
    async def device_token(request: Request):
        need_urls()
        body = await _json(request)
        now = time.time()
        row = conn.execute("SELECT * FROM device_requests WHERE device_hash=?", (db.hash_key(str(body.get("device_code", ""))),)).fetchone()
        if row is None:
            fail(400, "invalid_grant")
        if row["expires_at"] < now:
            conn.execute("DELETE FROM device_requests WHERE id=?", (row["id"],))
            fail(400, "expired_token")
        if row["decision"] is None:
            too_soon = row["last_poll_at"] is not None and now - row["last_poll_at"] < DEVICE_INTERVAL_S - 0.5
            conn.execute("UPDATE device_requests SET last_poll_at=? WHERE id=?", (now, row["id"]))
            fail(400, "slow_down" if too_soon else "authorization_pending")
        # Only one poll may take the answer: the key is made now and never stored in plain text.
        if conn.execute("DELETE FROM device_requests WHERE id=? AND decision IS NOT NULL", (row["id"],)).rowcount != 1:
            fail(400, "invalid_grant")
        user = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if row["decision"] != "approved" or user is None or user["revoked_at"] is not None or not user["enabled"]:
            fail(400, "access_denied")
        key = db.add_machine_key(conn, user["id"], row["label"])
        return {"key": key, "user": user["name"], "url": cfg.listener.public_url.rstrip("/"),
                "dashboard": cfg.listener.dashboard_url.rstrip("/")}

    # ---------- machines: the keys authorized from the browser ----------

    @app.get("/api/keys")
    async def keys_list(request: Request, user_id: int | None = None):
        user = principal(request)
        uid = user_id if is_admin(user) and user_id is not None else user["id"]
        return {"keys": db.machine_keys(conn, uid)}

    @app.post("/api/keys/{key_id}/remove")
    async def key_remove(request: Request, key_id: int):
        user = principal(request, write=True)
        row = conn.execute("SELECT user_id FROM keys WHERE id=?", (key_id,)).fetchone()
        if row is None or (row["user_id"] != user["id"] and not is_admin(user)):
            fail(404, "No such machine.")
        # A computer's own key (sent as Bearer, or the session made from it) may sign that computer out and
        # nothing more: a leaked laptop key must not sign the owner's other computers out.
        own = user.get("machine_key_id") or user.get("session_key_id")
        if own and own != key_id:
            fail(403, "A computer's key can only remove that computer. Sign in with your first key to manage "
                      "the others.")
        return {"ok": True, "removed": db.remove_machine_key(conn, row["user_id"], key_id, user["id"])}

    # ---------- admin actions ----------

    def target_user(ref) -> dict:
        u = db.find_user(conn, ref) if ref not in (None, "") else None
        if u is None:
            fail(404, f"No user {ref!r}.")
        return u

    @app.post("/api/admin/users")
    async def create_user(request: Request):
        actor = admin(request, write=True)
        body = await _json(request)
        name = str(body.get("name", "")).strip()
        role = body.get("role", "user")
        if not name or len(name) > 64 or role not in ("user", "admin"):
            fail(400, "Need a name of 1-64 characters and role user or admin.")
        if conn.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
            fail(409, f"User {name!r} already exists.")
        uid, key = db.create_user(conn, name, role=role)
        db.audit(conn, actor["id"], "user_add", name, {"role": role})
        return {"ok": True, "id": uid, "name": name, "key": key}

    @app.post("/api/admin/users/{uid}/{action}")
    async def user_action(request: Request, uid: int, action: str):
        actor = admin(request, write=True)
        u = target_user(uid)
        if action in ("revoke", "disable") and u["id"] == actor["id"]:
            fail(400, "You cannot disable or revoke your own account.")
        if action == "rotate":
            return {"ok": True, "key": db.rotate_key(conn, u["id"], actor["id"])}
        if action == "routes_key":
            try:
                return {"ok": True, "key": db.set_routes_key(conn, u["id"], actor["id"])}
            except ValueError as e:
                fail(400, str(e))
        if action == "upgrade":
            # From the one-time credit to a daily allowance: limits apply together, so the credit has to go.
            try:
                daily = float((await _json(request)).get("cost_daily", 100))
            except (TypeError, ValueError):
                daily = -1.0
            if not (daily > 0 and math.isfinite(daily)):
                fail(400, "Need a daily amount in dollars above 0.")
            conn.execute("DELETE FROM limits WHERE user_id=? AND kind='cost_total'", (u["id"],))
            conn.execute("INSERT OR REPLACE INTO limits(user_id, kind, scope, value, unit, updated_at, updated_by) VALUES(?,?,?,?,?,?,?)",
                         (u["id"], "cost_daily", "*", f"{daily:g}", "usd", int(time.time()), actor["id"]))
            db.audit(conn, actor["id"], "upgrade", u["name"], {"cost_daily": daily})
            return {"ok": True}
        if action == "routes_key_remove":
            return {"ok": True, "removed": db.remove_routes_key(conn, u["id"], actor["id"])}
        if action == "delete":
            if str((await _json(request)).get("confirm", "")) != u["name"]:
                fail(400, "Type the user's name to confirm.")
            try:
                return {"ok": True, "deleted_requests": db.delete_user(conn, u["id"], actor["id"])}
            except ValueError as e:
                fail(400, str(e))
        if action == "enable":
            if u["revoked_at"] is not None:
                fail(400, "A revoked user cannot be re-enabled; delete them and add them again for a new key.")
            db.set_enabled(conn, u["id"], True, actor["id"])
        elif action == "disable":
            db.set_enabled(conn, u["id"], False, actor["id"])
        elif action == "revoke":
            db.revoke(conn, u["id"], actor["id"])
        elif action == "rename":
            set_name(u["id"], (await _json(request)).get("name", ""), actor["id"])
        else:
            fail(404, f"Unknown action {action!r}.")
        return {"ok": True}

    @app.post("/api/admin/limits")
    async def set_limit(request: Request):
        actor = admin(request, write=True)
        body = await _json(request)
        u = target_user(body.get("user") or body.get("user_id"))
        try:
            scope, value, unit = limits.validate(str(body.get("kind", "")), body.get("scope") or "*", body.get("value", ""), body.get("unit"))
        except ValueError as e:
            fail(400, str(e))
        kind = body["kind"]
        conn.execute("INSERT OR REPLACE INTO limits(user_id, kind, scope, value, unit, updated_at, updated_by) VALUES(?,?,?,?,?,?,?)",
                     (u["id"], kind, scope, value, unit, int(time.time()), actor["id"]))
        db.audit(conn, actor["id"], "limit_set", f"{u['name']}:{kind}:{scope}", {"value": value, "unit": unit})
        return {"ok": True, "user": u["name"], "kind": kind, "scope": scope, "value": value, "unit": unit}

    @app.post("/api/admin/limits/delete")
    async def delete_limit(request: Request):
        actor = admin(request, write=True)
        body = await _json(request)
        u = target_user(body.get("user") or body.get("user_id"))
        kind, scope = str(body.get("kind", "")), body.get("scope") or "*"
        if conn.execute("DELETE FROM limits WHERE user_id=? AND kind=? AND scope=?", (u["id"], kind, scope)).rowcount == 0:
            fail(404, "No such limit.")
        db.audit(conn, actor["id"], "limit_clear", f"{u['name']}:{kind}:{scope}")
        return {"ok": True}

    # ---------- page ----------

    @app.get("/")
    async def root():
        return RedirectResponse("/dashboard")

    @app.get("/d/{code}")
    @app.get("/D/{code}")   # as the QR code spells it
    async def device_short_link(code: str):
        code = "".join(c for c in code.upper() if c in USER_CODE_LETTERS)
        page = f"{cfg.listener.dashboard_url.rstrip('/')}/dashboard#authorize"
        return RedirectResponse(f"{page}/{code[:4]}-{code[4:]}" if len(code) == 8 else page)

    # Asset URLs carry a content hash, so a CDN or browser that caches them still picks up a deploy.
    @app.get("/dashboard")
    @app.get("/admin")   # the same page, offering the admin's password sign-in instead of Clerk and keys
    async def page():
        html = (STATIC / "index.html").read_text()
        for name in ("app.js", "app.css"):
            v = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
            html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={v}"')
        return HTMLResponse(html, headers=PAGE_HEADERS)

    @app.get("/privacy")
    async def privacy():
        # Linked from the Google sign-in consent screen, which requires a privacy policy.
        html = (STATIC / "privacy.html").read_text().replace("after 180 days", f"after {cfg.retention_days} days")
        return HTMLResponse(html, headers=PAGE_HEADERS)

    @app.get("/static/{name}")
    async def static(name: str, v: str | None = None):
        path = (STATIC / name).resolve()
        if path.parent != STATIC.resolve() or not path.is_file():
            fail(404, "Not found.")
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable" if v else "no-cache"})

    return app


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        fail(400, "Expected a JSON body.")
    if not isinstance(body, dict):
        fail(400, "Expected a JSON object.")
    return body


def install_sh(cfg) -> str:
    """What `curl -fsSL <dashboard>/install | sh` runs: install.sh, then `claude-gateway on` for this gateway."""
    q = shlex.quote
    return ("#!/bin/sh\n"
            f"# Installs claude-gateway and connects this computer to {cfg.listener.dashboard_url}: it opens the\n"
            "# browser to authorize it, then sets up gclaude.\n"
            "set -eu\n"
            f"curl -fsSL {q(cfg.signup.installer_url)} | sh -s -- on --url {q(cfg.listener.public_url.rstrip('/'))} "
            f"--dashboard {q(cfg.listener.dashboard_url.rstrip('/'))} \"$@\"\n")


def install_ps1(cfg) -> str:
    """What `irm <dashboard>/install.ps1 | iex` runs in Windows PowerShell 5.1 (Windows client design section 4)."""
    q = lambda s: "'" + s.replace("'", "''") + "'"
    dash = cfg.listener.dashboard_url.rstrip("/")
    return (f"# Installs claude-gateway and connects this computer to {dash}: it opens the browser to authorize it,\n"
            "# then sets up gclaude.\n"
            "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor "
            "[Net.SecurityProtocolType]::Tls12\n"
            f"& ([scriptblock]::Create((Invoke-RestMethod {q(cfg.signup.installer_ps1_url)}))) on "
            f"--url {q(cfg.listener.public_url.rstrip('/'))} --dashboard {q(dash)}\n")


def _public_user(u) -> dict:
    return {"id": u["id"], "name": u["name"], "role": u["role"], "prefix": u["key_prefix"],
            "routes_prefix": u["routes_key_prefix"], "email": u["email"]}


def _exhaustion(conn, now: float) -> dict | None:
    """Projected time until the 5h bucket reaches 100%, from its slope over the last 30 minutes."""
    rows = conn.execute("SELECT observed_at, utilization_pct, resets_at FROM quota_snapshots WHERE bucket='5h' AND observed_at>=? "
                        "ORDER BY observed_at", (now - 1800,)).fetchall()
    if len(rows) < 2:
        return None
    rows = quota._window(rows)
    if len(rows) < 2:
        return None
    dt = rows[-1]["observed_at"] - rows[0]["observed_at"]
    du = rows[-1]["utilization_pct"] - rows[0]["utilization_pct"]
    if dt <= 0 or du <= 0:
        return {"pct_per_hour": 0.0, "eta_s": None}
    rate = du / dt
    eta = (100 - rows[-1]["utilization_pct"]) / rate
    resets = rows[-1]["resets_at"]
    return {"pct_per_hour": rate * 3600, "eta_s": eta,
            "before_reset": bool(resets and now + eta < resets)}


def _fmt_pct(v) -> str:
    return "?" if v is None else f"{v:.0f}%"


_PERIOD = {"minute": "per min", "5h": "5h", "daily": "daily", "weekly": "weekly", "monthly": "monthly", "total": "credit"}
_SHARE_PERIOD = {"5h": "5h", "7d": "week", "day": "today"}


def _amount(v: float, unit: str) -> str:
    if unit == "usd":
        return f"${v:,.0f}" if v >= 10 else f"${v:.2f}"
    if unit == "count" or v < 1000:
        return f"{v:,.0f}"
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K"


# What a non-admin sees instead of error kinds and limit names that describe the account.
_USER_ERROR_KINDS = {"upstream_quota": "usage_limit", "upstream_throttle": "usage_limit",
                     "gateway_needs_login": "gateway_unavailable", "gateway_refresh_unavailable": "gateway_unavailable"}


def _user_error_kind(k):
    return _USER_ERROR_KINDS.get(k, k)


def _user_rejected_by(r):
    return limits.USER_KINDS.get(r, r)


def _resets(s) -> str:
    return f" (resets in {limits.human(s.reset_in)})" if s.reset_in else ""


def _account_line(conn, user) -> str:
    """e.g. `maya · maya@example.com · user · key sk-proxy-ab12… for MacBook, authorized 2026-09-20`: who the key
    that asked belongs to, and which key it is."""
    parts = [user["name"]] + ([user["email"]] if user["email"] and user["email"] != user["name"] else []) + [user["role"]]
    if "csrf_token" in user:   # a browser session, not a key
        pass
    elif user.get("machine_key_id"):
        k = conn.execute("SELECT key_prefix, label, created_at FROM keys WHERE id=?", (user["machine_key_id"],)).fetchone()
        parts.append(f"key {k['key_prefix']}… for {k['label']}, authorized {time.strftime('%Y-%m-%d', time.gmtime(k['created_at']))}")
    elif user.get("key_scope") == "routes":
        parts.append(f"routes-only key {user['routes_key_prefix']}…")
    elif user.get("key_scope") == "full":
        parts.append(f"key {user['key_prefix']}… (your first key)")
    return " · ".join(parts)


def _status_line(user, states, account) -> str:
    """e.g. `maya · daily $61/$100 (resets in 3.2 h) · plan 5h 8% (yours 6%) · week 10%`: the user's limits as
    used/limit, each with when its window resets, then, for an admin (`account` given), the shared subscription's quota
    and the estimated part their requests used. A non-admin's share limits read as their own allowance: `5h 30% (resets in 2.1 h)`."""
    parts = [user["name"]]
    for s in states:
        if s.kind == "allowed_models":
            continue
        base, _, period = s.kind.partition("_")
        if base == "share" and account is None:
            label = _SHARE_PERIOD.get(period, period)
            parts.append(f"{label} n/a" if s.skipped or s.current is None else f"{label} {s.pct:.0f}%{_resets(s)}")
            continue
        if base == "share":
            label, used = f"{_SHARE_PERIOD.get(period, period)} share", lambda v: f"{v:.0f}"
        else:
            label, used = _PERIOD.get(period, period), lambda v, u=s.unit: _amount(v, u)
        if s.skipped or s.current is None:
            parts.append(f"{label} n/a")
            continue
        suffix = {"requests": " req", "tokens": " tok", "share": "%"}.get(base, "")
        parts.append(f"{label} {used(s.current)}/{used(s.limit)}{suffix}{_resets(s)}")
    if account is None:
        return " · ".join(parts)
    a5, a7 = account["5h"], account["7d"]
    plan = f"plan 5h {_fmt_pct(a5['utilization_pct'])}"
    if a5["your_estimated_share"]:
        plan += f" (yours {a5['your_estimated_share']:.0f}%)"
    plan += f" · week {_fmt_pct(a7['utilization_pct'])}"
    if a5["stale"] and a5["utilization_pct"] is not None:
        plan += " (stale)"
    parts.append(plan)
    return " · ".join(parts)
