"""Sign-up with Clerk, browser authorization for `claude-gateway on`, machine keys and the one-time credit."""
import base64
import json
import time

import httpx
import pytest
from argon2 import PasswordHasher
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from claude_proxy import clerk, limits
from claude_proxy.auth import AuthError, authenticate
from claude_proxy.db import create_user
from claude_proxy.gateway import Gateway
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client

PW = "correct horse battery"
FAPI = "clerk.test.dev"
PK = "pk_test_" + base64.b64encode(f"{FAPI}$".encode()).decode().rstrip("=")
DASH = "https://dash.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt(claims: dict, kid="k1", key=KEY) -> str:
    head = b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    body = b64(json.dumps(claims).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(sig)}"


def claims(sub="user_1", **kw) -> dict:
    now = int(time.time())
    return {"sub": sub, "sid": "sess_1", "iss": f"https://{FAPI}", "azp": DASH, "exp": now + 60, "nbf": now - 5, "iat": now - 5, **kw}


class Clerk:
    """Clerk's JWKS and Backend API, as far as the gateway uses them."""

    def __init__(self):
        n = KEY.public_key().public_numbers()
        self.jwks = {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
                               "n": b64(n.n.to_bytes(256, "big")), "e": b64(n.e.to_bytes(3, "big"))}]}
        self.users = {"user_1": ("new@example.com", "verified")}
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if request.url.host == FAPI and request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json=self.jwks)
        if request.url.host == "api.clerk.com" and request.url.path.startswith("/v1/users/"):
            assert request.headers["authorization"] == "Bearer sk_test_secret"
            uid = request.url.path.rsplit("/", 1)[1]
            if uid not in self.users:
                return httpx.Response(404, json={})
            email, status = self.users[uid]
            return httpx.Response(200, json={"id": uid, "primary_email_address_id": "e1", "email_addresses": [
                {"id": "e0", "email_address": "other@example.com", "verification": {"status": "verified"}},
                {"id": "e1", "email_address": email, "verification": {"status": status}}]})
        return httpx.Response(404)


