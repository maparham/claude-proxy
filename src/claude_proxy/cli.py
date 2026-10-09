from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import time

import httpx

from . import db, estimates, limits, orders, payments
from .config import Config, ConfigError
from .credentials import CredentialKeyMissing, OAuthBackend, check_key, generate_key

logger = logging.getLogger("claude_proxy")


def _conn(cfg: Config):
    return db.init_db(cfg.db.path)


def _user(conn, ref):
    u = db.find_user(conn, ref)
    if u is None:
        sys.exit(f"No user {ref!r}.")
    return u


def cmd_keygen(args, cfg):
    print(generate_key())


def cmd_init(args, cfg):
    check_key()
    conn = _conn(cfg)
    if conn.execute("SELECT 1 FROM users WHERE name=?", (args.admin_name,)).fetchone():
        sys.exit(f"User {args.admin_name!r} already exists.")
    password = os.environ.get("CLAUDE_PROXY_ADMIN_PASSWORD") or getpass.getpass("Admin dashboard password: ")
    if len(password) < 10:
        sys.exit("Use a password of at least 10 characters.")
    if not os.environ.get("CLAUDE_PROXY_ADMIN_PASSWORD") and getpass.getpass("Repeat: ") != password:
        sys.exit("Passwords differ.")
    from argon2 import PasswordHasher
    uid, key = db.create_user(conn, args.admin_name, role="admin", password_hash=PasswordHasher().hash(password))
    db.audit(conn, None, "init", args.admin_name)
    print(f"Admin {args.admin_name!r} created. Database: {cfg.db.path}")
    print(f"Admin's own gateway key (shown once): {key}")
    if not OAuthBackend(cfg, conn, httpx.AsyncClient()).describe().healthy:
        print("Next: `claude-proxy login` to link the Claude subscription.")


def cmd_login(args, cfg):
    """The gateway's own OAuth grant (spec 5.1). Never reads Claude Code's stored credentials.

    Interactive by default; `--print-url` then `--code` splits it across two commands.
    """
    from . import login
    check_key()
    conn = _conn(cfg)
    if not args.code:
        url = login.start(conn, cfg)
        print("1. Open this URL in a browser signed in to the Claude account that owns the subscription:\n")
        print(f"   {url}\n")
        print("2. Approve access, then copy the code the page shows (or the whole URL you land on).")
        if args.print_url:
            print("3. Run: claude-proxy login --code '<code>'   (within 15 minutes)")
            return
        pasted = input("\nPaste code: ")
    else:
        pasted = args.code

    async def run():
        async with httpx.AsyncClient() as http:
            return await login.finish(conn, cfg, http, pasted)
    try:
        record = asyncio.run(run())
    except login.LoginError as e:
        sys.exit(str(e))
    print(f"Linked{' ' + record['account'] if record.get('account') else ''}. Access token valid until "
          f"{time.ctime(record['expires_at'])}; the gateway refreshes it from now on.")
    print("Do not reuse this grant anywhere else: refresh tokens are single-use.")


def cmd_status(args, cfg):
    conn = _conn(cfg)
    be = OAuthBackend(cfg, conn, httpx.AsyncClient())
    st = be.describe()
    print(f"credential: {'OK' if st.healthy else 'NOT OK'} — {st.detail}")
    if st.expires_at:
        print(f"access token expires: {time.ctime(st.expires_at)}")
    for r in conn.execute("SELECT bucket, utilization_pct, resets_at, observed_at, source FROM quota_snapshots q "
                          "WHERE observed_at = (SELECT MAX(observed_at) FROM quota_snapshots WHERE bucket=q.bucket) ORDER BY bucket"):
        age = int(time.time() - r["observed_at"])
        print(f"account {r['bucket']}: {r['utilization_pct']:.0f}% (resets {time.ctime(r['resets_at']) if r['resets_at'] else '?'}; "
              f"{r['source']} {age}s ago)")
    for route in cfg.routes:
        print(f"route {route.name}: models {', '.join(route.models)} -> {route.base_url} "
              f"({route.api_key_env} {'set' if route.api_key() else 'NOT SET'})")
    n = conn.execute("SELECT COUNT(*) FROM users WHERE enabled=1 AND revoked_at IS NULL").fetchone()[0]
    print(f"active users: {n}")


