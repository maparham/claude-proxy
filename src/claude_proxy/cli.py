from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

from .config import Config
from .db import init_db, create_user
from .credentials import generate_pkce, build_authorize_url, encrypt_blob


def _get_conn(cfg: Config):
    return init_db(cfg.db.path)


def cmd_init(args):
    cfg = Config.load(args.config)
    conn = _get_conn(cfg)
    cur = conn.execute("SELECT id FROM users WHERE name=?", (args.admin_name,))
    if cur.fetchone():
        print(f"User {args.admin_name} already exists")
        return
    from argon2 import PasswordHasher

    ph = PasswordHasher()
    phash = ph.hash(args.admin_password)
    from .db import generate_virtual_key

    raw, h, prefix = generate_virtual_key()
    now = int(time.time())
    conn.execute(
        "INSERT INTO users(name, role, key_hash, key_prefix, enabled, created_at, password_hash) VALUES(?,?,?,?,?,?,?)",
        (args.admin_name, "admin", h, prefix, 1, now, phash),
    )
    conn.commit()
    print(f"Admin {args.admin_name} created. Virtual key: {raw} (prefix {prefix}) — store once!")
    print(f"DB: {cfg.db.path}")
    print("Next: run `claude-proxy login` to link your Pro Max subscription (OAuth).")


def cmd_user_add(args):
    cfg = Config.load(args.config)
    conn = _get_conn(cfg)
    uid, raw = create_user(conn, args.name, role="user")
    conn.commit()
    print(f"User {args.name} id={uid} key={raw}")


def cmd_user_list(args):
    cfg = Config.load(args.config)
    conn = _get_conn(cfg)
    rows = conn.execute("SELECT id, name, role, key_prefix, enabled, created_at FROM users ORDER BY id").fetchall()
    for r in rows:
        print(f"{r['id']:3} {r['name']:15} {r['role']:6} {r['key_prefix']:12} enabled={r['enabled']}")


def cmd_login(args):
    """Own grant PKCE: print URL, admin pastes code, exchange and store encrypted tokens (spec 5.1)."""
    cfg = Config.load(args.config)
    conn = _get_conn(cfg)
    verifier, challenge, state = generate_pkce()
    url = build_authorize_url(cfg, challenge, state)
    print("1. Open this URL in your browser (logged into claude.ai):\n")
    print(f"   {url}\n")
    print("2. After login you will be redirected to:")
    print(f"   {cfg.credential.redirect_uri}?code=...&state={state}")
    print("   Copy the `code` value from the URL.\n")
    code = input("Paste code: ").strip()
    if not code:
        # also accept full redirect URL
        print("No code pasted.")
        sys.exit(1)
    # If user pasted full URL, extract code
    if "code=" in code:
        from urllib.parse import urlparse, parse_qs

        qs = parse_qs(urlparse(code).query)
        code = qs.get("code", [code])[0]

    # Exchange
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.credential.redirect_uri,
        "client_id": cfg.credential.client_id,
        "code_verifier": verifier,
        "state": state,
    }
    print(f"\nExchanging code at {cfg.credential.token_url} ...")
    try:
        resp = httpx.post(cfg.credential.token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
    except Exception as e:
        print(f"Token request failed: {e}")
        sys.exit(1)
    if resp.status_code != 200:
        print(f"Token exchange failed: {resp.status_code} {resp.text}")
        sys.exit(1)
    tok = resp.json()
    expires_at = int(time.time()) + int(tok.get("expires_in", 28800))
    blob_data = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": expires_at,
        "scope": tok.get("scope"),
        "raw": tok,
    }
    blob = encrypt_blob(blob_data)
    conn.execute(
        "INSERT OR REPLACE INTO credentials(backend, encrypted_blob, expires_at, updated_at, state) VALUES('oauth',?,?,?,?)",
        (blob, expires_at, int(time.time()), "active"),
    )
    conn.commit()
    print(f"\n✓ OAuth linked. Access token expires at {time.ctime(expires_at)} (in {tok.get('expires_in')}s).")
    print("Refresh token stored encrypted — this gateway is now the sole refresher (spec 5.1).")


def cmd_limit_set(args):
    cfg = Config.load(args.config)
    conn = _get_conn(cfg)
    u = conn.execute("SELECT id FROM users WHERE name=?", (args.user,)).fetchone()
    if not u:
        print(f"User {args.user} not found")
        sys.exit(1)
    uid = u["id"]
    conn.execute(
        "INSERT OR REPLACE INTO limits(user_id, kind, value, unit, updated_at) VALUES(?,?,?,?,?)",
        (uid, args.kind, str(args.value), args.unit, int(time.time())),
    )
    conn.execute("INSERT INTO audit_log(at, action, target, detail_json) VALUES(?,?,?,?)", (int(time.time()), "limit_set", f"{args.user}:{args.kind}", f'{{"value":"{args.value}","unit":"{args.unit}"}}'))
    conn.commit()
    print(f"Set {args.user} {args.kind}={args.value} ({args.unit})")


