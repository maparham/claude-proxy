"""The admin dashboard's APIs per subscription account (multi-subscription design 2026-11-24, §7): token views,
quota and capacity take ?account=, users are assigned an account, the Users table counts shares in Claude Pro weeks
over every account, passes move between accounts, and nothing a user sees names an account or its email.

`alpha` is on Claude Max 20x and `bravo` on Claude Pro. Anthropic's reports and the observed rates are stubbed per
account where passes are sold, as the measuring itself is tested elsewhere."""
import json
import time

import pytest
from argon2 import PasswordHasher

from claude_proxy import db as dbm, quota, tickets
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client, make_gateway, seed_oauth
from tests.test_web import PW, admin_client, bearer
from tests.tickets_helpers import DAY, link, tcfg, user

A, B = "alpha", "bravo"
EMAILS = {A: "alpha-owner@subs.test", B: "bravo-owner@subs.test"}
RATES = {A: 20000.0, B: 1000.0}   # weighted tokens per point: an alpha point is 20 bravo points


@pytest.fixture
def env(db, cfg):
    conn = db[1]
    for a in (A, B):
        seed_oauth(conn, account=a, sub=f"sub-{a}", email=EMAILS[a])
        conn.execute("INSERT INTO settings(key, value) VALUES(?, '4102444800')", (f"subscription_ends_at:{a}",))
    admin, _ = dbm.create_user(conn, "admin", role="admin", password_hash=PasswordHasher().hash(PW), account=A)
    alice, alice_key = dbm.create_user(conn, "alice", account=A)
    gw = make_gateway(cfg, conn)
    return gw, conn, cfg, {"admin": admin, "alice": alice}, alice_key


def req(conn, uid, account, tokens, t=None, rejected_by=None):
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status, input_tokens, "
                 "output_tokens, rejected_by, account) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (uid, (t or time.time()) - 2, (t or time.time()) - 1, "POST", "/v1/messages", "anthropic", "claude-sonnet-5",
                  403 if rejected_by else 200, tokens, 0, rejected_by, account))
    return conn.execute("SELECT MAX(id) FROM requests").fetchone()[0]


async def test_views_filter_by_account(env):
    gw, conn, cfg, ids, _ = env
    on_a = req(conn, ids["alice"], A, 100)
    on_b = req(conn, ids["alice"], B, 7)
    refused = req(conn, ids["alice"], None, 0, rejected_by="auth")   # refused before routing: under every account
    async with admin_client(gw) as c:
        rows = (await c.get(f"/api/requests?account={A}")).json()["requests"]
        assert {r["id"] for r in rows} == {on_a, refused}
        assert {r["id"]: r["account"] for r in rows} == {on_a: A, refused: None}
        assert (await c.get(f"/api/overview?account={B}")).json()["totals"]["24h"]["input"] == 7
        assert {r["id"] for r in (await c.get("/api/requests")).json()["requests"]} == {on_a, refused}   # the first linked
        assert (await c.get("/api/overview")).json()["totals"]["24h"]["input"] == 100
        r = await c.get(f"/api/series?account={B}&range=1d&granularity=hour&split=none")
        assert sum(p["input"] for p in r.json()["points"]) == 7
        r = await c.get(f"/api/models?account={B}")
        assert [m["input"] for m in r.json()["models"]] == [7]
        for path in ("/api/requests", "/api/overview", "/api/series", "/api/models", "/api/heatmap", "/api/sessions",
                     "/api/errors"):
            r = await c.get(f"{path}?account=zz")
            assert r.status_code == 404 and r.json()["error"] == "No subscription account 'zz'.", path
    # A user's own views: always all of theirs, whatever account is asked for, and no account named.
    async with asgi_client(create_dashboard_app(gw)) as c:
        alice = bearer(env[4])
        rows = (await c.get(f"/api/requests?account={B}", headers=alice)).json()["requests"]
        assert {r["id"] for r in rows} == {on_a, on_b, refused} and all("account" not in r for r in rows)
        assert (await c.get("/api/overview?account=zz", headers=alice)).json()["totals"]["24h"]["input"] == 107


