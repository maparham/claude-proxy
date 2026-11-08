import json, sys
from playwright.sync_api import sync_playwright
from cap_live import OUT, at, hide_chat
from cap_local import open_ctx, SWAP, D
CODE, SHOW = "HHCV-ZNDJ", "JNZH-BCXC"
taps = json.load(open(OUT / "taps_order.json"))
with sync_playwright() as p:
    br, ctx, pg = open_ctx(p)
    pg.goto(D + "/dashboard#authorize/" + CODE); pg.wait_for_timeout(2500)
    pg.evaluate("""([c, s]) => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (w.nextNode()) { const n = w.currentNode;
        n.nodeValue = n.nodeValue.replaceAll(c, s).replace(/(\d{1,3}\.){3}\d{1,3}/g, '—'); } }""", [CODE, SHOW])
    pg.evaluate(SWAP); hide_chat(pg)
    ok = [b for b in pg.locator("button").all() if b.inner_text().strip() == "تأیید" and b.is_visible()][0]
    pg.evaluate("document.activeElement && document.activeElement.blur()")
    pg.screenshot(path=OUT / "m_auth.png"); taps["m_auth"] = at(ok)
    print(pg.locator("body").inner_text()[:600])
    br.close()
json.dump(taps, open(OUT / "taps_order.json", "w")); print(taps)
