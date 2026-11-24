"""Serving from several subscription accounts (2026-11-24 spec §4, §6): one backend and poller per account; a request
goes to its serving pass's account, else to the user's assigned one, else is refused; never to another account."""
import time

import pytest
from fastapi.responses import JSONResponse

from claude_proxy import cli, db as dbm, limits, login, quota, routing
from claude_proxy.app import create_app
from tests.conftest import asgi_client, make_gateway, message_json, mounted_client, seed_oauth
from tests.test_web import bearer

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
DAY = 86400


def fresh(conn, account, now):
    """Fresh 5-hour and weekly figures for `account`, so its passes are measured."""
    for bucket, resets in (("5h", now + 3600), ("7d", now + 3 * DAY)):
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at, account) "
                     "VALUES(?,?,?,?,?,?)", (now - 60, "header", bucket, 0.0, resets, account))


def add_pass(conn, uid, account, starts_at, days=7):
    """A Standard week pass of `uid` held on `account`."""
    return conn.execute(
        "INSERT INTO tickets(user_id, user_name, tier, share_pct, length, days, starts_at, ends_at, list_usd, usd, currency, "
        "rate, amount, granted_at, account) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, "u", "standard", 10.0, "week", days, int(starts_at), int(starts_at + days * DAY), 1, 1, "USD", 1, 1,
         int(starts_at), account)).lastrowid


def assign(conn, uid, account):
    conn.execute("UPDATE users SET account=? WHERE id=?", (account, uid))


def last_row(conn):
    return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()


def tokens_seen(anthropic):
    return [c["headers"].get("authorization") for c in anthropic.calls]


@pytest.fixture
def two(cfg, db, anthropic):
    """Accounts `a` and `b` linked with fresh figures, tickets on, alice created; the gateway built over both."""
    conn = db[1]
    cfg.tickets.enabled = True
    seed_oauth(conn, access="tok-a", account="a")
    seed_oauth(conn, access="tok-b", account="b")
    now = time.time()
    fresh(conn, "a", now)
    fresh(conn, "b", now)
    alice, key = dbm.create_user(conn, "alice")
    gw = make_gateway(cfg, conn, anthropic)
    return gw, conn, alice, key


async def post(gw, key, path="/v1/messages", json=MSG, method="POST"):
    async with asgi_client(create_app(gw)) as c:
        if method == "GET":
            return await c.get(path, headers=bearer(key))
        return await c.post(path, json=json, headers=bearer(key))


async def test_pass_account_serves(two, anthropic):
    gw, conn, alice, key = two
    assign(conn, alice, "a")
    tid = add_pass(conn, alice, "b", time.time() - 3600)
    before = conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0]
    anthropic.queue.append(lambda req: JSONResponse(message_json(), headers={
        "anthropic-ratelimit-unified-5h-utilization": "0.10", "anthropic-ratelimit-unified-5h-reset": str(int(time.time()) + 3600)}))
    r = await post(gw, key)
    assert r.status_code == 200, r.text
    assert tokens_seen(anthropic) == ["Bearer tok-b"]
    row = last_row(conn)
    assert row["account"] == "b" and row["ticket_id"] == tid
    new = conn.execute("SELECT account FROM quota_snapshots ORDER BY id DESC LIMIT ?",
                       (conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0] - before,)).fetchall()
    assert new and {r[0] for r in new} == {"b"}


async def test_no_pass_uses_assigned(two, anthropic):
    gw, conn, alice, key = two
    assign(conn, alice, "a")
    r = await post(gw, key)
    assert r.status_code == 200, r.text
    assert tokens_seen(anthropic) == ["Bearer tok-a"]
    assert last_row(conn)["account"] == "a"


async def test_unassigned_refused(two, anthropic):
    gw, conn, alice, key = two
    r = await post(gw, key)
    assert r.status_code == 403
    assert r.json()["error"] == {"type": "permission_error", "message": "No subscription is assigned to you; ask the admin."}
    row = last_row(conn)
    assert row["rejected_by"] == "no_account" and row["account"] is None
    assert anthropic.calls == []