async def test_quota_timeline_and_capacity_per_account(env):
    gw, conn, cfg, ids, _ = env
    cfg.tickets = tcfg().tickets
    now = time.time()
    for a, util in ((A, 11.0), (B, 42.0)):
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at, account) "
                     "VALUES(?,?,?,?,?,?)", (now - 60, "header", "7d", util, now + 3 * DAY, a))
    for a, share in ((A, 3.0), (B, 20.0), (B, 5.0)):
        conn.execute("INSERT INTO tickets(user_id, user_name, tier, share_pct, share_5h_pct, length, days, starts_at, ends_at, "
                     "list_usd, usd, currency, rate, amount, granted_at, account) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ids["alice"], "alice", "standard", share, share, "week", 7, now - DAY, now + 6 * DAY, 1, 1, "USD", 1, 1,
                      now - DAY, a))
    async with admin_client(gw) as c:
        snaps = (await c.get(f"/api/quota/timeline?bucket=7d&account={B}")).json()["snapshots"]
        assert [s["utilization_pct"] for s in snaps] == [42.0]
        assert (await c.get(f"/api/admin/capacity?account={B}")).json()["five_hour"]["now"] == 25.0
        assert (await c.get(f"/api/admin/capacity?account={A}")).json()["five_hour"]["now"] == 3.0
        listed = (await c.get(f"/api/admin/tickets?account={B}")).json()["tickets"]
        assert sorted(t["share_pct"] for t in listed) == [5.0, 20.0] and {t["account"] for t in listed} == {B}


async def test_assign_user_account(env):
    gw, conn, cfg, ids, _ = env
    async with admin_client(gw) as c:
        r = await c.put(f"/api/admin/users/{ids['alice']}/account", json={"account": B})
        assert r.status_code == 200, r.text
        assert conn.execute("SELECT account FROM users WHERE id=?", (ids["alice"],)).fetchone()[0] == B
        audit = conn.execute("SELECT target, detail_json FROM audit_log WHERE action='user_account_set' ORDER BY id DESC").fetchone()
        assert audit[0] == "alice" and json.loads(audit[1]) == {"account": B, "from": A}
        assert {u["name"]: u["account"] for u in (await c.get("/api/users")).json()["users"]}["alice"] == B
        r = await c.put(f"/api/admin/users/{ids['alice']}/account", json={"account": "ghost"})
        assert r.status_code == 400 and "ghost" in r.json()["error"]
        assert (await c.put(f"/api/admin/users/{ids['alice']}/account", json={})).status_code == 400
        assert conn.execute("SELECT account FROM users WHERE id=?", (ids["alice"],)).fetchone()[0] == B
        del c.headers["x-csrf-token"]
        assert (await c.put(f"/api/admin/users/{ids['alice']}/account", json={"account": A})).status_code == 403
    async with admin_client(gw) as c:
        assert (await c.post(f"/api/admin/users/{ids['alice']}/disable")).status_code == 200
    assert conn.execute("SELECT enabled FROM users WHERE id=?", (ids["alice"],)).fetchone()[0] == 0
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.put(f"/api/admin/users/{ids['alice']}/account", json={"account": A},
                            headers=bearer(env[4]))).status_code in (401, 403)


async def test_users_share_in_pro_weeks(env, monkeypatch):
    gw, conn, cfg, ids, _ = env
    link(conn, A, "max_20x")
    link(conn, B, "pro")
    shares = {A: 10.0, B: 30.0}

    def fake(conn_, pricing, bucket, now, stale_after_s=1800, *, account):
        return {"bucket": bucket, "utilization_pct": 40.0, "resets_at": now + 3600, "observed_at": now - 60, "stale": False,
                "unattributed": 0.0, "history": [], "shares": {ids["alice"]: shares[account]}}

    monkeypatch.setattr(quota, "attribution", fake)
    async with admin_client(gw) as c:
        alice = next(u for u in (await c.get("/api/users")).json()["users"] if u["name"] == "alice")
        assert alice["share"]["7d"] == pytest.approx(2.3) and alice["account"] == A
        admin = next(u for u in (await c.get("/api/users")).json()["users"] if u["name"] == "admin")
        assert admin["share"]["7d"] == 0.0
        monkeypatch.setattr(quota, "attribution", lambda *a, **k: fake(*a, **k) | {"stale": True})
        alice = next(u for u in (await c.get("/api/users")).json()["users"] if u["name"] == "alice")
        assert alice["share"]["7d"] is None   # every account stale


