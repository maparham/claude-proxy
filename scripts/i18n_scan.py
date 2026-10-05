"""Walks the dashboard in Persian and English and reports text that slipped past the dictionaries.

For each language it signs in as the admin (password, at /admin) and as a user (gateway key), opens every tab and
each row action's dialog, and reports:
  - in Persian, visible English words outside code, <bdi>, dir="ltr" and a short list of names that stay Latin;
  - in both, dictionary keys shown raw ("ov.title_admin"), "undefined", "NaN", and i18n.js's "missing" warnings;
  - a page wider than the window at 1280 and 375 pixels.
Screenshots go to --shots. Exits 1 when anything was reported. Run against a local gateway, never production:

  uv run --with playwright python scripts/i18n_scan.py --base http://127.0.0.1:18090 --admin-password … --user-key sk-proxy-…
"""
import argparse
import re
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

TABS = ["overview", "usage", "users", "user/2", "tickets", "orders", "pricing", "quota", "models", "activity", "sessions", "errors", "audit"]
USER_TABS = ["overview", "usage", "models", "activity", "sessions", "errors"]
LATIN_OK = (r"Claude Code|Claude|Anthropic|Opus|Sonnet|Haiku|gclaude|claude-gateway|claude-proxy|OpenCode|Muse|Meta|Google|GitHub|Clerk|API|"
            r"USD|EUR|IRR|macOS|Linux|Windows|PowerShell|JSON|CSRF|Lite|Standard|Pro|Max|English")

# Text nodes shown to the reader, minus the places that stay Latin on purpose.
WALK = """(sel) => {
  const out = [], skip = "code, pre, bdi, [dir=ltr], script, style, .tsel-list, svg, .echarts-text";
  for (const root of document.querySelectorAll(sel)) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n; (n = w.nextNode());) {
      const el = n.parentElement;
      if (!el || el.closest(skip) || !el.getClientRects().length) continue;
      const s = n.textContent.trim();
      if (s) out.push(s);
    }
    root.querySelectorAll("[placeholder],[title],[aria-label]").forEach((el) => {
      if (el.closest(skip)) return;
      ["placeholder", "title", "aria-label"].forEach((a) => el.getAttribute(a) && out.push(el.getAttribute(a)));
    });
  }
  return out;
}"""


def scan(page, lang: str, where: str, keys: set[str], findings: list[str]) -> None:
    for s in page.evaluate(WALK, "#app:not(.hidden), #login:not(.hidden), dialog[open]"):
        if "undefined" in s or re.search(r"\bNaN\b", s):
            findings.append(f"[{lang}] {where}: {s[:120]!r}")
        if any(k in s for k in re.findall(r"\b[a-z]+\.[a-z0-9_.:]+\b", s) if k in keys):
            findings.append(f"[{lang}] {where}: raw key in {s[:120]!r}")
        if lang == "fa":
            rest = re.sub(LATIN_OK, "", re.sub("\u2068.*?\u2069", "", s))   # isolated names are data
            if re.search(r"[A-Za-z]{3,}", rest) and not re.fullmatch(r"[\w.@+-]+(\s*[·,]\s*[\w.@+-]+)*", s):
                findings.append(f"[fa] {where}: English {s[:120]!r}")


def run(args) -> int:
    findings: list[str] = []
    shots = Path(args.shots or tempfile.mkdtemp(prefix="i18n-scan-"))
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for lang in ("fa", "en"):
            for who in ("admin", "user"):
                for width in (1280, 375):
                    ctx = browser.new_context(viewport={"width": width, "height": 900}, bypass_csp=True)   # wait_for_function evaluates a string
                    ctx.add_init_script(f"try {{ localStorage.setItem('cp-lang', '{lang}'); }} catch {{}}")
                    page = ctx.new_page()
                    warns: list[str] = []
                    page.on("console", lambda m: "i18n: missing" in m.text and warns.append(m.text))
                    page.goto(f"{args.base}/{'admin' if who == 'admin' else 'dashboard'}")
                    page.wait_for_selector("#login:not(.hidden)")
                    keys = set(page.evaluate("Object.keys(I18N.en)"))
                    scan(page, lang, f"{who} sign-in", keys, findings)
                    page.screenshot(path=str(shots / f"{lang}-{who}-{width}-signin.png"))
                    if who == "admin":
                        page.fill("#form-admin input[name=password]", args.admin_password)
                        page.click("#form-admin button[type=submit]")
                    else:
                        page.fill("#form-key input[name=key]", args.user_key)
                        page.click("#form-key button[type=submit]")
                    page.wait_for_selector("#app:not(.hidden)")
                    for tab in TABS if who == "admin" else USER_TABS:
                        page.evaluate(f"location.hash = '{tab}'")
                        page.wait_for_function("!document.querySelector('#main').textContent.includes(t('app.loading'))")
                        page.wait_for_timeout(400)
                        where = f"{who} #{tab} @{width}"
                        scan(page, lang, where, keys, findings)
                        if page.evaluate("document.documentElement.scrollWidth > innerWidth + 1"):
                            findings.append(f"[{lang}] {where}: page scrolls sideways")
                        page.screenshot(path=str(shots / f"{lang}-{who}-{width}-{tab.replace('/', '-')}.png"), full_page=True)
                        if width == 1280 and tab in ("users", "tickets", "orders"):
                            n = page.locator("#main [data-act], #main [data-tact], #main [data-oact]").count()
                            for i in range(n):
                                b = page.locator("#main [data-act], #main [data-tact], #main [data-oact]").nth(i)
                                if b.get_attribute("data-act") in ("rotate", "revoke", "disable", "enable", "routes_key", "routes_key_remove", "ungate") \
                                        or b.get_attribute("data-oact") == "contacted" or b.get_attribute("data-tact") == "cancel":
                                    continue   # these act at once (or after confirm()); only dialogs are scanned
                                b.click()
                                if page.locator("dialog[open]").count():
                                    page.wait_for_timeout(400)
                                    scan(page, lang, f"{where} dialog {i}", keys, findings)
                                    page.screenshot(path=str(shots / f"{lang}-{tab}-dialog-{i}.png"))
                                    page.keyboard.press("Escape")
                    findings += [f"[{lang}] {who}: {w}" for w in sorted(set(warns))]
                    ctx.close()
        browser.close()
    for f in dict.fromkeys(findings):
        print(f)
    print(f"{len(dict.fromkeys(findings))} findings; screenshots in {shots}")
    return 1 if findings else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--user-key", required=True)
    ap.add_argument("--shots")
    sys.exit(run(ap.parse_args()))