async def test_serving_pass_switch_moves_account(two, anthropic, monkeypatch):
    gw, conn, alice, key = two
    now = time.time()
    on_a = add_pass(conn, alice, "a", now - 7200)
    on_b = add_pass(conn, alice, "b", now - 3600)
    real = limits.pass_states

    def states(conn_, cfg_, t, now_):
        if t["id"] == on_a:   # the oldest pass is at its 5-hour limit: requests spill to the next one
            return [limits.LimitState("share_5h", "*", "10", "pct", exceeded=True, resets_at=now_ + 600)]
        return real(conn_, cfg_, t, now_)
    monkeypatch.setattr(limits, "pass_states", states)
    att = quota.attribution(conn, gw.cfg.pricing, "5h", now=now, account="a")
    r = await post(gw, key)
    assert r.status_code == 200, r.text
    assert tokens_seen(anthropic) == ["Bearer tok-b"]
    assert last_row(conn)["ticket_id"] == on_b and last_row(conn)["account"] == "b"
    assert quota.attribution(conn, gw.cfg.pricing, "5h", now=now, account="a") == att


async def test_count_tokens_and_models_follow_first_pass(two, anthropic):
    gw, conn, alice, key = two
    assign(conn, alice, "a")
    add_pass(conn, alice, "b", time.time() - 3600)
    anthropic.default = lambda req: JSONResponse({"input_tokens": 3} if req["path"].endswith("count_tokens")
                                                 else {"data": [], "has_more": False, "first_id": None, "last_id": None})
    assert (await post(gw, key, "/v1/messages/count_tokens")).status_code == 200
    assert last_row(conn)["account"] == "b"
    assert (await post(gw, key, "/v1/models", method="GET")).status_code == 200
    assert last_row(conn)["account"] == "b"
    assert tokens_seen(anthropic) == ["Bearer tok-b", "Bearer tok-b"]


async def test_unhealthy_account_never_fails_over(two, anthropic):
    gw, conn, alice, key = two
    conn.execute("UPDATE credentials SET state='needs_login' WHERE account='a'")
    add_pass(conn, alice, "a", time.time() - 3600)
    r = await post(gw, key)
    assert r.status_code == 503
    assert "Bearer tok-b" not in tokens_seen(anthropic)
    assert last_row(conn)["account"] == "a"
    bob, bob_key = dbm.create_user(conn, "bob")
    assign(conn, bob, "b")
    assert (await post(gw, bob_key)).status_code == 200
    async with asgi_client(create_app(gw)) as c:
        assert (await c.get("/health")).json()["credential"] is False


async def test_third_party_route_untouched(two, anthropic, meta, monkeypatch):
    gw, conn, alice, key = two   # alice has neither a pass nor an account: routes need neither
    gw.cfg.routes[0].base_url = "http://meta.fake"
    monkeypatch.setenv(gw.cfg.routes[0].api_key_env, "meta-key")
    gw.http = mounted_client(anthropic, meta)
    r = await post(gw, key, json={**MSG, "model": gw.cfg.routes[0].models[0].rstrip("*")})
    assert r.status_code == 200, r.text
    assert last_row(conn)["account"] is None and last_row(conn)["provider"] == gw.cfg.routes[0].name
    assert anthropic.calls == [] and not any("tok-" in (c["headers"].get("authorization") or "") for c in meta.calls)


async def test_auth_refused_has_no_account(two):
    gw, conn, alice, key = two
    r = await post(gw, "sk-proxy-nope")
    assert r.status_code == 401
    assert last_row(conn)["rejected_by"] == "auth" and last_row(conn)["account"] is None


