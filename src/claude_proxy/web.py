from __future__ import annotations
import time
import sqlite3
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

def _overview(conn: sqlite3.Connection) -> dict:
    now = int(time.time())
    day_ago = now - 86400
    week_ago = now - 86400*7
    month_ago = now - 86400*30

    def cnt(since):
        r = conn.execute("SELECT COUNT(*) FROM requests WHERE started_at>=? AND rejected_by IS NULL", (since,)).fetchone()
        return r[0] if r else 0

    def token_sums(since):
        r = conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_creation_tokens+cache_creation_5m+cache_creation_1h),0) FROM requests WHERE started_at>=? AND rejected_by IS NULL",
            (since,),
        ).fetchone()
        return {"input": int(r[0] or 0), "output": int(r[1] or 0), "cache_read": int(r[2] or 0), "cache_creation": int(r[3] or 0)}

    users = conn.execute("SELECT COUNT(*) FROM users WHERE enabled=1").fetchone()[0]
    pending = {
        "requests_today": cnt(day_ago),
        "requests_7d": cnt(week_ago),
        "requests_30d": cnt(month_ago),
        "tokens_today": token_sums(day_ago),
        "tokens_7d": token_sums(week_ago),
        "tokens_30d": token_sums(month_ago),
        "active_users": users,
        "total_requests": conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
        "meter_errors": conn.execute("SELECT COUNT(*) FROM requests WHERE meter_error=1").fetchone()[0],
    }
    return pending

def _users_leaderboard(conn: sqlite3.Connection) -> list:
    # Multi-user leaderboard with token sums (weighted approx: use output 5x etc via python helper if needed)
    rows = conn.execute("""
        SELECT u.id, u.name, u.role, u.key_prefix, u.enabled,
               (SELECT COUNT(*) FROM requests r WHERE r.user_id=u.id AND r.started_at>=strftime('%s','now','-1 day') AND r.rejected_by IS NULL) as today,
               (SELECT COUNT(*) FROM requests r WHERE r.user_id=u.id AND r.started_at>=strftime('%s','now','-7 days') AND r.rejected_by IS NULL) as week,
               (SELECT COUNT(*) FROM requests r WHERE r.user_id=u.id AND r.started_at>=strftime('%s','now','-30 days') AND r.rejected_by IS NULL) as month,
               (SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens+cache_creation_5m+cache_creation_1h),0) FROM requests r WHERE r.user_id=u.id AND r.started_at>=strftime('%s','now','-1 day') AND r.rejected_by IS NULL) as tokens_today,
               (SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens+cache_creation_5m+cache_creation_1h),0) FROM requests r WHERE r.user_id=u.id AND r.started_at>=strftime('%s','now','-7 days') AND r.rejected_by IS NULL) as tokens_week,
               (SELECT MAX(started_at) FROM requests r WHERE r.user_id=u.id) as last_seen
        FROM users u ORDER BY today DESC
    """).fetchall()
    out=[]
    for r in rows:
        limits = conn.execute("SELECT kind, value, unit FROM limits WHERE user_id=?", (r["id"],)).fetchall()
        lim = [{"kind": x["kind"], "value": x["value"], "unit": x["unit"]} for x in limits]
        out.append(dict(id=r["id"], name=r["name"], role=r["role"], prefix=r["key_prefix"], enabled=bool(r["enabled"]), today=r["today"], week=r["week"], month=r["month"], tokens_today=int(r["tokens_today"] or 0), tokens_week=int(r["tokens_week"] or 0), last_seen=r["last_seen"], limits=lim))
    return out

def _auth_user(request: Request):
    """Authenticate via Bearer virtual key OR session cookie (for password login)."""
    from .auth import authenticate
    conn = request.app.state.db_conn
    auth = request.headers.get("authorization")
    if auth:
        try:
            return authenticate(conn, auth)
        except HTTPException as e:
            # If Bearer present but invalid, fall through to cookie, but if both fail, raise Bearer error
            cookie_tok = request.cookies.get("session")
            if cookie_tok:
                from .db import find_user_by_session
                u = find_user_by_session(conn, cookie_tok)
                if u:
                    return u
            raise e
    # no Bearer, try cookie
    cookie_tok = request.cookies.get("session")
    if cookie_tok:
        from .db import find_user_by_session
        u = find_user_by_session(conn, cookie_tok)
        if u:
            return u
    raise HTTPException(status_code=401, detail={"type": "error", "error": {"type": "authentication_error", "message": "Missing authentication: login with password or provide Bearer token"}})

