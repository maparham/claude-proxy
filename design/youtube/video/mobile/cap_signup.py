import json, pathlib
from playwright.sync_api import sync_playwright
from cap_live import VW, VH, DPR, UA, OUT, at, hide_chat
taps = json.load(open(OUT / "taps_live.json"))
with sync_playwright() as p:
    br = p.chromium.launch(channel="chrome", headless=True)
    ctx = br.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=DPR, is_mobile=True,
                         has_touch=True, locale="fa-IR", user_agent=UA)
    pg = ctx.new_page()
    pg.goto("https://dash.rahkar.pro/dashboard", wait_until="domcontentloaded")
    pg.wait_for_timeout(7000)
    print([t for t in pg.locator("a, button").all_inner_texts() if t.strip()][:20])
    su = pg.get_by_text("ثبت نام", exact=True)
    print("signup links:", su.count())
    if su.count():
        su.first.evaluate("e => e.click()")
        pg.wait_for_timeout(5000)
    hide_chat(pg)
    pg.screenshot(path=OUT / "m_signup.png")
    g = pg.locator("button:has-text('Google'), a:has-text('Google')")
    print("google:", g.count())
    if g.count():
        taps["m_signup"] = at(g.first)
    ctx.close(); br.close()
json.dump(taps, open(OUT / "taps_live.json", "w")); print(taps)