def cmd_user_add(args, cfg):
    conn = _conn(cfg)
    if conn.execute("SELECT 1 FROM users WHERE name=?", (args.name,)).fetchone():
        sys.exit(f"User {args.name!r} already exists.")
    uid, key = db.create_user(conn, args.name, role=args.role)
    db.audit(conn, None, "user_add", args.name, {"role": args.role})
    print(f"User {args.name!r} (id {uid}). Gateway key, shown once:\n{key}")


def cmd_user_list(args, cfg):
    conn = _conn(cfg)
    for r in conn.execute("SELECT * FROM users ORDER BY id"):
        state = "revoked" if r["revoked_at"] else ("enabled" if r["enabled"] else "disabled")
        opencode = f"  opencode {r['routes_key_prefix']}…" if r["routes_key_prefix"] else ""
        print(f"{r['id']:>3}  {r['name']:<16} {r['role']:<5} {r['key_prefix']}…  {state}{opencode}")


def cmd_user_rotate(args, cfg):
    conn = _conn(cfg)
    u = _user(conn, args.user)
    key = db.rotate_key(conn, u['id'])
    print(f"New gateway key for {u['name']}, shown once (the old key and every computer authorized under it are signed out):\n{key}")


def cmd_user_routes_key(args, cfg):
    conn = _conn(cfg)
    u = _user(conn, args.user)
    if args.remove:
        removed = db.remove_routes_key(conn, u["id"])
        print(f"{u['name']}: OpenCode key removed" if removed else f"{u['name']} has no OpenCode key.")
        return
    try:
        key = db.set_routes_key(conn, u["id"])
    except ValueError as e:
        sys.exit(str(e))
    print(f"OpenCode key for {u['name']} (third-party models only, never Claude), shown once:\n{key}")


def cmd_user_state(args, cfg):
    conn = _conn(cfg)
    u = _user(conn, args.user)
    if args.action == "revoke":
        db.revoke(conn, u["id"])
    else:
        db.set_enabled(conn, u["id"], args.action == "enable")
    print(f"{u['name']}: {args.action}d")


def cmd_user_passwd(args, cfg):
    from argon2 import PasswordHasher
    conn = _conn(cfg)
    u = _user(conn, args.user)
    if u["role"] != "admin":
        sys.exit("Only admins have a dashboard password; users sign in with their gateway key.")
    password = getpass.getpass(f"New dashboard password for {u['name']}: ")
    if len(password) < 10:
        sys.exit("Use a password of at least 10 characters.")
    if getpass.getpass("Repeat: ") != password:
        sys.exit("Passwords differ.")
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (PasswordHasher().hash(password), u["id"]))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
    db.audit(conn, None, "passwd", u["name"])
    print(f"Password changed for {u['name']}; existing dashboard sessions signed out.")


def cmd_user_delete(args, cfg):
    conn = _conn(cfg)
    u = _user(conn, args.user)
    if not args.yes:
        try:
            answer = input("Type the user's name to confirm deletion: ")
        except EOFError:
            sys.exit("No confirmation (not a terminal); pass --yes to delete without the prompt.")
        if answer != u["name"]:
            sys.exit("Names differ; nothing deleted.")
    try:
        n = db.delete_user(conn, u["id"])
    except ValueError as e:
        sys.exit(str(e))
    print(f"Deleted {u['name']} and {n} recorded requests.")


def cmd_user_rename(args, cfg):
    conn = _conn(cfg)
    u = _user(conn, args.user)
    try:
        new = db.rename_user(conn, u["id"], args.new, None)
    except (ValueError, LookupError) as e:
        sys.exit(str(e))
    print(f"Renamed {u['name']} -> {new}")