@pytest.fixture
def selling(env, monkeypatch):
    """Passes on sale: alpha on Max 20x, bravo on Pro, both with a fresh weekly report, prices and sizes seeded."""
    gw, conn, cfg, ids, alice_key = env
    cfg.tickets = tcfg().tickets
    now = time.time()
    link(conn, A, "max_20x")
    link(conn, B, "pro")
    reset = now + 3 * DAY
    for a in (A, B):
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at, account) "
                     "VALUES(?,?,?,?,?,?)", (now - 60, "header", "7d", 0.0, reset, a))
    tickets.seed_prices(conn, cfg, now=now - DAY)
    tickets.seed_sizes(conn, cfg, now=now - DAY)

    def fake_att(conn_, pricing, bucket, now, stale_after_s=1800, *, account):
        out = {"bucket": bucket, "utilization_pct": 0.0, "observed_at": now - 60, "stale": False, "shares": {},
               "ticket_shares": {}, "unattributed": 0.0, "history": [],
               "resets_at": reset if bucket == "7d" else now + 3600,
               "window_start": reset - 7 * DAY + 60 if bucket == "7d" else now - 3600}
        return out | {"counted_from": out["window_start"]}

    monkeypatch.setattr(quota, "attribution_cached", fake_att)
    monkeypatch.setattr(quota, "observed_rate", lambda conn_, pricing, bucket, now, days=7, *, account: RATES.get(account))
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "standard", "week", "USD", terms_version="v1",
                      now=now)
    assert t["account"] == A
    return gw, conn, cfg, ids, alice_key, t


async def test_move_api(selling):
    gw, conn, cfg, ids, _, t = selling
    async with admin_client(gw) as c:
        r = await c.post(f"/api/admin/tickets/{t['id']}/move", json={"account": B, "dry_run": True})
        assert r.status_code == 200, r.text
        assert r.json()["to"] == B and r.json()["dry_run"] is True
        assert tickets.get(conn, t["id"])["account"] == A
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='ticket_move'").fetchone()[0] == 0
        r = await c.post(f"/api/admin/tickets/{t['id']}/move", json={"account": B})
        assert r.status_code == 200 and r.json()["to"] == B and r.json()["ticket"]["account"] == B
        assert tickets.get(conn, t["id"])["account"] == B
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='ticket_move'").fetchone()[0] == 1
        r = await c.post(f"/api/admin/tickets/{t['id']}/move", json={"account": B})
        assert r.status_code == 400 and "already" in r.json()["error"]
        assert (await c.post(f"/api/admin/tickets/{t['id']}/move", json={})).status_code == 400
        del c.headers["x-csrf-token"]
        assert (await c.post(f"/api/admin/tickets/{t['id']}/move", json={"account": A})).status_code == 403


async def test_preview_names_the_account_and_the_others(selling):
    gw, conn, cfg, ids, _, t = selling
    async with admin_client(gw) as c:
        p = (await c.post("/api/admin/tickets/preview", json={"user": "alice", "tier": "standard", "length": "week"})).json()
        assert p["account"] == A and {r["account"] for r in p["accounts"]} == {A, B}
        p = (await c.post("/api/admin/tickets/preview", json={"user": "alice", "tier": "standard", "length": "week",
                                                               "account": B})).json()
        assert p["account"] == B


async def test_user_views_never_name_accounts(selling):
    gw, conn, cfg, ids, alice_key, t = selling
    async with asgi_client(create_dashboard_app(gw)) as c:
        for path in ("/api/me/status", f"/api/me/status?account={A}", "/api/me/tickets", "/api/pricing"):
            r = await c.get(path, headers=bearer(alice_key))
            assert r.status_code == 200, (path, r.text)
            body = r.text
            for secret in (A, B, *EMAILS.values()):
                assert secret not in body, (path, secret)


async def test_session_lists_accounts_and_admin_status(selling):
    gw, conn, cfg, ids, _, t = selling
    async with admin_client(gw) as c:
        s = (await c.get("/api/session")).json()
        assert [(a["account"], a["plan"], a["sells"]) for a in s["accounts"]] == [(A, "max_20x", None), (B, "pro", None)]
        assert set(s["accounts"][0]) == {"account", "label", "plan", "healthy", "sells"}
        assert s["credential"]["healthy"] is True and set(s["credential"]["accounts"]) == {A, B}
        assert (await c.get("/api/session?account=zz")).status_code == 404
        st = (await c.get("/api/me/status")).json()
        assert set(st["account"]) >= {"5h", "7d"} and st["credential_healthy"] is True
        assert A not in json.dumps(st["account"])