def cmd_limit_clear(args):
    cfg = Config.load(args.config)
    conn = _get_conn(cfg)
    u = conn.execute("SELECT id FROM users WHERE name=?", (args.user,)).fetchone()
    if not u:
        print(f"User {args.user} not found")
        sys.exit(1)
    conn.execute("DELETE FROM limits WHERE user_id=? AND kind=?", (u["id"], args.kind))
    conn.commit()
    print(f"Cleared {args.user} {args.kind}")


def cmd_limit_list(args):
    cfg = Config.load(args.config)
    conn = _get_conn(cfg)
    q = args.user
    if q:
        u = conn.execute("SELECT id FROM users WHERE name=?", (q,)).fetchone()
        if not u:
            print(f"User {q} not found")
            sys.exit(1)
        rows = conn.execute("SELECT kind, value, unit, updated_at FROM limits WHERE user_id=? ORDER BY kind", (u["id"],)).fetchall()
        print(f"Limits for {q} (user_id={u['id']}):")
    else:
        rows = conn.execute("SELECT u.name, l.kind, l.value, l.unit FROM limits l JOIN users u ON u.id=l.user_id ORDER BY u.name, l.kind").fetchall()
        for r in rows:
            print(f"{r['name']:15} {r['kind']:20} {r['value']:15} {r['unit']}")
        return
    for r in rows:
        print(f"  {r['kind']:20} {r['value']:15} {r['unit']:10} updated={time.ctime(r['updated_at'])}")
    if not rows:
        print("  (no limits)")


def cmd_user_rename(args):
    cfg = Config.load(args.config)
    conn = _get_conn(cfg)
    u = conn.execute("SELECT id FROM users WHERE name=?", (args.old,)).fetchone()
    if not u:
        print(f"User {args.old} not found")
        sys.exit(1)
    if conn.execute("SELECT id FROM users WHERE name=?", (args.new,)).fetchone():
        print(f"User {args.new} already exists")
        sys.exit(1)
    conn.execute("UPDATE users SET name=? WHERE id=?", (args.new, u["id"]))
    conn.commit()
    print(f"Renamed {args.old} -> {args.new}")


def cmd_status(args):
    cfg = Config.load(args.config)
    from .credentials import OAuthBackend

    be = OAuthBackend(cfg, cfg.db.path)
    st = be.describe()
    print(f"backend={st.backend} healthy={st.healthy} detail={st.detail}")
    conn = _get_conn(cfg)
    row = conn.execute("SELECT expires_at, state, updated_at FROM credentials WHERE backend='oauth'").fetchone()
    if row:
        print(f"expires_at={row['expires_at']} ({time.ctime(row['expires_at']) if row['expires_at'] else 'unknown'}) state={row['state']}")


def cmd_serve(args):
    import uvicorn

    cfg = Config.load(args.config)
    from .app import create_app

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.listener.host, port=cfg.listener.port, log_level="info")


def main():
    p = argparse.ArgumentParser(prog="claude-proxy")
    p.add_argument("--config", default=None, help="TOML config file")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_init = sub.add_parser("init")
    s_init.add_argument("--admin-name", default="admin")
    s_init.add_argument("--admin-password", required=True)
    s_init.set_defaults(func=cmd_init)

    s_login = sub.add_parser("login", help="Link Pro Max subscription via OAuth PKCE")
    s_login.set_defaults(func=cmd_login)

    s_status = sub.add_parser("status", help="Show credential health")
    s_status.set_defaults(func=cmd_status)

    s_add = sub.add_parser("user")
    s_add_sub = s_add.add_subparsers(dest="sub", required=True)
    s_a = s_add_sub.add_parser("add")
    s_a.add_argument("name")
    s_a.set_defaults(func=cmd_user_add)
    s_l = s_add_sub.add_parser("list")
    s_l.set_defaults(func=cmd_user_list)
    s_r = s_add_sub.add_parser("rename")
    s_r.add_argument("old")
    s_r.add_argument("new")
    s_r.set_defaults(func=cmd_user_rename)

    s_lim = sub.add_parser("limit")
    s_lim_sub = s_lim.add_subparsers(dest="sub", required=True)
    s_ls = s_lim_sub.add_parser("set")
    s_ls.add_argument("--user", required=True)
    s_ls.add_argument("--kind", required=True, choices=["tokens_5h","tokens_daily","tokens_weekly","requests_daily","share_5h","share_7d","allowed_models","enabled"])
    s_ls.add_argument("--value", required=True, help="e.g. 100000 or 'claude-sonnet-4-*,claude-haiku-*' or 0/1 for enabled")
    s_ls.add_argument("--unit", default="weighted", choices=["raw","weighted","pct","count","list"])
    s_ls.set_defaults(func=cmd_limit_set)
    s_lc = s_lim_sub.add_parser("clear")
    s_lc.add_argument("--user", required=True)
    s_lc.add_argument("--kind", required=True)
    s_lc.set_defaults(func=cmd_limit_clear)
    s_ll = s_lim_sub.add_parser("list")
    s_ll.add_argument("--user", default=None, help="filter by user name")
    s_ll.set_defaults(func=cmd_limit_list)

    s_serve = sub.add_parser("serve")
    s_serve.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