@pytest.fixture
def env(cfg, db, monkeypatch):
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_secret")
    cfg.listener.public_url, cfg.listener.dashboard_url = "https://gw.test", DASH
    cfg.signup.clerk_publishable_key, cfg.signup.enabled = PK, True
    conn = db[1]
    create_user(conn, "admin", role="admin", password_hash=PasswordHasher().hash(PW))
    fake = Clerk()
    gw = Gateway(cfg, conn, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return gw, conn, fake


def app(gw):
    return asgi_client(create_dashboard_app(gw))


async def signed_in(c, sub="user_1"):
    r = await c.post("/api/login/clerk", json={"token": jwt(claims(sub))})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = r.json()["csrf"]
    return r.json()["user"]


def test_frontend_api_comes_from_the_publishable_key():
    assert clerk.frontend_api(PK) == FAPI
    assert clerk.frontend_api("pk_live_" + base64.b64encode(b"clerk.rahkar.pro$").decode()) == "clerk.rahkar.pro"
    assert clerk.frontend_api("sk_live_abc") is None
    assert clerk.frontend_api("pk_live_!!!") is None


@pytest.mark.parametrize("bad, why", [
    ({"exp": int(time.time()) - 60}, "expired"),
    ({"nbf": int(time.time()) + 120}, "not valid yet"),
    ({"iss": "https://clerk.other.dev"}, "another Clerk instance"),
    ({"azp": "https://evil.test"}, "another site"),
    ({"azp": None}, "another site"),
    ({"azp": ["x"]}, "another site"),
    ({"sub": "org_1"}, "not a user's session token"),
    ({"sid": None}, "not a user's session token"),   # e.g. another kind of token the instance signs
])
async def test_clerk_tokens_are_checked(env, bad, why):
    gw, *_ = env
    v = clerk.Verifier(PK, "sk_test_secret", DASH, gw.http)
    with pytest.raises(clerk.ClerkError, match=why):
        await v.verify(jwt(claims(**bad)))


async def test_clerk_token_signature_and_key_id(env):
    gw, _, fake = env
    v = clerk.Verifier(PK, "sk_test_secret", DASH, gw.http)
    assert (await v.verify(jwt(claims())))["sub"] == "user_1"
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(clerk.ClerkError, match="bad signature"):
        await v.verify(jwt(claims(), key=other))
    with pytest.raises(clerk.ClerkError, match="unknown key"):
        await v.verify(jwt(claims(), kid="k9"))
    with pytest.raises(clerk.ClerkError, match="not a JWT"):
        await v.verify("abc")
    head, body, _ = jwt(claims()).split(".")
    with pytest.raises(clerk.ClerkError, match="bad signature"):
        await v.verify(f"{head}.{body}.!!!")
    assert sum("jwks" in u for u in fake.calls) == 1   # cached; an unknown kid refetches at most every 30 s


async def test_sign_up_creates_an_account_with_the_one_time_credit(env):
    gw, conn, _ = env
    async with app(gw) as c:
        user = await signed_in(c)
        assert user["name"] == "new@example.com" and user["role"] == "user" and user["email"] == "new@example.com"
        st = (await c.get("/api/me/status?format=text")).text
    assert st.strip() == "new@example.com · credit $0.00/$5.00"
    row = conn.execute("SELECT kind, value, unit FROM limits WHERE user_id=?", (user["id"],)).fetchone()
    assert tuple(row) == ("cost_total", "5", "usd")
    assert conn.execute("SELECT action FROM audit_log WHERE action='signup'").fetchone()
    async with app(gw) as c:   # the next sign-in finds the same account by its Clerk id
        assert (await signed_in(c))["id"] == user["id"]
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


async def test_sign_in_links_an_existing_account_named_by_email(env):
    gw, conn, fake = env
    uid, _ = create_user(conn, "New@Example.com")
    fake.users["user_2"] = ("new@example.com", "verified")
    async with app(gw) as c:
        assert (await signed_in(c, "user_2"))["id"] == uid
    assert conn.execute("SELECT clerk_id FROM users WHERE id=?", (uid,)).fetchone()[0] == "user_2"
    assert not conn.execute("SELECT 1 FROM limits WHERE user_id=?", (uid,)).fetchone()   # no credit for an existing account


async def test_sign_in_refuses_removed_admin_unverified_and_closed(env):
    gw, conn, fake = env
    uid, _ = create_user(conn, "gone@example.com")
    conn.execute("UPDATE users SET revoked_at=1, enabled=0 WHERE id=?", (uid,))
    fake.users.update({"user_gone": ("gone@example.com", "verified"), "user_unv": ("x@example.com", "unverified"),
                       "user_admin": ("admin", "verified")})
    async with app(gw) as c:
        r = await c.post("/api/login/clerk", json={"token": jwt(claims("user_gone"))})
        assert r.status_code == 403 and "removed" in r.json()["error"]
        r = await c.post("/api/login/clerk", json={"token": jwt(claims("user_unv"))})
        assert r.status_code == 401 and "verified email" in r.json()["error"]
        r = await c.post("/api/login/clerk", json={"token": jwt(claims("user_admin"))})
        assert r.status_code == 409   # never the admin account, and no second user named "admin"
        gw.cfg.signup.enabled = False
        r = await c.post("/api/login/clerk", json={"token": jwt(claims("user_1"))})
        assert r.status_code == 403 and "closed" in r.json()["error"]


async def test_auth_config_and_csp(env):
    gw, *_ = env
    async with app(gw) as c:
        r = await c.get("/api/auth-config")
        assert r.json() == {"clerk": {"publishable_key": PK, "frontend_api": FAPI}, "signup": True}
        csp = r.headers["content-security-policy"]
    assert f"https://{FAPI}" in csp and "https://challenges.cloudflare.com" in csp
    gw.cfg.signup.clerk_publishable_key = ""
    async with app(gw) as c:
        r = await c.get("/api/auth-config")
    assert r.json() == {"clerk": None, "signup": False}
    assert "clerk" not in r.headers["content-security-policy"] and "frame-src 'none'" in r.headers["content-security-policy"]


async def test_install_script(env):
    gw, *_ = env
    async with app(gw) as c:
        s = (await c.get("/install")).text
        assert (await c.get("/api/session")).status_code == 401
        await signed_in(c)
        assert (await c.get("/api/session")).json()["install"] == f"curl -fsSL {DASH}/install | sh"
    assert s.startswith("#!/bin/sh\n")
    assert f"| sh -s -- on --url https://gw.test --dashboard {DASH} \"$@\"" in s
    gw.cfg.listener.public_url = ""
    async with app(gw) as c:
        assert (await c.get("/install")).status_code == 503


async def test_device_flow_gives_the_cli_a_key_of_its_own(env):
    gw, conn, _ = env
    async with app(gw) as cli, app(gw) as browser:
        start = (await cli.post("/api/device/start", json={"label": "  maya's\nlaptop "})).json()
        code, dc = start["user_code"], start["device_code"]
        assert start["verification_uri_complete"] == f"{DASH}/dashboard#authorize/{code}"
        assert start["interval"] == 3 and start["expires_in"] == 600
        assert len(code) == 9 and code[4] == "-" and not set(code) & set("AEIOUY0123456789")
        r = await cli.post("/api/device/token", json={"device_code": dc})
        assert r.status_code == 400 and r.json()["error"] == "authorization_pending"
        assert (await cli.post("/api/device/token", json={"device_code": dc})).json()["error"] == "slow_down"
        assert (await browser.get(f"/api/device/{code}")).status_code == 401   # signing in comes first
        user = await signed_in(browser)
        view = (await browser.get(f"/api/device/{code.lower().replace('-', '')}")).json()
        assert view["label"] == "maya's laptop" and view["user_code"] == code
        del browser.headers["x-csrf-token"]
        assert (await browser.post(f"/api/device/{code}/approve", json={})).status_code == 403
        browser.headers["x-csrf-token"] = (await browser.get("/api/session")).json()["csrf"]
        assert (await browser.post(f"/api/device/{code}/approve", json={})).status_code == 200
        assert (await browser.get(f"/api/device/{code}")).status_code == 404   # answered
        r = await cli.post("/api/device/token", json={"device_code": dc})
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["user"] == "new@example.com" and got["url"] == "https://gw.test" and got["dashboard"] == DASH
        assert (await cli.post("/api/device/token", json={"device_code": dc})).json()["error"] == "invalid_grant"
        keys = (await browser.get("/api/keys")).json()["keys"]
        assert [k["label"] for k in keys] == ["maya's laptop"] and got["key"].startswith(keys[0]["key_prefix"])
    assert authenticate(conn, {"authorization": f"Bearer {got['key']}"})["id"] == user["id"]
    assert conn.execute("SELECT COUNT(*) FROM device_requests").fetchone()[0] == 0
    assert got["key"] not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM keys")])


async def test_device_flow_denied_and_expired(env):
    gw, conn, _ = env
    async with app(gw) as cli, app(gw) as browser:
        await signed_in(browser)
        a = (await cli.post("/api/device/start", json={})).json()
        assert (await browser.get(f"/api/device/{a['user_code']}")).json()["label"] == "a computer"
        await browser.post(f"/api/device/{a['user_code']}/deny", json={})
        assert (await cli.post("/api/device/token", json={"device_code": a["device_code"]})).json()["error"] == "access_denied"
        b = (await cli.post("/api/device/start", json={})).json()
        conn.execute("UPDATE device_requests SET expires_at=?", (int(time.time()) - 1,))
        assert (await browser.post(f"/api/device/{b['user_code']}/approve", json={})).status_code == 404
        assert (await cli.post("/api/device/token", json={"device_code": b["device_code"]})).json()["error"] == "expired_token"
        assert (await cli.post("/api/device/token", json={"device_code": "nope"})).json()["error"] == "invalid_grant"
    assert not db_count(conn, "keys")


async def test_device_start_is_rate_limited(env):
    gw, *_ = env
    async with app(gw) as c:
        codes = [(await c.post("/api/device/start", json={})).status_code for _ in range(11)]
    assert codes == [200] * 10 + [429]


async def test_a_disabled_user_approval_yields_no_key(env):
    gw, conn, _ = env
    async with app(gw) as cli, app(gw) as browser:
        user = await signed_in(browser)
        s = (await cli.post("/api/device/start", json={})).json()
        await browser.post(f"/api/device/{s['user_code']}/approve", json={})
        conn.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
        assert (await cli.post("/api/device/token", json={"device_code": s["device_code"]})).json()["error"] == "access_denied"
    assert not db_count(conn, "keys")


async def test_machines_can_be_removed_by_their_owner_or_the_admin(env):
    gw, conn, _ = env
    from claude_proxy.db import add_machine_key
    async with app(gw) as c:
        user = await signed_in(c)
        k1, k2 = add_machine_key(conn, user["id"], "one"), add_machine_key(conn, user["id"], "two")
        other, _ = create_user(conn, "carol")
        k3 = add_machine_key(conn, other, "carol's")
        ids = {k["label"]: k["id"] for k in (await c.get("/api/keys")).json()["keys"]}
        assert set(ids) == {"one", "two"}
        carol_id = conn.execute("SELECT id FROM keys WHERE label=?", ("carol's",)).fetchone()[0]
        assert (await c.post(f"/api/keys/{carol_id}/remove", json={})).status_code == 404
        assert [k["label"] for k in (await c.get(f"/api/keys?user_id={other}")).json()["keys"]] == ["two", "one"]   # own only
        assert (await c.post(f"/api/keys/{ids['one']}/remove", json={})).json()["removed"] is True
    with pytest.raises(AuthError):
        authenticate(conn, {"authorization": f"Bearer {k1}"})
    assert authenticate(conn, {"authorization": f"Bearer {k2}"})["id"] == user["id"]
    async with app(gw) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": PW})
        c.headers["x-csrf-token"] = r.json()["csrf"]
        assert [k["label"] for k in (await c.get(f"/api/keys?user_id={other}")).json()["keys"]] == ["carol's"]
        assert (await c.post(f"/api/keys/{carol_id}/remove", json={})).json()["removed"] is True
    with pytest.raises(AuthError):
        authenticate(conn, {"authorization": f"Bearer {k3}"})


async def test_a_non_admin_sees_only_their_own_machines(env):
    gw, conn, _ = env
    from claude_proxy.db import add_machine_key
    other, _ = create_user(conn, "carol")
    add_machine_key(conn, other, "carol's")
    async with app(gw) as c:
        await signed_in(c)
        assert (await c.get(f"/api/keys?user_id={other}")).json()["keys"] == []


def db_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def spend(conn, uid, dollars):
    # claude-sonnet-5 output is $10 per million tokens
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status, input_tokens, output_tokens) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, time.time() - 10, time.time(), "POST", "/v1/messages",
                                                 "anthropic", "claude-sonnet-5", 200, 0, int(dollars * 100_000)))