def cmd_limit_set(args, cfg):
    conn = _conn(cfg)
    u = _user(conn, args.user)
    try:
        scope, value, unit = limits.validate(args.kind, args.scope, args.value, args.unit)
    except ValueError as e:
        sys.exit(str(e))
    conn.execute("INSERT OR REPLACE INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,?)",
                 (u["id"], args.kind, scope, value, unit, int(time.time())))
    db.audit(conn, None, "limit_set", f"{u['name']}:{args.kind}:{scope}", {"value": value, "unit": unit})
    print(f"{u['name']}: {args.kind}{'' if scope == '*' else ' [' + scope + ']'} = {value} {unit}")


def cmd_limit_clear(args, cfg):
    conn = _conn(cfg)
    u = _user(conn, args.user)
    n = conn.execute("DELETE FROM limits WHERE user_id=? AND kind=? AND scope=?", (u["id"], args.kind, args.scope)).rowcount
    db.audit(conn, None, "limit_clear", f"{u['name']}:{args.kind}:{args.scope}")
    print("cleared" if n else "no such limit")


def cmd_limit_list(args, cfg):
    conn = _conn(cfg)
    users = [_user(conn, args.user)] if args.user else conn.execute("SELECT * FROM users ORDER BY name").fetchall()
    for u in users:
        for s in limits.states(conn, cfg, u["id"]):
            cur = "skipped: " + s.skipped if s.skipped else ("" if s.current is None else f"{s.current:,.1f} used")
            scope = "" if s.scope == "*" else f" [{s.scope}]"
            print(f"{u['name']:<16} {s.kind}{scope:<18} {s.value:>12} {s.unit:<8} {cur}")


def cmd_serve(args, cfg):
    try:
        check_key()
    except CredentialKeyMissing as e:
        sys.exit(str(e))
    asyncio.run(_serve(cfg))


async def _serve(cfg: Config):
    import uvicorn
    from .app import create_app
    from .gateway import Gateway
    from .web import create_dashboard_app

    conn = _conn(cfg)
    gw = Gateway(cfg, conn)
    servers = [
        uvicorn.Server(uvicorn.Config(create_app(gw), host=cfg.listener.host, port=cfg.listener.port, log_level="info")),
        uvicorn.Server(uvicorn.Config(create_dashboard_app(gw), host=cfg.listener.dashboard_host,
                                      port=cfg.listener.dashboard_port, log_level="info")),
    ]
    print(f"proxy:     http://{cfg.listener.host}:{cfg.listener.port}   (ANTHROPIC_BASE_URL)")
    print(f"dashboard: http://{cfg.listener.dashboard_host}:{cfg.listener.dashboard_port}/dashboard")
    payments.warn_if_off(cfg)
    background = [asyncio.create_task(gw.poller.run()), asyncio.create_task(gw.status_page.run()), asyncio.create_task(_maintenance(conn, cfg)),
                  asyncio.create_task(_reconcile_payments(conn, cfg))]
    running = [asyncio.create_task(s.serve()) for s in servers]
    try:
        await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*running, return_exceptions=True)
        for t in background:
            t.cancel()
        from . import web
        for t in list(web._mail_tasks):   # their mail stays "pending" (spec section 5); a send already in smtplib finishes in its thread
            t.cancel()
        await gw.aclose()


async def _maintenance(conn, cfg: Config):
    while True:
        # Each task on its own, so one failing does not skip the other.
        try:
            removed = db.cleanup(conn, cfg.retention_days)
            if any(removed.values()):
                logger.info("retention cleanup: %s", removed)
        except Exception:
            logger.exception("retention cleanup failed")
        try:
            done = estimates.refresh_if_due(conn, cfg)
            if done:
                logger.info("usage estimates refreshed: %s", done)
        except Exception:
            logger.exception("usage estimates refresh failed")
        try:
            cleared = orders.clear_old_ips(conn)
            if cleared:
                logger.info("cleared the IP of %d orders older than 30 days", cleared)
        except Exception:
            logger.exception("clearing old order IPs failed")
        await asyncio.sleep(6 * 3600)


