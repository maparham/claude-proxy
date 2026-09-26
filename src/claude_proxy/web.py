"""Dashboard listener: static page plus a JSON API (spec section 10).

Access (spec 10.1):
- admin: username + password (argon2id) -> session cookie (HttpOnly, SameSite=Strict, Secure behind TLS);
- user: their virtual key, either exchanged for a session cookie or sent as a Bearer token (statusline);
- cookie sessions must send the session's CSRF token in `x-csrf-token` on every state change;
- no loopback or IP-based exemptions; failed logins are rate limited per client address.
Non-admins only ever see their own usage, their own limits and their own estimated share.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections import defaultdict, deque
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import db, limits, quota, usage
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


def fail(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)


class LoginLimiter:
    def __init__(self, max_failures: int = 5, window_s: int = 300):
        self.max_failures = max_failures
        self.window_s = window_s
        self.failures: dict[str, deque] = defaultdict(deque)

    def check(self, ip: str) -> None:
        now = time.time()
        q = self.failures.get(ip)
        while q and now - q[0] > self.window_s:
            q.popleft()
        if q is not None and not q:
            del self.failures[ip]   # keep the table to addresses with recent failures
        elif q and len(q) >= self.max_failures:
            fail(429, f"Too many failed logins. Try again in {int(self.window_s - (now - q[0])) + 1}s.")

    def failed(self, ip: str) -> None:
        now = time.time()
        if len(self.failures) > 10_000:
            for k in [k for k, q in self.failures.items() if not q or now - q[-1] > self.window_s]:
                del self.failures[k]
        self.failures[ip].append(now)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net/npm/echarts@5.6.0/; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp


def create_dashboard_app(gw: Gateway) -> FastAPI:
    app = FastAPI(title="claude-proxy dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityHeaders)
    app.state.gw = gw
    limiter = LoginLimiter()
    conn, cfg = gw.conn, gw.cfg

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

    def start_session(user) -> JSONResponse:
        raw, csrf = db.create_session(conn, user["id"])
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
        return start_session(user)

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
               "settings": {"reference_model": cfg.pricing.reference_model}}
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
        line = _status_line(user, states, account)
        if format == "text":
            return PlainTextResponse(line + "\n")
        out = {"user": _public_user(user), "limits": limit_views(user, states), "line": line}
        if account is not None:
            out |= {"account": account, "credential_healthy": gw.backend.describe().healthy}
        return out

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
        if action == "routes_key_remove":
            return {"ok": True, "removed": db.remove_routes_key(conn, u["id"], actor["id"])}
        if action == "delete":
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
            new = str((await _json(request)).get("name", "")).strip()
            if not new or len(new) > 64:
                fail(400, "Need a name of 1-64 characters.")
            if conn.execute("SELECT 1 FROM users WHERE name=? AND id!=?", (new, u["id"])).fetchone():
                fail(409, f"User {new!r} already exists.")
            conn.execute("UPDATE users SET name=? WHERE id=?", (new, u["id"]))
            db.audit(conn, actor["id"], "rename", f"{u['name']}->{new}")
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

    # Asset URLs carry a content hash, so a CDN or browser that caches them still picks up a deploy.
    @app.get("/dashboard")
    async def page():
        html = (STATIC / "index.html").read_text()
        for name in ("app.js", "app.css"):
            v = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
            html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={v}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

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


def _public_user(u) -> dict:
    return {"id": u["id"], "name": u["name"], "role": u["role"], "prefix": u["key_prefix"],
            "routes_prefix": u["routes_key_prefix"]}


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


_PERIOD = {"minute": "per min", "5h": "5h", "daily": "daily", "weekly": "weekly", "monthly": "monthly"}


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
    # A share limit follows the account's bucket, which resets all at once; the others are rolling windows.
    if not s.reset_in:
        return ""
    return f" ({'resets' if s.kind in limits.SHARE_BUCKETS else 'frees'} in {limits.human(s.reset_in)})"


def _status_line(user, states, account) -> str:
    """e.g. `maya · daily $61/$100 (frees in 3.2 h) · plan 5h 8% (yours 6%) · week 10%`: the user's limits as
    used/limit, each with when its oldest counted usage leaves the rolling window (when over the limit: when enough has
    left to be under it), then, for an admin (`account` given), the shared subscription's quota and the estimated part
    their requests used. A non-admin's share limits read as their own allowance: `5h 30% (resets in 2.1 h)`."""
    parts = [user["name"]]
    for s in states:
        if s.kind == "allowed_models":
            continue
        base, _, period = s.kind.partition("_")
        if base == "share" and account is None:
            label = "5h" if period == "5h" else "week"
            parts.append(f"{label} n/a" if s.skipped or s.current is None else f"{label} {s.pct:.0f}%{_resets(s)}")
            continue
        if base == "share":
            label, used = f"{'5h' if period == '5h' else 'week'} share", lambda v: f"{v:.0f}"
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
