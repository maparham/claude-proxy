from playwright.sync_api import sync_playwright
from cap_live import VW, VH, DPR, UA
with sync_playwright() as p:
    br = p.chromium.launch(channel="chrome", headless=True)
    ctx = br.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=DPR, is_mobile=True, has_touch=True, user_agent=UA)
    pg = ctx.new_page()
    pg.goto("file:///tmp/rahkar-demo2/home/nan-e-sangak/index.html", wait_until="domcontentloaded")
    pg.wait_for_timeout(4000)
    pg.screenshot(path="../out/bakery-mobile.png")
    br.close()