def _require_admin(request: Request):
    user = _auth_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail={"type": "error", "error": {"type": "permission_error", "message": "admin only"}})
    return user

def register_web(app, cfg):
    @app.get("/api/overview")
    async def api_overview(request: Request):
        conn = request.app.state.db_conn
        return JSONResponse(_overview(conn))

    @app.get("/api/users")
    async def api_users(request: Request):
        conn = request.app.state.db_conn
        return JSONResponse({"users": _users_leaderboard(conn)})

    @app.get("/api/limits")
    async def api_limits(request: Request):
        conn = request.app.state.db_conn
        user = request.query_params.get("user")
        if user:
            row = conn.execute("SELECT id FROM users WHERE name=?", (user,)).fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "user not found"})
            rows = conn.execute("SELECT kind, value, unit, updated_at FROM limits WHERE user_id=?", (row["id"],)).fetchall()
            return JSONResponse({"user": user, "limits": [dict(r) for r in rows]})
        rows = conn.execute("SELECT u.name, l.kind, l.value, l.unit, l.updated_at FROM limits l JOIN users u ON u.id=l.user_id ORDER BY u.name, l.kind").fetchall()
        return JSONResponse({"limits": [dict(r) for r in rows]})

    @app.post("/api/login")
    async def api_login(request: Request):
        body = await request.json()
        username = (body.get("username") or body.get("name") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return JSONResponse(status_code=400, content={"error": "need username and password"})
        conn = request.app.state.db_conn
        row = conn.execute("SELECT * FROM users WHERE name=? AND role='admin'", (username,)).fetchone()
        if not row or not row["password_hash"]:
            return JSONResponse(status_code=401, content={"error": "invalid credentials"})
        try:
            from argon2 import PasswordHasher
            ph = PasswordHasher()
            ph.verify(row["password_hash"], password)
        except Exception:
            return JSONResponse(status_code=401, content={"error": "invalid credentials"})
        # create session
        from .db import create_session, cleanup_sessions
        try:
            cleanup_sessions(conn)
        except:
            pass
        raw = create_session(conn, row["id"], ttl_s=7*86400)
        resp = JSONResponse({"ok": True, "user": {"id": row["id"], "name": row["name"]}})
        # HttpOnly cookie; Secure only if request is https, but we set Lax for http
        resp.set_cookie(key="session", value=raw, max_age=7*86400, httponly=True, samesite="lax", path="/")
        return resp

    @app.post("/api/logout")
    async def api_logout(request: Request):
        tok = request.cookies.get("session")
        if tok:
            from .db import delete_session
            try:
                delete_session(request.app.state.db_conn, tok)
            except:
                pass
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(key="session", path="/")
        return resp

    @app.get("/api/session")
    async def api_session(request: Request):
        try:
            user = _auth_user(request)
            return JSONResponse({"user": {"id": user["id"], "name": user["name"], "role": user["role"], "prefix": user["key_prefix"]}})
        except HTTPException as e:
            return JSONResponse(status_code=401, content={"error": "not authenticated"})

    @app.post("/api/admin/rename")
    async def api_rename(request: Request):
        admin = _require_admin(request)
        body = await request.json()
        old = (body.get("old") or body.get("old_name") or "").strip()
        new = (body.get("new") or body.get("new_name") or body.get("name") or "").strip()
        user_id = body.get("user_id")
        conn = request.app.state.db_conn
        if user_id is not None:
            try:
                user_id = int(user_id)
            except:
                return JSONResponse(status_code=400, content={"error": "invalid user_id"})
            row = conn.execute("SELECT id, name FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "user not found"})
            old = row["name"]
        else:
            if not old or not new:
                return JSONResponse(status_code=400, content={"error": "need {old,new} or {user_id,new}"})
            row = conn.execute("SELECT id FROM users WHERE name=?", (old,)).fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": f"user '{old}' not found"})
            user_id = row["id"]
        if not new or len(new) < 1 or len(new) > 64:
            return JSONResponse(status_code=400, content={"error": "invalid new name"})
        if conn.execute("SELECT id FROM users WHERE name=?", (new,)).fetchone():
            return JSONResponse(status_code=409, content={"error": f"name '{new}' already exists"})
        if new == old:
            return JSONResponse(status_code=400, content={"error": "no change"})
        conn.execute("UPDATE users SET name=? WHERE id=?", (new, user_id))
        conn.execute("INSERT INTO audit_log(at, actor_user_id, action, target, detail_json) VALUES(?,?,?,?,?)",
                     (int(time.time()), admin["id"], "rename", f"{old}->{new}", f'{{"user_id":{user_id}}}'))
        conn.commit()
        return JSONResponse({"ok": True, "id": user_id, "old": old, "new": new})

    @app.post("/api/admin/limit/set")
    async def api_limit_set(request: Request):
        admin = _require_admin(request)
        body = await request.json()
        # accept user (name or id)
        user_ref = body.get("user") or body.get("name") or body.get("user_id")
        kind = (body.get("kind") or "").strip()
        value = body.get("value")
        unit = (body.get("unit") or "weighted").strip()
        if not user_ref or not kind or value is None:
            return JSONResponse(status_code=400, content={"error": "need {user, kind, value}"})
        valid_kinds = {"tokens_5h","tokens_daily","tokens_weekly","requests_daily","share_5h","share_7d","allowed_models","enabled"}
        if kind not in valid_kinds:
            return JSONResponse(status_code=400, content={"error": f"invalid kind, must be one of {sorted(valid_kinds)}"})
        valid_units = {"raw","weighted","pct","count","list"}
        if unit not in valid_units:
            return JSONResponse(status_code=400, content={"error": f"invalid unit {unit}"})
        conn = request.app.state.db_conn
        # resolve user
        if isinstance(user_ref, int) or (isinstance(user_ref, str) and user_ref.isdigit()):
            row = conn.execute("SELECT id, name FROM users WHERE id=?", (int(user_ref),)).fetchone()
        else:
            row = conn.execute("SELECT id, name FROM users WHERE name=?", (str(user_ref),)).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": f"user '{user_ref}' not found"})
        uid = row["id"]
        uname = row["name"]
        # normalize value
        val_str = str(value).strip()
        conn.execute("INSERT OR REPLACE INTO limits(user_id, kind, value, unit, updated_at) VALUES(?,?,?,?,?)",
                     (uid, kind, val_str, unit, int(time.time())))
        conn.execute("INSERT INTO audit_log(at, actor_user_id, action, target, detail_json) VALUES(?,?,?,?,?)",
                     (int(time.time()), admin["id"], "limit_set", f"{uname}:{kind}", f'{{"value":"{val_str}","unit":"{unit}"}}'))
        conn.commit()
        return JSONResponse({"ok": True, "user": uname, "kind": kind, "value": val_str, "unit": unit})

    @app.post("/api/admin/limit/clear")
    async def api_limit_clear(request: Request):
        admin = _require_admin(request)
        body = await request.json()
        user_ref = body.get("user") or body.get("name") or body.get("user_id")
        kind = (body.get("kind") or "").strip()
        if not user_ref or not kind:
            return JSONResponse(status_code=400, content={"error": "need {user, kind}"})
        conn = request.app.state.db_conn
        if isinstance(user_ref, int) or (isinstance(user_ref, str) and user_ref.isdigit()):
            row = conn.execute("SELECT id, name FROM users WHERE id=?", (int(user_ref),)).fetchone()
        else:
            row = conn.execute("SELECT id, name FROM users WHERE name=?", (str(user_ref),)).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": f"user '{user_ref}' not found"})
        uid = row["id"]
        cur = conn.execute("DELETE FROM limits WHERE user_id=? AND kind=?", (uid, kind))
        conn.commit()
        if cur.rowcount == 0:
            return JSONResponse(status_code=404, content={"error": "limit not found"})
        conn.execute("INSERT INTO audit_log(at, actor_user_id, action, target) VALUES(?,?,?,?)",
                     (int(time.time()), admin["id"], "limit_clear", f"{row['name']}:{kind}"))
        conn.commit()
        return JSONResponse({"ok": True})

    @app.get("/api/me/status")
    async def api_me_status(request: Request):
        try:
            user = _auth_user(request)
        except HTTPException as e:
            raise e
        conn = request.app.state.db_conn
        cred = request.app.state.backend.describe() if request.app.state.backend else None
        # Compute limits with current values for this user
        try:
            from .limits import _get_limits, WINDOWS
            from .meter import weighted_from_row
            from .config import Config
            cfg = request.app.state.config
            lims = _get_limits(conn, user["id"])
            now = int(time.time())
            enriched = []
            for kind, (val, unit) in lims.items():
                cur = None
                remaining = None
                reset_in = None
                if kind in WINDOWS:
                    window = WINDOWS[kind]
                    since = now - window
                    if kind.startswith("tokens"):
                        use_weighted = unit == "weighted"
                        if unit not in ("raw", "weighted"):
                            use_weighted = True
                        if use_weighted:
                            rows = conn.execute("SELECT input_tokens, output_tokens, cache_creation_tokens, cache_creation_5m, cache_creation_1h, cache_read_tokens, model FROM requests WHERE user_id=? AND started_at>=? AND rejected_by IS NULL", (user["id"], since)).fetchall()
                            cur = sum(weighted_from_row(dict(r), cfg.weights) for r in rows)
                        else:
                            row = conn.execute("SELECT COALESCE(SUM(input_tokens+output_tokens+cache_creation_tokens+cache_creation_5m+cache_creation_1h+cache_read_tokens),0) FROM requests WHERE user_id=? AND started_at>=? AND rejected_by IS NULL", (user["id"], since)).fetchone()
                            cur = float(row[0] or 0)
                    elif kind == "requests_daily":
                        row = conn.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND started_at>=? AND rejected_by IS NULL", (user["id"], since)).fetchone()
                        cur = float(row[0] or 0)
                    else:  # share
                        cur = 0.0
                    try:
                        remaining = float(val) - (cur or 0)
                    except:
                        remaining = None
                    oldest = conn.execute("SELECT MIN(started_at) FROM requests WHERE user_id=? AND started_at>=? AND rejected_by IS NULL", (user["id"], since)).fetchone()[0]
                    if oldest:
                        reset_in = max(0, int(oldest + window - now))
                enriched.append({"kind": kind, "value": val, "unit": unit, "current": cur, "remaining": remaining, "reset_in": reset_in})
        except Exception as e:
            enriched = []
        # Share staleness warning
        share_warning = None
        try:
            row = conn.execute("SELECT observed_at FROM quota_snapshots ORDER BY observed_at DESC LIMIT 1").fetchone()
            if not row or (int(time.time()) - int(row["observed_at"]) > 1800):
                if any(k.startswith("share") for k in lims.keys()):
                    share_warning = "share limits are skipped — no quota snapshot <30m (need Phase 4 headers/poll)"
        except:
            pass
        return JSONResponse({
            "user": {"id": user["id"], "name": user["name"], "prefix": user["key_prefix"]},
            "limits": enriched,
            "credential": {"healthy": cred.healthy if cred else False, "detail": cred.detail if cred else "no backend"},
            "share_warning": share_warning,
        })

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        # Serve static inline — ECharts via CDN, no build step (spec 10)
        import pathlib
        p = pathlib.Path(__file__).parent / ".." / ".." / "static" / "dashboard" / "index.html"
        # fallback inline if file missing
        try:
            html = p.read_text()
        except:
            html = "<h1>Dashboard placeholder — static/dashboard/index.html missing</h1>"
        return HTMLResponse(html)

    @app.get("/dashboard/", response_class=HTMLResponse)
    async def dashboard_slash():
        return await dashboard()
