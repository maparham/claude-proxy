"""[listener] home_url: the public home page on its own host (e.g. the apex domain), with everything that needs
sign-in sent on to dashboard_url, where Clerk's tokens are made for."""
import httpx

from claude_proxy.web import create_dashboard_app
from tests.test_tickets_web import env  # noqa: F401  (the fixture)


def client(app, host: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"https://{host}")


async def test_home_host_serves_the_home_page_and_sends_sign_in_to_the_dashboard(env):
    gw, conn, cfg, ids, keys = env
    cfg.listener.dashboard_url = "https://dash.example.com/"
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
            assert r.headers["location"] == "https://dash.example.com" + path, path
    async with client(app, "dash.example.com") as c:
        assert (await c.get("/dashboard")).status_code == 200


async def test_without_home_url_nothing_is_redirected(env):
    gw, conn, cfg, ids, keys = env
    cfg.listener.dashboard_url = "https://dash.example.com"
    async with client(create_dashboard_app(gw), "example.com") as c:
        assert (await c.get("/dashboard")).status_code == 200


async def test_search_engines_index_the_home_host_only(env):
    gw, conn, cfg, ids, keys = env
    cfg.listener.dashboard_url = "https://dash.example.com/"
    cfg.listener.home_url = "https://example.com"
    app = create_dashboard_app(gw)
    async with client(app, "example.com") as c:
        assert '<link rel="canonical" href="https://example.com/">' in (await c.get("/")).text
        robots = (await c.get("/robots.txt")).text
        assert "Allow: /$" in robots and "Sitemap: https://example.com/sitemap.xml" in robots
        sitemap = await c.get("/sitemap.xml")
        assert sitemap.headers["content-type"].startswith("application/xml")
        assert "<loc>https://example.com/</loc>" in sitemap.text and "<loc>https://example.com/privacy</loc>" in sitemap.text
    async with client(app, "dash.example.com") as c:
        assert (await c.get("/robots.txt")).text == "User-agent: *\nDisallow: /\n"
        assert (await c.get("/sitemap.xml")).status_code == 404


async def test_blog_is_served_on_the_home_host_and_listed_in_the_sitemap(env):
    gw, conn, cfg, ids, keys = env
    cfg.listener.dashboard_url = "https://dash.example.com/"
    cfg.listener.home_url = "https://example.com"
    async with client(create_dashboard_app(gw), "example.com") as c:
        index = await c.get("/blog")
        assert index.status_code == 200 and 'href="/blog/visa-documents-lite-pass"' in index.text
        post = await c.get("/blog/visa-documents-lite-pass")
        assert post.status_code == 200 and "بسته اقتصادی" in post.text
        assert '<link rel="canonical" href="https://example.com/blog/visa-documents-lite-pass">' in post.text
        assert '<meta property="og:image" content="https://example.com/static/og.png">' in post.text
        home = (await c.get("/")).text
        assert '<meta property="og:image" content="https://example.com/static/og.png">' in home
        assert '<ul class="latest-list"' in home and 'href="/blog/visa-documents-lite-pass"' in home
        assert (await c.get("/static/og.png")).headers["content-type"] == "image/png"
        assert (await c.get("/blog/no-such-post")).status_code == 404
        assert "<link>https://example.com/blog/visa-documents-lite-pass</link>" in (await c.get("/blog/feed.xml")).text
        sitemap = (await c.get("/sitemap.xml")).text
        assert "<loc>https://example.com/blog/visa-documents-lite-pass</loc><lastmod>" in sitemap
        assert "Allow: /blog" in (await c.get("/robots.txt")).text


async def test_home_page_html_carries_its_persian_text_for_search_engines(env):
    # Crawlers that read the HTML without running i18n.js still see the Persian headings, FAQ and keywords.
    gw, conn, cfg, ids, keys = env
    cfg.listener.home_url = "https://example.com"
    async with client(create_dashboard_app(gw), "example.com") as c:
        html = (await c.get("/")).text
    assert "<title>خرید اشتراک کلاد" in html
    assert "اشتراک کلاد<span" in html and "Your Claude Code access" not in html
    assert "به کارت ارزی یا فیلترشکن نیاز دارم؟" in html and "پرداخت ریالی است" in html
    assert '"@type": "FAQPage"' in html and '"به کارت ارزی یا فیلترشکن نیاز دارم؟"' in html


async def test_home_host_serves_its_indexnow_key_and_lets_crawlers_fetch_it(env):
    from claude_proxy.web import INDEXNOW_KEY
    gw, conn, cfg, ids, keys = env
    cfg.listener.dashboard_url = "https://dash.example.com/"
    cfg.listener.home_url = "https://example.com"
    app = create_dashboard_app(gw)
    async with client(app, "example.com") as c:
        r = await c.get(f"/{INDEXNOW_KEY}.txt")
        assert r.status_code == 200 and r.text == INDEXNOW_KEY
        assert f"Allow: /{INDEXNOW_KEY}.txt" in (await c.get("/robots.txt")).text
    async with client(app, "dash.example.com") as c:
        assert (await c.get(f"/{INDEXNOW_KEY}.txt")).status_code == 404


def test_each_post_links_the_buying_guide_and_related_posts():
    from claude_proxy import blog
    posts = {p.slug: p for p in blog.posts()}
    for p in posts.values():
        links = [q.slug for q in blog.related(p)]
        assert len(links) == 3 and p.slug not in links
        if p.slug != blog.GUIDE:
            assert links[0] == blog.GUIDE
    page = blog.post_page("https://example.com", posts["what-is-claude"], None)
    assert 'id="related-h">مطالب مرتبط</h2>' in page and 'href="/blog/buy-claude-subscription-iran"' in page
