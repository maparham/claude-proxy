"""Mobile captures of the live site (signed out, fresh profile): home, pricing, visitor order dialog, sign-up."""
import json, pathlib
from playwright.sync_api import sync_playwright

OUT = pathlib.Path(__file__).resolve().parent / "shots"
OUT.mkdir(exist_ok=True)
VW, VH, DPR = 392, 697, 1080 / 392
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/18.0 Mobile/15E148 Safari/604.1")
taps = {}


def at(loc):
    b = loc.bounding_box()
    return [round((b["x"] + b["width"] / 2) * DPR), round((b["y"] + b["height"] / 2) * DPR)]


def hide_chat(pg):   # the floating "questions?" bubble covers the buttons in a phone-sized view
    pg.add_style_tag(content=".chat-fab,#chat-fab,[class*=chat-btn],[class*=support-fab]{display:none!important}")
    pg.evaluate("""() => { for (const e of document.querySelectorAll('body *')) { const s = getComputedStyle(e);
        if (s.position === 'fixed' && e.getBoundingClientRect().bottom > innerHeight - 140 && e.getBoundingClientRect().width < 200) e.style.display = 'none'; } }""")


def shot(pg, name, tap=None):
    hide_chat(pg)
    pg.wait_for_timeout(400)
    pg.screenshot(path=OUT / f"{name}.png")
    if tap is not None:
        taps[name] = at(tap)


if __name__ == "__main__":
    with sync_playwright() as p:
          br = p.chromium.launch(channel="chrome", headless=True)
          ctx = br.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=DPR, is_mobile=True,
                               has_touch=True, locale="fa-IR", user_agent=UA)
          pg = ctx.new_page()
          pg.goto("https://rahkar.pro/", wait_until="domcontentloaded")
          pg.wait_for_timeout(4000)
          shot(pg, "m_home", pg.locator(".hero-cta a").first)
          pg.evaluate("document.querySelector('#pricing').scrollIntoView({block:'start'})")
          pg.evaluate("window.scrollBy(0, 150)")
          pg.wait_for_timeout(800)
          buy = pg.locator("#cards button").nth(1)
          shot(pg, "m_pricing", buy)
          buy.evaluate("e => e.click()")
          pg.wait_for_timeout(1500)
          dlg = pg.locator("#order-dialog")
          print("dialog buttons:", dlg.locator("button, a").all_inner_texts())
          go = dlg.get_by_text("ادامه و ورود")
          go.scroll_into_view_if_needed()
          shot(pg, "m_visitor", go)
          go.evaluate("e => e.click()")
          pg.wait_for_timeout(6000)
          print("url:", pg.url)
          pg.screenshot(path=OUT / "m_signin_raw.png")
          links = pg.locator("a, button").all_inner_texts()
          print("links:", [l for l in links if l.strip()][:30])
          ctx.close(); br.close()
    json.dump(taps, open(OUT / "taps_live.json", "w"))
    print(taps)