def test_sync_accounts_live(cfg, db):
    conn = db[1]
    seed_oauth(conn, access="tok-a", account="a")
    gw = make_gateway(cfg, conn)
    a = gw.backends["a"]
    assert set(gw.backends) == set(gw.pollers) == {"a"}
    seed_oauth(conn, access="tok-c", account="c")
    assert gw.sync_accounts() == (["c"], [])
    assert gw.pollers["c"].account == "c" and gw.backends["c"].account == "c"
    conn.execute("DELETE FROM credentials WHERE account='c'")
    assert gw.sync_accounts() == ([], ["c"])
    assert set(gw.backends) == set(gw.pollers) == {"a"}
    assert gw.backends["a"] is a


def test_remove_refused_with_passes_or_users(cfg, db):
    conn = db[1]
    seed_oauth(conn, account="default")
    seed_oauth(conn, account="a", access="x", sub="s-a")
    seed_oauth(conn, account="b", access="y", sub="s-b")
    uid, _ = dbm.create_user(conn, "alice")
    conn.execute("UPDATE users SET account='a'")
    tid = add_pass(conn, uid, "b", time.time() + DAY)   # queued
    with pytest.raises(login.LoginError, match="1 running or queued passes"):
        login.remove_account(conn, "b")
    conn.execute("UPDATE tickets SET cancelled_at=? WHERE id=?", (int(time.time()), tid))
    assign(conn, uid, "b")
    with pytest.raises(login.LoginError, match="1 users are assigned to it"):
        login.remove_account(conn, "b")
    assign(conn, uid, "a")
    login.remove_account(conn, "b")
    login.remove_account(conn, "default")
    assert [r["account"] for r in conn.execute("SELECT account FROM credentials")] == ["a"]
    with pytest.raises(login.LoginError, match="last linked"):
        login.remove_account(conn, "a")


def test_status_lists_every_account(cfg, db, capsys):
    conn = db[1]
    seed_oauth(conn, account="a", access="x", sub="s-a")
    seed_oauth(conn, account="b", access="y", sub="s-b")
    conn.execute("UPDATE credentials SET state='needs_login' WHERE account='b'")
    cli.cmd_status(None, cfg)
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("credential [a]: OK") for line in lines)
    assert any(line.startswith("credential [b]: NOT OK") for line in lines)
    assert "credential: OK" not in lines and "credential: NOT OK" in lines
    conn.execute("UPDATE credentials SET state='active'")
    cli.cmd_status(None, cfg)
    assert "credential: OK" in capsys.readouterr().out.splitlines()
    conn.execute("DELETE FROM credentials")
    cli.cmd_status(None, cfg)
    assert "credential: NOT OK" in capsys.readouterr().out.splitlines()


def test_create_user_single_account(db):
    conn = db[1]
    assert routing.assigned(conn, dbm.create_user(conn, "none")[0]) is None
    seed_oauth(conn, account="a")
    assert routing.assigned(conn, dbm.create_user(conn, "one")[0]) == "a"
    assert routing.assigned(conn, dbm.create_user(conn, "given", account="zz")[0]) == "zz"
    seed_oauth(conn, account="b", sub="s-b")
    assert routing.assigned(conn, dbm.create_user(conn, "two")[0]) is None


async def test_run_pollers_ticks_every_account_and_picks_up_logins(cfg, db, monkeypatch):
    import asyncio
    from claude_proxy.quota import Poller
    conn = db[1]
    seed_oauth(conn, account="a")
    gw = make_gateway(cfg, conn)
    ticked = []

    async def tick(self, now=None):
        ticked.append(self.account)
        if self.account == "a":
            raise RuntimeError("boom")   # logged; never stops another account's poll
    monkeypatch.setattr(Poller, "tick", tick)
    seed_oauth(conn, account="c", sub="s-c")   # a `claude-proxy login` from another process
    task = asyncio.create_task(gw.run_pollers(interval_s=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    assert {"a", "c"} <= set(ticked) and ticked.count("c") >= 2