async def test_credit_runs_out_and_upgrade_replaces_it(env):
    gw, conn, _ = env
    async with app(gw) as c:
        user = await signed_in(c)
    uid = user["id"]
    spend(conn, uid, 4.0)
    assert limits.evaluate(conn, gw.cfg, uid, "claude-sonnet-5", "/v1/messages") is None
    conn.execute("UPDATE requests SET started_at=started_at-40*86400")   # a credit has no window: old spending counts
    spend(conn, uid, 1.5)
    d = limits.evaluate(conn, gw.cfg, uid, "claude-sonnet-5", "/v1/messages")
    assert d.status == 403 and d.kind == "cost_total"   # it never frees up: nothing to retry
    assert d.body["error"]["type"] == "permission_error"
    assert d.body["error"]["message"] == "Your gateway credit is used up ($5.50 of $5.00). Ask the gateway admin for more."
    st = limits.states(conn, gw.cfg, uid)[0]
    assert st.reset_in is None and st.exceeded
    assert limits.evaluate(conn, gw.cfg, uid, "claude-sonnet-5", "/v1/messages/count_tokens") is None
    async with app(gw) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": PW})
        c.headers["x-csrf-token"] = r.json()["csrf"]
        assert (await c.post(f"/api/admin/users/{uid}/upgrade", json={"cost_daily": 0})).status_code == 400
        assert (await c.post(f"/api/admin/users/{uid}/upgrade", content=b'{"cost_daily": Infinity}',
                             headers={"content-type": "application/json"})).status_code == 400
        assert (await c.post(f"/api/admin/users/{uid}/upgrade", json={"cost_daily": 50})).status_code == 200
    rows = conn.execute("SELECT kind, value FROM limits WHERE user_id=?", (uid,)).fetchall()
    assert [tuple(r) for r in rows] == [("cost_daily", "50")]
    assert limits.evaluate(conn, gw.cfg, uid, "claude-sonnet-5", "/v1/messages") is None


