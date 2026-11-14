"""Clerk's Frontend API through the dashboard (/__clerk, Clerk "proxy" mode): from Iran Clerk's own host freezes."""
import httpx
import pytest

from claude_proxy import clerk_proxy
from claude_proxy.gateway import Gateway
from claude_proxy.web import COOKIE, create_dashboard_app
from tests.conftest import asgi_client
from tests.test_signup import PK

PROXY = "https://dash.test/__clerk"


class Body(httpx.AsyncByteStream):
    """A body that arrives as a stream, as from the network (an httpx.Response built from bytes is read at once)."""

    def __init__(self, data: bytes = b""):
        self.data = data

    async def __aiter__(self):
        yield self.data


class Upstream(httpx.AsyncBaseTransport):
    """Clerk's Frontend API. Not httpx.MockTransport: that reads the whole body first, and the proxy streams it."""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.reply = lambda: httpx.Response(200, headers={"content-type": "application/json"}, stream=Body(b'{"ok": true}'))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == clerk_proxy.UPSTREAM_HOST:
            await request.aread()
            self.seen.append(request)
            return self.reply()
        return httpx.Response(404, stream=Body())


@pytest.fixture
def up(cfg, db, monkeypatch):
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_secret")
    cfg.listener.public_url, cfg.listener.dashboard_url = "https://gw.test", "https://dash.test"
    cfg.signup.clerk_publishable_key, cfg.signup.clerk_proxy_url = PK, PROXY
    fake = Upstream()
    gw = Gateway(cfg, db[1], http=httpx.AsyncClient(transport=fake))
    return gw, fake


async def test_forwards_path_query_and_body(up):
    gw, fake = up
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.post("/__clerk/v1/client/sign_ins?_clerk_js_version=6", content=b"identifier=a%40b.c",
                         headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    req = fake.seen[0]
    assert str(req.url) == f"https://{clerk_proxy.UPSTREAM_HOST}/v1/client/sign_ins?_clerk_js_version=6"
    assert req.method == "POST" and req.content == b"identifier=a%40b.c"


async def test_adds_clerk_headers(up):
    gw, fake = up
    async with asgi_client(create_dashboard_app(gw)) as c:
        await c.get("/__clerk/v1/environment")
    h = fake.seen[0].headers
    assert h["clerk-proxy-url"] == PROXY
    assert h["clerk-secret-key"] == "sk_test_secret"
    assert h["x-forwarded-for"]


async def test_strips_own_session_and_auth(up):
    gw, fake = up
    async with asgi_client(create_dashboard_app(gw)) as c:
        await c.get("/__clerk/v1/client", headers={"cookie": f"{COOKIE}=abc; __client=x", "authorization": "Bearer y"})
    h = fake.seen[0].headers
    assert h.get("cookie") == "__client=x"
    assert "authorization" not in h


async def test_passes_set_cookie_and_location(up):
    gw, fake = up
    fake.reply = lambda: httpx.Response(302, headers=[("location", "https://accounts.google.com/o/oauth2/auth?x=1"),
                                              ("set-cookie", "__client=1; Path=/"), ("set-cookie", "__client_uat=2; Path=/")], stream=Body())
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/__clerk/v1/oauth_callback")
    assert r.status_code == 302
    assert r.headers["location"] == "https://accounts.google.com/o/oauth2/auth?x=1"
    assert sorted(r.headers.get_list("set-cookie")) == ["__client=1; Path=/", "__client_uat=2; Path=/"]


@pytest.mark.parametrize("path", ["/__clerk/..%2Fapi%2Fkeys", "/__clerk//evil.example/x", "/__clerk/https:/evil.example/x",
                                  "/__clerk/@evil.example/x"])
async def test_upstream_host_is_fixed(up, path):
    gw, fake = up
    async with asgi_client(create_dashboard_app(gw)) as c:
        await c.get(path)
    assert all(r.url.host == clerk_proxy.UPSTREAM_HOST for r in fake.seen)


async def test_off_without_config(up):
    gw, fake = up
    gw.cfg.signup.clerk_proxy_url = ""
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/__clerk/v1/environment")
    assert r.status_code == 404 and not fake.seen


@pytest.mark.parametrize("exc, status", [(httpx.ConnectError("refused"), 502), (httpx.ReadTimeout("slow"), 504)])
async def test_upstream_failure_is_a_gateway_error(up, exc, status):
    gw, fake = up

    async def fail(request):
        raise exc

    fake.handle_async_request = fail
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/__clerk/v1/environment")
    assert r.status_code == status


async def test_drops_cloudflare_headers(up):
    # After a DNS rollback requests come through cloudflared; Clerk's own host is on Cloudflare too, and a forwarded
    # cdn-loop or cf-* header must not reach it.
    gw, fake = up
    async with asgi_client(create_dashboard_app(gw)) as c:
        await c.get("/__clerk/v1/client", headers={"cdn-loop": "cloudflare", "cf-ray": "1", "cf-connecting-ip": "1.2.3.4",
                                                    "cf-ipcountry": "IR", "cf-visitor": '{"scheme":"https"}'})
    h = fake.seen[0].headers
    assert not [k for k in h if k.lower().startswith("cf-") or k.lower() == "cdn-loop"]
