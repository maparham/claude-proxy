"""[listener] home_url: the public home page on its own host (e.g. the apex domain), with everything that needs
sign-in sent on to dashboard_url, where Clerk's tokens are made for."""
import httpx

from claude_proxy.web import create_dashboard_app
from tests.test_tickets_web import env  # noqa: F401  (the fixture)


def client(app, host: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"https://{host}")


async def test_home_host_serves_the_home_page_and_sends_sign_in_to_the_dashboard(env):
    gw, conn, cfg, ids, keys = env
    cfg.listener.dashboard_url = "https://claude-dash.example.com/"
    cfg.listener.home_url = "https://example.com"
    app = create_dashboard_app(gw)
    async with client(app, "example.com") as c:
        r = await c.get("/")
        assert r.status_code == 200 and "home.js" in r.text
        assert (await c.get("/privacy")).status_code == 200
        assert (await c.get("/api/pricing")).status_code == 200
        for path in ("/dashboard?tier=lite&length=week", "/admin", "/install", "/install.ps1", "/d/ABCDEFGH"):
            r = await c.get(path)
            assert r.status_code == 302, path
            assert r.headers["location"] == "https://claude-dash.example.com" + path, path
    async with client(app, "claude-dash.example.com") as c:
        assert (await c.get("/dashboard")).status_code == 200


async def test_without_home_url_nothing_is_redirected(env):
    gw, conn, cfg, ids, keys = env
    cfg.listener.dashboard_url = "https://claude-dash.example.com"
    async with client(create_dashboard_app(gw), "example.com") as c:
        assert (await c.get("/dashboard")).status_code == 200
