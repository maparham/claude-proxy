import json, pathlib, sys
from playwright.sync_api import sync_playwright
from cap_live import VW, VH, DPR, UA, OUT, at, hide_chat
KEY = open("/tmp/cpdemo/userkey").read().strip()
D = "http://127.0.0.1:18181"
SWAP = """() => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) { const n = w.currentNode; if (n.nodeValue.includes('127.0.0.1:18181'))
    n.nodeValue = n.nodeValue.replaceAll('http://127.0.0.1:18181', 'https://dash.rahkar.pro'); } }"""


def open_ctx(p):
    br = p.chromium.launch(channel="chrome", headless=True)
    ctx = br.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=DPR, is_mobile=True,
                         has_touch=True, locale="fa-IR", user_agent=UA)
    pg = ctx.new_page()
    pg.goto(D + "/dashboard", wait_until="domcontentloaded")
    pg.wait_for_timeout(1500)
    pg.evaluate("""k => { const i = document.querySelector('input[name=key]'); i.value = k; i.form.requestSubmit(); }""", KEY)
    pg.wait_for_timeout(2500)
    return br, ctx, pg


if __name__ == "__main__":
    with sync_playwright() as p:
        br, ctx, pg = open_ctx(p)
        print(pg.url)
        print([t for t in pg.locator("a, button").all_inner_texts() if t.strip()][:40])
        pg.screenshot(path=OUT / "l_dash.png")
        br.close()