def test_cost_total_is_a_valid_admin_limit():
    assert limits.validate("cost_total", "*", "5", None) == ("*", "5", "usd")
    with pytest.raises(ValueError):
        limits.validate("cost_total", "*", "5", "count")


async def test_only_a_browser_sign_in_can_authorize_a_computer(env):
    gw, conn, _ = env
    async with app(gw) as cli, app(gw) as browser, app(gw) as thief:
        user = await signed_in(browser)
        a = (await cli.post("/api/device/start", json={"label": "one"})).json()
        await browser.post(f"/api/device/{a['user_code']}/approve", json={})
        key = (await cli.post("/api/device/token", json={"device_code": a["device_code"]})).json()["key"]
        b = (await cli.post("/api/device/start", json={"label": "two"})).json()
        r = await cli.post(f"/api/device/{b['user_code']}/approve", json={}, headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 403   # a key as Bearer never authorizes
        r = await thief.post("/api/login/key", json={"key": key})   # a session made from a computer's key
        assert r.status_code == 200 and r.json()["user"]["role"] == "user"
        thief.headers["x-csrf-token"] = r.json()["csrf"]
        r = await thief.post(f"/api/device/{b['user_code']}/approve", json={})
        assert r.status_code == 403 and "computer's key" in r.json()["error"]
        assert (await thief.get("/api/session")).status_code == 200
        key_id = (await browser.get("/api/keys")).json()["keys"][0]["id"]
        assert (await browser.post(f"/api/keys/{key_id}/remove", json={})).json()["removed"]
        assert (await thief.get("/api/session")).status_code == 401   # removing the key ends its sessions
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE key_id IS NOT NULL").fetchone()[0] == 0
    assert user


async def test_an_admins_computer_key_is_not_an_admin(env):
    gw, conn, _ = env
    from claude_proxy.db import add_machine_key
    admin_id = conn.execute("SELECT id FROM users WHERE name='admin'").fetchone()[0]
    key = add_machine_key(conn, admin_id, "phished")
    assert authenticate(conn, {"authorization": f"Bearer {key}"})["role"] == "user"
    async with app(gw) as c:
        assert (await c.get("/api/users", headers={"Authorization": f"Bearer {key}"})).status_code == 403
        r = await c.post("/api/login/key", json={"key": key})
        assert r.json()["user"]["role"] == "user"
        assert (await c.get("/api/users")).status_code == 403
        assert (await c.get("/api/session")).json()["user"]["role"] == "user"


async def test_a_deleted_account_gets_no_new_credit(env):
    gw, conn, _ = env
    from claude_proxy.db import delete_user, revoke
    async with app(gw) as c:
        uid = (await signed_in(c))["id"]
    revoke(conn, uid)
    delete_user(conn, uid)
    async with app(gw) as c:
        r = await c.post("/api/login/clerk", json={"token": jwt(claims())})
    assert r.status_code == 403 and "removed" in r.json()["error"]
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


async def test_the_authorize_page_shows_where_the_request_came_from(env):
    gw, *_ = env
    async with app(gw) as cli, app(gw) as browser:
        await signed_in(browser)
        s = (await cli.post("/api/device/start", json={})).json()
        v = (await browser.get(f"/api/device/{s['user_code']}")).json()
    assert v["ip"] == "127.0.0.1" and v["your_ip"] == "127.0.0.1"


async def test_privacy_page(env):
    gw, *_ = env
    gw.cfg.retention_days = 90
    async with app(gw) as c:
        r = await c.get("/privacy")
    assert r.status_code == 200 and "after 90 days" in r.text and "not stored" in r.text
    assert "no-transform" in r.headers["cache-control"]


def test_credit_accounts_share_a_daily_cap(env):
    gw, conn, _ = env
    gw.cfg.signup.free_daily_cap_usd = 3.0
    a, _ = create_user(conn, "a"); b, _ = create_user(conn, "b"); paid, _ = create_user(conn, "paid")
    for uid in (a, b):
        conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (uid, "cost_total", "*", "5", "usd"))
    spend(conn, a, 2.0)
    spend(conn, paid, 50.0)   # accounts without a credit neither count nor are held back
    assert limits.evaluate(conn, gw.cfg, b, "claude-sonnet-5", "/v1/messages") is None
    spend(conn, b, 1.5)
    d = limits.evaluate(conn, gw.cfg, a, "claude-sonnet-5", "/v1/messages")
    assert d.status == 429 and d.kind == limits.FREE_CAP
    assert "Try again in" in d.body["error"]["message"] and 80000 < d.retry_after <= 86400
    assert limits.evaluate(conn, gw.cfg, paid, "claude-sonnet-5", "/v1/messages") is None
    assert limits.evaluate(conn, gw.cfg, a, "claude-sonnet-5", "/v1/messages/count_tokens") is None
    conn.execute("UPDATE requests SET started_at=started_at-86400")   # a day later it has room again
    assert limits.evaluate(conn, gw.cfg, a, "claude-sonnet-5", "/v1/messages") is None
    gw.cfg.signup.free_daily_cap_usd = 0
    spend(conn, a, 1.0); spend(conn, b, 2.9)
    assert limits.evaluate(conn, gw.cfg, a, "claude-sonnet-5", "/v1/messages") is None   # 0: no cap