RECONCILE_EVERY_S = 300


async def _reconcile_payments(conn, cfg: Config):
    """Payments design, section 4 (Expiry): settle payments left `started`, every few minutes."""
    while True:
        await _reconcile_once(conn, cfg)
        await asyncio.sleep(RECONCILE_EVERY_S)


async def _reconcile_once(conn, cfg: Config):
    try:
        out = await payments.reconcile(conn, cfg)
    except Exception:
        logger.exception("reconciling payments failed")
        return
    if any(v for k, v in out.items() if k != "changed"):
        logger.info("reconciled unfinished payments: %s", out)
    if cfg.email is not None:
        for pid in out["changed"]:   # each moved to paid/paid_unfulfilled by this run only: mail once
            await payments.send_mails(conn, cfg, pid)


def main(argv=None):
    p = argparse.ArgumentParser(prog="claude-proxy", description="Shared-subscription gateway for Claude Code")
    p.add_argument("--config", help="TOML config file (or CLAUDE_PROXY_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keygen", help="print a new credential encryption key").set_defaults(func=cmd_keygen)
    s = sub.add_parser("init", help="create the database and the admin account")
    s.add_argument("--admin-name", default="admin")
    s.set_defaults(func=cmd_init)
    s = sub.add_parser("login", help="link the Claude subscription (OAuth, PKCE)")
    s.add_argument("--print-url", action="store_true", help="print the URL and exit; finish with --code")
    s.add_argument("--code", help="finish a login started with --print-url")
    s.set_defaults(func=cmd_login)
    sub.add_parser("status", help="credential, quota and route health").set_defaults(func=cmd_status)
    sub.add_parser("serve", help="run the proxy and dashboard listeners").set_defaults(func=cmd_serve)

    u = sub.add_parser("user").add_subparsers(dest="sub", required=True)
    s = u.add_parser("add"); s.add_argument("name"); s.add_argument("--role", choices=["user", "admin"], default="user"); s.set_defaults(func=cmd_user_add)
    u.add_parser("list").set_defaults(func=cmd_user_list)
    s = u.add_parser("rotate"); s.add_argument("user"); s.set_defaults(func=cmd_user_rotate)
    s = u.add_parser("routes-key", help="issue, replace or --remove a user's OpenCode key (third-party models only)")
    s.add_argument("user"); s.add_argument("--remove", action="store_true"); s.set_defaults(func=cmd_user_routes_key)
    for action in ("enable", "disable", "revoke"):
        s = u.add_parser(action); s.add_argument("user"); s.set_defaults(func=cmd_user_state, action=action)
    s = u.add_parser("rename"); s.add_argument("user"); s.add_argument("new"); s.set_defaults(func=cmd_user_rename)
    s = u.add_parser("delete", help="remove a revoked user and their usage history"); s.add_argument("user"); s.add_argument("--yes", action="store_true", help="skip the typed confirmation"); s.set_defaults(func=cmd_user_delete)
    s = u.add_parser("passwd", help="change an admin's dashboard password"); s.add_argument("user"); s.set_defaults(func=cmd_user_passwd)

    lm = sub.add_parser("limit").add_subparsers(dest="sub", required=True)
    s = lm.add_parser("set")
    s.add_argument("user"); s.add_argument("kind", choices=limits.KINDS); s.add_argument("value")
    s.add_argument("--unit"); s.add_argument("--scope", default="*", help="model glob, e.g. 'claude-opus-*'")
    s.set_defaults(func=cmd_limit_set)
    s = lm.add_parser("clear"); s.add_argument("user"); s.add_argument("kind"); s.add_argument("--scope", default="*"); s.set_defaults(func=cmd_limit_clear)
    s = lm.add_parser("list"); s.add_argument("user", nargs="?"); s.set_defaults(func=cmd_limit_list)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        sys.exit(str(e))
    try:
        args.func(args, cfg)
    except CredentialKeyMissing as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
