import json
from playwright.sync_api import sync_playwright
from cap_live import OUT, at, hide_chat
from cap_local import open_ctx, SWAP
taps = {}
with sync_playwright() as p:
    br, ctx, pg = open_ctx(p)
    pg.get_by_text("۱ هفته", exact=True).first.evaluate("e => e.click()"); pg.wait_for_timeout(600)
    print(pg.evaluate("() => [...document.querySelectorAll('input[type=radio]')].map(i => [i.name, i.value, i.checked])"))
    pg.locator("input[name=tier-week][value=standard]").evaluate("e => e.click()"); pg.wait_for_timeout(600)
    pg.get_by_text("ادامه", exact=True).first.evaluate("e => e.click()"); pg.wait_for_timeout(1500)
    d = pg.locator("dialog[open]")
    d.locator("input[type=checkbox]").first.evaluate("e => e.click()"); pg.wait_for_timeout(400)
    pay = d.locator("button", has_text="زرین")
    pay.scroll_into_view_if_needed()
    pg.evaluate(SWAP); hide_chat(pg); pg.wait_for_timeout(300)
    pg.evaluate("document.activeElement && document.activeElement.blur()")
    pg.screenshot(path=OUT / "m_order.png"); taps["m_order"] = at(pay)
    print(pay.inner_text())
    pay.evaluate("e => e.click()")
    pg.wait_for_timeout(8000)
    print("url:", pg.url)
    pg.evaluate("window.scrollTo(0,0)"); pg.wait_for_timeout(500)
    pg.screenshot(path=OUT / "z_top.png")
    zp = pg.locator("button", has_text="پرداخت").first
    print("pay buttons:", pg.locator("button", has_text="پرداخت").count(), [b.is_visible() for b in pg.locator("button", has_text="پرداخت").all()])
    vis = [b for b in pg.locator("button", has_text="پرداخت").all() if b.is_visible()]
    zp = vis[0]
    zp.evaluate("e => e.scrollIntoView({block:'center'})"); pg.wait_for_timeout(500)
    pg.screenshot(path=OUT / "m_zarin.png"); taps["m_zarin"] = at(zp)
    zp.evaluate("e => e.click()")
    pg.wait_for_timeout(9000)
    print("after pay:", pg.url)
    pg.evaluate(SWAP); hide_chat(pg); pg.wait_for_timeout(300)
    pg.screenshot(path=OUT / "m_paid_raw.png")
    ok = pg.locator("dialog[open] button")
    print("paid dialog:", ok.all_inner_texts())
    if ok.count():
        taps["m_paid"] = at(ok.last)
    json.dump(taps, open(OUT / "taps_order.json", "w"))
    input_wait = pg.url
    br.close()
