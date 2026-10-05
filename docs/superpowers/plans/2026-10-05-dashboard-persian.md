# Bilingual Dashboard (Persian default) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The dashboard (`/dashboard`, `/admin`) shows in Persian (RTL) by default, with a switch to English.

**Architecture:** A new `static/i18n.js` holds both dictionaries and `t()`/`plural()`/`errMsg()`. It loads
blocking in `<head>` and sets `lang`/`dir` before paint. `app.js` replaces every literal UI string with `t("key")`.
`index.html` text gets `data-i18n` attributes, filled in once at start-up. Switching language saves the choice
and reloads the page. CSS moves to logical properties, and Persian uses Vazirmatn.

**Tech Stack:** Vanilla JS (no build step), `Intl` (`fa-IR` / `en-US`), ECharts 5.6, Clerk JS v6, Starlette
(`web.py`), pytest and node (`vm` harnesses) for tests, Playwright for the visual pass.

**Spec:** `docs/superpowers/specs/2026-10-05-dashboard-persian-design.md`

## Global Constraints

- Default language `fa`. `localStorage` key `cp-lang`, values `"fa"` | `"en"`. Every storage access is wrapped in try/catch.
- `LOC` is `"fa-IR"` when `LANG === "fa"`, otherwise `"en-US"`.
- Scope is `index.html`, `app.js`, `app.css`, `i18n.js` and the CSP only. `home.html`, `privacy.html`, mail and the CLI stay English.
- Work directly on `master`: no branches or worktrees, subagents included. Commit only the paths you touched.
- Persian text uses `ی` (U+06CC) and `ک` (U+06A9), never Arabic `ي`/`ك`. Use ZWNJ (U+200C) in `می‌`, `‌ها`, `‌ای`.
- No Persian literals in `app.js`. All Persian text lives in `i18n.js`.
- English output must stay byte-identical to today's wherever a test asserts it. The node harnesses run with `LANG = "en"`.
- Keys, model ids, commands, code, emails, URLs, user names and IPs stay Latin, wrapped in `<bdi>` (inline) or `dir="ltr"` (blocks and inputs).
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Persian glossary (use these terms everywhere)

| English | فارسی |
|---|---|
| Gateway | درگاه |
| Dashboard | داشبورد |
| Sign in / Sign out | ورود / خروج |
| Gateway key / key | کلید درگاه / کلید |
| Subscription | اشتراک |
| Usage | مصرف |
| Tokens / Weighted tokens / Raw tokens | توکن / توکن وزنی / توکن خام |
| Requests | درخواست‌ها |
| Est. cost (USD) | هزینهٔ تخمینی (دلار) |
| Limit(s) | سقف / سقف‌ها |
| Quota / Account quota | سهمیه / سهمیهٔ حساب |
| 5-hour bucket / 7-day (weekly) bucket | سهمیهٔ ۵ ساعته / سهمیهٔ هفتگی |
| Share | سهم |
| Credit | اعتبار |
| Ticket(s) | تیکت / تیکت‌ها |
| Order(s) | سفارش / سفارش‌ها |
| Pricing | تعرفه‌ها |
| Computer | رایانه |
| Authorize / Authorized / Cancelled | تأیید / تأیید شد / لغو شد |
| Session(s) | نشست / نشست‌ها |
| Model(s) / Cache / Cache hit ratio | مدل / کش / نرخ برخورد کش |
| Admin / User | مدیر / کاربر |
| Overview | نمای کلی |
| Usage over time | مصرف در طول زمان |
| Users & limits | کاربران و سقف‌ها |
| Models & cache | مدل‌ها و کش |
| Activity / Errors / Audit log | فعالیت / خطاها / گزارش رویدادها |
| Connect a computer | اتصال رایانه |
| Enable / Disable / Revoke / Delete / Rotate key / Upgrade / Rename | فعال‌سازی / غیرفعال‌سازی / ابطال / حذف / کلید تازه / ارتقا / تغییر نام |
| Bonus / Capacity | پاداش / ظرفیت |
| Theme / Loading… / Other / never | پوسته / در حال بارگذاری… / سایر / هرگز |
| Cancel / Save / Close | انصراف / ذخیره / بستن |

Brand and product names stay Latin: Claude, Anthropic, Opus, Sonnet, Haiku, gclaude, claude-gateway, OpenCode,
Muse, Meta, Google, GitHub, Clerk, API.

## Key naming

Keys are flat and dotted: `<area>.<name>`. The areas are `app` (header, generic), `login`, `tab`, `ov`
(overview), `usage`, `users`, `user`, `lim` (limits/dialogs), `tk` (tickets), `ord` (orders), `price`, `auth`
(authorize/computers), `quota`, `models`, `act` (activity), `sess`, `errs` (Errors tab), `audit`, `chart`, `dur`,
`tip`, `kind`, `kindnote`, `unit`, `kshort`, `etip`, `elabel`, `utip`. Tip keys keep their current names after
the prefix: `tip.bucket:5h`, `etip.gateway_limit`. Plural pairs are `<key>.one` / `<key>.other`.

## Review Focus

1. **Persian digits typed into a number field** (Persian keyboard: `۲۵`) are accepted as 25, not rejected or saved as NaN. Pinned in Task 3.
2. **A Latin user name or model id inside a Persian sentence** (`کاربر alice@example.com …`) keeps its place and punctuation. Pinned by the `bdi()` helper test in Task 3 and the scan in Task 10.
3. **A server error with no translation** (a new `fail()` message) still shows, in English, and never as `undefined` or a raw key. Pinned in Task 8.
4. **A missing dictionary key** shows the English text, not the dotted key. Pinned in Task 1 (`t` fallback) and Task 10 (raw-key scan).
5. **The language switch on the sign-in screen and mid-flow** (`#authorize/ABCD-EFGH` open) keeps the address and the pending code. Pinned in Task 2 (reload keeps `location.hash`) and Task 10.

---

### Task 1: `i18n.js` core, served and versioned, test harnesses load it

**Files:**
- Create: `src/claude_proxy/static/i18n.js`
- Modify: `src/claude_proxy/static/index.html` (head)
- Modify: `src/claude_proxy/web.py:1194` (`versioned("index.html", …)`)
- Modify: `tests/test_tickets_web.py` (`PICK_HARNESS`, `APP_FN_HARNESS`)
- Create: `tests/test_i18n.py`

**Interfaces:**
- Produces (globals, classic script): `I18N` (`{en: {}, fa: {}}`), `LANG` (`"fa"|"en"`), `LOC` (`"fa-IR"|"en-US"`),
  `t(key: string, vars?: object): string`, `plural(key: string, n: number, vars?: object): string`,
  `applyStatic(root?: Element): void`, `setLang(lang: "fa"|"en"): void`, `ERR_FA` (`{[english]: persian}`),
  `ERR_PATTERNS` (`[RegExp, string][]`), `errMsg(text: string): string`.
- `plural` puts `n` (unformatted) into `vars.n` unless the caller passes its own `n`.

- [ ] **Step 1: Write the failing tests** in `tests/test_i18n.py`:

```python
"""The dashboard's dictionaries (static/i18n.js): both languages complete, and every key the page uses defined."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "claude_proxy" / "static"

DUMP = r"""
const fs = require("fs"), vm = require("vm");
const ctx = { localStorage: { getItem: () => process.argv[3] || null, setItem() {} }, console };
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8") + "\n;this.out = { I18N, LANG, LOC, ERR_FA, ERR_PATTERNS: ERR_PATTERNS.map(([r, k]) => [r.source, k]) };", ctx);
const probe = process.argv[4];
if (probe) vm.runInNewContext(probe, ctx);
console.log(JSON.stringify(ctx.out));
"""


def node(tmp_path, lang=None, probe=""):
    if not shutil.which("node"):
        pytest.skip("no node here")
    h = tmp_path / "dump.js"
    h.write_text(DUMP)
    return json.loads(subprocess.run(["node", str(h), str(STATIC / "i18n.js"), lang or "", probe],
                                     capture_output=True, text=True, timeout=30, check=True).stdout)


def test_persian_is_the_default_and_english_is_remembered(tmp_path):
    assert node(tmp_path)["LANG"] == "fa" and node(tmp_path)["LOC"] == "fa-IR"
    assert node(tmp_path, "en")["LANG"] == "en" and node(tmp_path, "en")["LOC"] == "en-US"
    assert node(tmp_path, "xx")["LANG"] == "fa"   # junk in storage: the default


def test_both_languages_have_the_same_keys_and_placeholders(tmp_path):
    d = node(tmp_path)["I18N"]
    assert set(d["en"]) == set(d["fa"])
    ph = lambda s: sorted(set(re.findall(r"\{(\w+)\}", s)))
    bad = [k for k in d["en"] if ph(d["en"][k]) != ph(d["fa"][k])]
    assert bad == []


def test_persian_uses_persian_letters(tmp_path):
    d = node(tmp_path)
    text = "".join(d["I18N"]["fa"].values()) + "".join(d["ERR_FA"].values())
    assert "ي" not in text and "ك" not in text


def test_t_falls_back_to_english_then_the_key(tmp_path):
    out = node(tmp_path, "fa", 'I18N.en["x.only_en"] = "Hi {name}";'
               ' I18N.en["x.n.one"] = "{n} thing"; I18N.en["x.n.other"] = "{n} things"; I18N.fa["x.n.one"] = I18N.fa["x.n.other"] = "{n} چیز";'
               ' this.out = [t("x.only_en", {name: "a"}), t("x.nowhere"), plural("x.n", 1), plural("x.n", 2)];')
    assert out == ["Hi a", "x.nowhere", "1 چیز", "2 چیز"]


def _used_keys():
    js = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    keys = set(re.findall(r"""\bt\(\s*"([a-z][\w.:-]*)\"""", js))
    plurals = set(re.findall(r"""\bplural\(\s*"([a-z][\w.:-]*)\"""", js))
    keys |= set(re.findall(r'data-i18n="([^"]+)"', html))
    for spec in re.findall(r'data-i18n-attr="([^"]+)"', html):
        keys |= {part.split(":", 1)[1] for part in spec.split(";")}
    return keys, plurals


def test_every_key_the_page_uses_is_defined(tmp_path):
    en = node(tmp_path)["I18N"]["en"]
    keys, plurals = _used_keys()
    assert sorted(k for k in keys if k not in en) == []
    assert sorted(k for k in plurals if f"{k}.one" not in en or f"{k}.other" not in en) == []


def test_app_js_holds_no_persian():
    assert not re.search(r"[؀-ۿ]", (STATIC / "app.js").read_text())
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_i18n.py -q`
Expected: FAIL. `i18n.js` doesn't exist, so node exits non-zero.

- [ ] **Step 3: Create `static/i18n.js`**

```js
"use strict";
// The dashboard's words, in Persian (the default) and English. Loaded in <head> before the page paints, so the
// direction is right from the first frame; app.js asks t() for every string it shows (design 2026-10-05).

const I18N = {
  en: {
    "app.lang_other": "فارسی",
    "app.lang_title": "Switch language",
  },
  fa: {
    "app.lang_other": "English",
    "app.lang_title": "تغییر زبان",
  },
};

// Persian unless this browser picked English.
let LANG = "fa";
try { if (localStorage.getItem("cp-lang") === "en") LANG = "en"; } catch { /* private mode: the default */ }
const LOC = LANG === "fa" ? "fa-IR" : "en-US";
if (typeof document !== "undefined") {
  document.documentElement.lang = LANG;
  document.documentElement.dir = LANG === "fa" ? "rtl" : "ltr";
}

// The active language's text, else English's, else the key itself; {name} filled from vars, unescaped (callers pass
// markup-safe values, as the template literals did).
function t(key, vars) {
  let s = I18N[LANG][key] ?? I18N.en[key];
  if (s === undefined) { if (typeof console !== "undefined") console.warn(`i18n: missing ${key}`); return key; }
  return vars ? s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : s;
}
function plural(key, n, vars = {}) { return t(`${key}.${n === 1 ? "one" : "other"}`, { n, ...vars }); }

// data-i18n="key" sets an element's innerHTML (dictionary markup is trusted); data-i18n-attr="placeholder:key;title:key"
// sets attributes. The English in index.html is what shows without JS.
function applyStatic(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => { el.innerHTML = t(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-attr]").forEach((el) => el.dataset.i18nAttr.split(";").forEach((p) => {
    const [attr, key] = p.split(":"); el.setAttribute(attr, t(key));
  }));
}

// A reload rebuilds formatters, charts and Clerk's widget in the new language; the address keeps the open tab.
function setLang(lang) {
  try { localStorage.setItem("cp-lang", lang); } catch { /* private mode: this page only */ }
  location.reload();
}

// Server messages (web.py's fail()) are English; the known ones read in Persian. Exact texts first, then the
// messages built from values, whose groups fill {1}, {2} of the Persian.
const ERR_FA = {};
const ERR_PATTERNS = [];
function errMsg(text) {
  if (LANG !== "fa" || !text) return text;
  if (ERR_FA[text]) return ERR_FA[text];
  for (const [re, fa] of ERR_PATTERNS) {
    const m = re.exec(text);
    if (m) return fa.replace(/\{(\d)\}/g, (_, i) => m[+i] ?? "");
  }
  return text;
}
```

- [ ] **Step 4: Load it in `index.html`'s head and version it**

In `index.html`, change `<html lang="en">` to `<html lang="fa" dir="rtl">`. Then add this line right after the
`app.css` link, without `defer`:

```html
<script src="/static/i18n.js"></script>
```

In `web.py` `page()`: `html = versioned("index.html", ("i18n.js", "app.js", "app.css"))`.

- [ ] **Step 5: Make the node harnesses load `i18n.js` in English**

In `tests/test_tickets_web.py`, `PICK_HARNESS`: before `const code = …`, add
`const i18n = fs.readFileSync(require("path").join(require("path").dirname(process.argv[2]), "i18n.js"), "utf8");`.
Change `vm.runInNewContext(code, ctx);` to `vm.runInNewContext(i18n + "\n" + code, ctx);`, and the ctx to
`const ctx = { String, console, localStorage: { getItem: () => "en" } };`.

In `APP_FN_HARNESS`, do the same: prepend the `i18n` source to `code` inside the `runInNewContext` call, and add
`localStorage: { getItem: () => "en" }` to `ctx`. Because `ctx` has no `document`, `i18n.js` skips the
`<html>` attributes.

- [ ] **Step 6: Run the tests**

Run: `pytest tests/test_i18n.py tests/test_tickets_web.py tests/test_orders_web.py tests/test_web.py -q`
Expected: all PASS. `test_every_key_the_page_uses_is_defined` passes trivially for now.

- [ ] **Step 7: Commit**

```bash
git add src/claude_proxy/static/i18n.js src/claude_proxy/static/index.html src/claude_proxy/web.py tests/test_i18n.py tests/test_tickets_web.py
git commit -m "Dashboard i18n: dictionaries, t(), Persian by default"
```

---

### Task 2: Static page text, the language switch, RTL CSS and the Persian font

**Files:**
- Modify: `src/claude_proxy/static/index.html`
- Modify: `src/claude_proxy/static/app.css` (the 24 `left`/`right` rules, the font)
- Modify: `src/claude_proxy/static/app.js` (start-up, `#lang-toggle`, `#lang-signin`, the tooltip side)
- Modify: `src/claude_proxy/static/i18n.js` (`login.*`, `app.*` keys)
- Modify: `src/claude_proxy/web.py:107-110` (CSP)
- Test: `tests/test_i18n.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `t`, `applyStatic`, `setLang`, `LANG` (Task 1).
- Produces: buttons `#lang-toggle` (header) and `#lang-signin` (sign-in foot). Elements carry `data-i18n` and keys
  `login.*` / `app.*`.

- [ ] **Step 1: Failing tests.** Add to `tests/test_i18n.py`:

```python
def test_the_page_offers_the_switch_and_marks_its_text():
    html = (STATIC / "index.html").read_text()
    assert 'id="lang-toggle"' in html and 'id="lang-signin"' in html
    assert html.count("data-i18n") >= 30
    assert "fonts.googleapis.com/css2?family=Vazirmatn" in html


def test_css_has_no_physical_sides_outside_the_tooltip():
    css = (STATIC / "app.css").read_text()
    lines = [l for l in css.splitlines() if re.search(r"\b(margin|padding|border)-(left|right)\b|text-align:\s*(left|right)|(^|[;{\s])(left|right):", l)
             and not l.lstrip().startswith("#tip")]
    assert lines == []
```

Then add to `tests/test_web.py`:

```python
async def test_dashboard_csp_allows_google_fonts(env):
    gw, *_ = env
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/dashboard")
    csp = dict(d.strip().split(" ", 1) for d in r.headers["content-security-policy"].split(";"))
    assert "https://fonts.googleapis.com" in csp["style-src"] and "https://fonts.gstatic.com" in csp["font-src"]
```

Match the fixture and client names to the ones `tests/test_web.py` already imports. If it uses another client
helper, use that one.

- [ ] **Step 2: Run them, expect FAIL.** `pytest tests/test_i18n.py tests/test_web.py -q`

- [ ] **Step 3: Mark up `index.html`.**
  - Add `<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@100..900&display=swap">` before `app.css`.
  - Give every text-bearing element `data-i18n="login.<name>"` or `data-i18n="app.<name>"`, keeping its English
    content as is. Name keys after the element ids or the role: `login.about_title`, `login.about_lede`,
    `login.auth_title`, `login.auth_lede`, `login.step1`, `login.step2`, `login.step3`, `login.os_unix`,
    `login.os_windows`, `login.privacy`, `login.pricing`, `login.title`, `login.signup_hint`, `login.other_ways`,
    `login.key_hint`, `login.admin_hint`, `login.submit`, `login.submit_key`, `app.title`, `app.pricing`,
    `app.theme`, `app.signout`, `app.credential`.
  - Where an element mixes text and child markup (steps 1–3, `#login-hint` with its `?` dot), wrap only the text
    in a `<span data-i18n>` so the tip dot and the `<pre>` survive `innerHTML`.
  - Attributes go through `data-i18n-attr`: `placeholder`, `aria-label` and `title` on the inputs, the copy
    button, the tablist, `#who` (`title:app.who_title`), `#pricing-btn`, `#theme-toggle` and `#logout`.
  - Make the key input `dir="ltr"`, as well as `.signin-cmd pre` and `#authorize-code`.
  - In the header, add `<button class="btn small" id="lang-toggle" data-i18n="app.lang_other" data-i18n-attr="title:app.lang_title">فارسی</button>` after `#theme-toggle`.
  - In `.signin-foot`, add ` · <button type="button" class="linklike" id="lang-signin" data-i18n="app.lang_other">فارسی</button>`.
  - Leave `<title>` as is. `app.js` sets `document.title = t("app.title")`.
- [ ] **Step 4: Write the `login.*` / `app.*` entries** in both dictionaries. `en` is the HTML text verbatim,
  including its `<b>`/`<code>` markup. `fa` is translated with the glossary. Example pair:
  `"login.about_title": "Use Claude Code on your team’s Claude subscription."` /
  `"login.about_title": "Claude Code را با اشتراک Claude تیم‌تان به کار ببرید."`.
- [ ] **Step 5: Wire it in `app.js`.** Near the top, after the helpers, add:

```js
applyStatic();
document.title = t("app.title");
$("#lang-toggle").onclick = $("#lang-signin").onclick = () => setLang(LANG === "fa" ? "en" : "fa");
```

  `.linklike` in `app.css` gets `{ background: none; border: 0; padding: 0; color: inherit; text-decoration: underline; cursor: pointer; font: inherit; }`.
  In `showTip`, the beside-the-item side prefers the start side of the reading direction. Replace the `side`
  computation with this:

```js
  const rtl = document.documentElement.dir === "rtl";
  const fitsR = r.right + gap + w <= vw - gutter, fitsL = r.left - gap - w >= gutter;
  const side = el.dataset.tipSide !== "right" ? null
    : rtl ? (fitsL ? "left" : fitsR ? "right" : null) : (fitsR ? "right" : fitsL ? "left" : null);
```

- [ ] **Step 6: Make the CSS logical.** Apply these changes, by today's line numbers:
  - 98: `border-inline-start`
  - 100: `margin-inline-end`
  - 107: `text-align: start`
  - 110: `text-align: end`
  - 149: `text-align: start`. If the rule has `padding-right`, change it to `padding-inline-end`.
  - 150: `inset-inline-end: 10px` in place of `right: 10px`
  - 157: `inset-inline-start: 9px`
  - 184, 257: `padding-inline-start`
  - 209: `margin-inline-end`
  - 223, 265: `border-inline-start`
  - 282: `margin-inline-start`
  - 289: `text-align: start`
  - 302: `padding-inline-start`
  - 314: `padding-inline: 16px`
  - 339: `margin-inline-start`
  - Leave `#tip::after` (293–299) physical: `showTip` positions it in pixels.
  - Font: after the `body` rule, add
    `html:lang(fa) body { font-family: Vazirmatn, system-ui, -apple-system, "Segoe UI", sans-serif; }` and
    `html:lang(fa) code, html:lang(fa) pre, html:lang(fa) .user-code, html:lang(fa) .signin-code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }`.
    If a monospace stack already exists in `app.css`, keep that one. Also add
    `bdi, [dir=ltr] { unicode-bidi: isolate; }`.
- [ ] **Step 7: Update the CSP** (`web.py` `SecurityHeaders.__init__`). In `style-src`, append
  `https://fonts.googleapis.com` after `'unsafe-inline'`. Add the directive `font-src 'self' https://fonts.gstatic.com;`
  after `img-src`.
- [ ] **Step 8: Run** `pytest tests/test_i18n.py tests/test_web.py tests/test_orders_web.py -q`. Expected: PASS.
- [ ] **Step 9: Commit**

```bash
git add src/claude_proxy/static/index.html src/claude_proxy/static/app.css src/claude_proxy/static/app.js src/claude_proxy/static/i18n.js src/claude_proxy/web.py tests/test_i18n.py tests/test_web.py
git commit -m "Dashboard i18n: sign-in and header text, language switch, RTL layout, Vazirmatn"
```

---

### Task 3: Locale-aware formatters, `bdi()`, Persian digits in number fields

**Files:**
- Modify: `src/claude_proxy/static/app.js:42-74` (formatters)
- Modify: `src/claude_proxy/static/i18n.js` (`dur.*`, `app.never`, `app.ago`)
- Test: `tests/test_i18n.py`

**Interfaces:**
- Consumes: `LOC`, `t` (Task 1).
- Produces:
  - `bdi(v): string` returns `<bdi>${esc(v)}</bdi>`.
  - `asciiDigits(s: string): string` maps `۰-۹` and `٠-٩` to `0-9` and `٫` to `.`.
  - `num(v): number` returns `Number(asciiDigits(String(v ?? "").trim()))`.
  - The formatters keep their names and signatures.

- [ ] **Step 1: Failing test.** Add to `tests/test_i18n.py`:

```python
FMT = r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const app = fs.readFileSync(process.argv[2], "utf8");
const i18n = fs.readFileSync(path.join(path.dirname(process.argv[2]), "i18n.js"), "utf8");
const grab = (re) => { const m = re.exec(app); if (!m) throw new Error("not found: " + re); return m[0]; };
const block = grab(/^\/\/ i18n-formatters:start[\s\S]*?^\/\/ i18n-formatters:end$/m);
const ctx = { console, Intl, Date, Math, Number, String, localStorage: { getItem: () => process.argv[3] } };
vm.runInNewContext(i18n + "\n" + block + "\n;this.out = [fmtNum(24900000), fmtUsd(12.5), fmtDur(9000), num('۲۵'), num('٣٫5'), bdi('<a>')];", ctx);
console.log(JSON.stringify(ctx.out));
"""


def _fmt(tmp_path, lang):
    if not shutil.which("node"):
        pytest.skip("no node here")
    h = tmp_path / "fmt.js"
    h.write_text(FMT)
    return json.loads(subprocess.run(["node", str(h), str(STATIC / "app.js"), lang], capture_output=True, text=True, timeout=30, check=True).stdout)


def test_formatters_follow_the_language(tmp_path):
    assert _fmt(tmp_path, "en") == ["24.9M", "$12.50", "2h 30m", 25, 3.5, "<bdi>&lt;a&gt;</bdi>"]
    fa = _fmt(tmp_path, "fa")
    assert fa[1] == "$۱۲٫۵۰" and fa[2] == "۲ ساعت و ۳۰ دقیقه" and fa[3] == 25 and fa[4] == 3.5
    assert re.search(r"[۰-۹]", fa[0])
```

- [ ] **Step 2: Run it, expect FAIL** (no markers). `pytest tests/test_i18n.py -q -k formatters`
- [ ] **Step 3: Rewrite the formatter block.** Wrap it in marker comments. `esc` must sit inside the block,
  because `bdi` uses it: move the `esc` line down into the block. Leave `const $` above it.

```js
// i18n-formatters:start
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const bdi = (v) => `<bdi>${esc(v)}</bdi>`;   // a name, id or address inside translated text keeps its own direction
// Persian keyboards type ۰-۹ (and Arabic ones ٠-٩); number fields read them as 0-9.
const asciiDigits = (s) => s.replace(/[۰-۹]/g, (d) => d.charCodeAt(0) - 0x6F0).replace(/[٠-٩]/g, (d) => d.charCodeAt(0) - 0x660).replace(/٫/g, ".");
const num = (v) => Number(asciiDigits(String(v ?? "").trim()));
const nf = new Intl.NumberFormat(LOC, { maximumFractionDigits: 1, notation: "compact" });   // 24.9M, never "24.9m" (minutes?)
const nfFull = new Intl.NumberFormat(LOC);
const nfFix = (d) => new Intl.NumberFormat(LOC, { minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: false });
const nf0 = nfFix(0), nf1 = nfFix(1), nf2 = nfFix(2);
function fmtNum(v) { return v == null ? "—" : nf.format(v); }
function fmtUsd(v) { return v == null ? "—" : v === 0 ? `$${nf0.format(0)}` : v < 0.01 ? `<$${nf2.format(0.01)}` : "$" + (v >= 100 ? nf0 : nf2).format(v); }
const fmtPrice = (v) => `$${Number.isInteger(v) ? nf0.format(v) : nf2.format(v)}`;   // a USD price: whole dollars bare, otherwise cents
const fmtShare = (v) => `${new Intl.NumberFormat(LOC, { maximumFractionDigits: 2 }).format(+v)}%`;   // 6.1000000000000005 reads 6.1%
function fmtPct(v, d = 0) { return v == null ? "—" : `${nfFix(d).format(v)}%`; }
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  const u = (n, unit) => t(`dur.${unit}`, { n: nf0.format(n) });
  if (s < 60) return u(s, "s");
  // Two largest units, e.g. "2h 30m"; the second is dropped when it is zero.
  const [big, bigU, small, smallU] = s < 3600 ? [Math.floor(s / 60), "m", s % 60, "s"]
    : s < 86400 ? [Math.floor(s / 3600), "h", Math.floor(s % 3600 / 60), "m"]
    : [Math.floor(s / 86400), "d", Math.floor(s % 86400 / 3600), "h"];
  return small ? t("dur.join", { a: u(big, bigU), b: u(small, smallU) }) : u(big, bigU);
}
function fmtAgo(t_) { return t_ ? t("app.ago", { d: fmtDur(Date.now() / 1000 - t_) }) : t("app.never"); }
function fmtTime(t_) { return t_ ? new Date(t_ * 1000).toLocaleString(LOC, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"; }
const fmtDate = (t_) => (t_ ? new Date(t_ * 1000).toLocaleString(LOC, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const money = (amount, currency) => { try { return new Intl.NumberFormat(LOC, { style: "currency", currency }).format(amount); } catch { return `${nf2.format(amount)} ${currency}`; } };
// i18n-formatters:end
```

  `toLocal`/`fromLocal` stay outside the block, unchanged. `fmtPct`'s English output must still match
  `toFixed`: `nfFix(d)` with `useGrouping: false` gives `"12%"`, `"12.5%"`. The parameter is named `t_` so it
  doesn't shadow `t()`. Rename the same way wherever a function parameter is named `t` and the body now calls
  `t(...)`.
- [ ] **Step 4: Dictionary entries.**
  - en: `"dur.s": "{n}s", "dur.m": "{n}m", "dur.h": "{n}h", "dur.d": "{n}d", "dur.join": "{a} {b}", "app.ago": "{d} ago", "app.never": "never"`.
  - fa: `"dur.s": "{n} ثانیه", "dur.m": "{n} دقیقه", "dur.h": "{n} ساعت", "dur.d": "{n} روز", "dur.join": "{a} و {b}", "app.ago": "{d} پیش", "app.never": "هرگز"`.
- [ ] **Step 5: Read number fields through `num()`.** Run `grep -nE "Number\(|parseFloat|parseInt|\+f\.get|\+[a-z]+\.value|valueAsNumber" src/claude_proxy/static/app.js`.
  Every hit that reads a form field or an input becomes `num(...)`. Inputs that hold numbers change from
  `type="number"` to `type="text" inputmode="decimal" dir="ltr"`, keeping their `min`/`step` checks in JS where
  they validate today. Chrome's number input drops Persian digits before JS sees them.
- [ ] **Step 6: Run** `pytest tests/test_i18n.py tests/test_tickets_web.py tests/test_orders_web.py -q`. Expected: PASS.
- [ ] **Step 7: Commit** `git commit -m "Dashboard i18n: locale formatters, Persian digits, bdi()"` with the two static files and the test.

---

### Tasks 4–7: Convert `app.js` strings, section by section

These four tasks follow the same rules. The rules are repeated here so that each task can be read alone.

**Conversion rules (every task 4–7):**
1. Every user-visible English literal in the task's line range becomes `t("<area>.<name>")`. This covers template
   text, `esc("…")` arguments, `title=`/`aria-label=`/`placeholder=` values, `confirm`/`alert` text, button
   labels, banner text and dialog headings. Add the `en` entry with today's exact text and the `fa` entry in the
   same commit.
2. Interpolated values become placeholders: `` `It asked ${fmtAgo(x)} from ${esc(ip)}` `` becomes
   `t("auth.asked", { ago: fmtAgo(x), ip: bdi(ip) })`, with `"auth.asked": "It asked {ago} from {ip}"`. Persian
   word order may move the placeholders.
3. Data values shown as text, such as user names, emails, model ids, key prefixes, IPs, scopes, labels and order
   messages, go through `bdi(v)` instead of `esc(v)`. Values inside attributes keep `esc`. Code, commands and
   keys stay in `<code>`, which is LTR-isolated by the CSS rule `bdi, [dir=ltr]` plus a new rule
   `code { unicode-bidi: isolate; direction: ltr; }` that Task 4 adds.
4. Where English pluralises today (`${n} request${n === 1 ? "" : "s"}`), use `plural("<key>", n, { n: fmtNum(n) })`
   with `.one`/`.other` entries in both languages. Persian `.one` and `.other` are the same text.
5. Lookup tables of labels (`METRICS`, `USER_LABELS`, the `label` in `VIEWS`, status and kind names) stop holding
   English. They hold keys, or the label is computed with `t(`prefix.${k}`)`. Code that compares or stores
   these values keeps comparing the internal ids, never the translated label.
6. Internal sentinel strings stay English and are never shown: `"signed out"`, `OTHER` (`"Other"`) and `NO_MODEL`
   (`"no model"`). Where they are shown, show `t("chart.other")` / `t("chart.no_model")`.
7. Don't translate API values, URLs, `data-*` values, CSS classes, `localStorage` keys or `console.warn` text.
8. After each task, run `grep -nE '"[A-Z][a-z]+ [a-z]+|>[A-Z][a-z]+[ <.]' src/claude_proxy/static/app.js` over the
   task's range. Each remaining hit must be a code comment, an internal id or a brand name.

**Test per task:** `test_every_key_the_page_uses_is_defined` and `test_both_languages_have_the_same_keys_and_placeholders`
(Task 1) enforce the dictionaries, and `test_app_js_holds_no_persian` keeps Persian out of `app.js`. For each
task, run `pytest tests/test_i18n.py tests/test_tickets_web.py tests/test_orders_web.py tests/test_web.py -q`
and `node --check src/claude_proxy/static/app.js`. Then open the affected tabs in the local gateway (Task 10's
setup) in `fa` and `en` and check the text renders: no `undefined`, no dotted keys.

### Task 4: Tooltips, error labels, limit labels (`app.js:82-486`)

**Files:** Modify `static/app.js` (`TIPS`, `KIND_TIPS`, `KIND_NOTE`, `UNIT_TIPS`, `KIND_SHORT`,
`LIMIT_PLACEHOLDER`, `ERROR_TIPS`, `ERROR_LABELS`, `USER_TIPS`, `tipI`'s default label, `METRICS`, `USER_LABELS`,
`limitLabel`, `limitValue`, `limitValueTip`, `limitsBlock`, `tableView` titles where passed literally), `static/i18n.js`, `static/app.css`.

**Interfaces:**
- Produces: the same table names, now filled from the dictionaries, so callers don't change:
  - `TIPS[k] = t("tip." + k)`, `KIND_TIPS[k] = t("kind." + k)`, `ERROR_LABELS[k] = t("elabel." + k)`, and so on.
  - Each table is built from an id list:

```js
const TIP_IDS = ["login_key", "requests", "weighted", /* … every current TIPS key, in today's order … */];
const TIPS = Object.fromEntries(TIP_IDS.map((k) => [k, t(`tip.${k}`)]));
```

  Copy the list from today's object keys verbatim. The markup moves into the `en` dictionary unchanged, so
  `{ref}` and `{stale}` keep working through `fillTip`. Do the same with `KIND_TIPS` (`kind.`), `KIND_NOTE`
  (`kindnote.` with `window`, `share`, `total`), `UNIT_TIPS` (`unit.`), `KIND_SHORT` (`kshort.`),
  `ERROR_TIPS` (`etip.`), `ERROR_LABELS` (`elabel.`, including `usage_limit` and `gateway_unavailable`) and
  `USER_TIPS` (`utip.`). `USER_TIPS["kind:tokens_5h"]` interpolates `KIND_NOTE.window`, so it becomes
  `t("utip.kind:tokens_5h", { note: KIND_NOTE.window })`.
  - The generated kind header `${k.replace(/_/g, " ")}` becomes `t("kname." + k)`. Add `kname.*` for all 14 kinds:
    en is today's spaced id (`"requests minute"`) and fa a short label (`"درخواست در دقیقه"`). `limitLabel`
    uses `kname.*` too, and `USER_LABELS` becomes `t("kname.5h_limit")`, etc. `cost_total` shows
    `t("kname.cost_total")` = "credit" / "اعتبار".
  - `METRICS[k]` becomes `t("metric." + k)`.
  - `LIMIT_PLACEHOLDER` keeps the digits and turns "e.g." into `t("lim.eg", { v: "200" })`.

- [ ] Step 1: Convert the tables as above, then `limitValue`/`limitValueTip`/`limitsBlock` by the rules. Typical
  values: `"lim.not_measured"`, `"lim.used_pct"`, `"lim.resets_in"`, `"lim.skipped"`, `"lim.est"`, `"lim.est_nolive"`,
  `"lim.none"`.
- [ ] Step 2: Add `code { unicode-bidi: isolate; direction: ltr; }` to `app.css`.
- [ ] Step 3: Run the per-task test commands, then commit: `"Dashboard i18n: tooltips, limit and error labels"`.

### Task 5: Shell, overview, usage, quota, models, activity, sessions, errors, audit

**Ranges:**
- `app.js:487-655`: `VIEWS`, `renderTabs`, `render`, `credentialPill`, `renderOverview`, `usersCard`,
  `userLimitsCard`, `pickedLine`, `priceListCard`
- `app.js:712-751`: `quotaBar`, `renderUsage`
- `app.js:1527-1683`: `renderQuota`, `renderModels`, `renderActivity`, `heatmap`, `renderSessions`,
  `sessionCell`, `sessionsTable`, `renderErrors`, `auditDetail`, `renderAudit`

**Files:** `static/app.js`, `static/i18n.js`, `tests/test_orders_web.py:396`, `tests/test_tickets_web.py:584`.

- [ ] Step 1: Remove `label` from `VIEWS`. Labels come from `t(`tab.${k}`)` in `renderTabs`, plus a `tab.*`
  entry for each of the 14 views. Change the assertion in `tests/test_orders_web.py` to
  `'orders: { render: renderOrders, admin: true, feature: "tickets" }' in js`.
- [ ] Step 2: Convert the ranges by the rules. `seg(...)` option labels become `t()` calls. `"Loading…"` becomes
  `t("app.loading")`. The weekday and hour labels in `heatmap` come from `Intl.DateTimeFormat(LOC, { weekday: "short" })`
  over a fixed week, with no dictionary entries. `pickedLine` must still give exactly
  `You picked <b>Lite</b> for <b>a week</b>. ` in English: key `"price.picked": "You picked <b>{tier}</b> for <b>{length}</b>. "`
  and the length words under `price.len.week`, etc.
- [ ] Step 3: Run the per-task tests (`test_dashboard_names_only_a_valid_pick` included) and commit: `"Dashboard i18n: overview, usage and reporting tabs"`.

### Task 6: Users, user detail, limits dialog, dialogs (`app.js:752-806`, `1079-1167`, `1328-1526`)

**Functions:** `renderUsers`, `wireUserActions`, `userRow`, `userActions`, `openDialog`, `keyDialog`, `showWho`,
`nameDialog`, `addUserDialog`, `userAction`, `deleteDialog`, `upgradeDialog`, `confirmInline`, `alertInline`,
`infoInline`, `tipSelect`, `limitsDialog`, `renderUser`.

- [ ] Step 1: Convert the functions by the rules. `deleteDialog` asks the admin to type the user's name. The
  comparison stays on the raw name, and the prompt text is translated.
- [ ] Step 2: Every number input in `limitsDialog`, `upgradeDialog` and `addUserDialog` is read with `num()` (see
  Task 3, Step 5).
- [ ] Step 3: Run the per-task tests and commit: `"Dashboard i18n: users, limits and dialogs"`.

### Task 7: Tickets, orders, pricing, buyer side, computers, authorize, sign-in and session (`app.js:655-711`, `804-1078`, `1168-1327`, `1684-1791`)

**Functions:** `myOrderCard`, `orderCurrencies`, `orderFormHtml`, `orderDialog`, `wireOrdering`, `stateBadge`,
`renderTickets`, `ticketRow`, `ticketAction`, `grantDialog`, `bonusDialog`, `renderOrders`, `orderRow`,
`mailState`, `orderActions`, `orderAction`, `orderNoteDialog`, `linkDialog`, `renderPricing`, `machinesCard`,
`wireMachines`, `renderAuthorize`, `signinSteps`, `showClerk`, the `ADMIN_PAGE` hint, `showLogin`, `login`.

**Files:** also `e2e/run.py:255`.

- [ ] Step 1: `stateBadge(s)` shows `t("state." + s)`, but its `class="state-${s}"` keeps the raw id. The
  `test_order_rows_escape_every_buyer_field` and `test_order_actions_follow_the_status_table` tests still pass,
  because `APP_STUBS` stubs `stateBadge`.
- [ ] Step 2: In `renderAuthorize`, the finished card's heading gets `id="az-done"`:
  `` `<h2 id="az-done">${title}</h2>…` ``. Its titles are `t("auth.done")` ("Authorized" / "تأیید شد") and
  `t("auth.cancelled")`. In `e2e/run.py`, replace line 255 with
  `expect(page.locator("#az-done")).to_have_text("تأیید شد", timeout=15000)`. The e2e browser runs the
  default language, so it now exercises Persian.
- [ ] Step 3: Convert the rest by the rules. The ADMIN_PAGE hint becomes `$("#login-hint span[data-i18n]").textContent = t("login.admin_hint")`.
- [ ] Step 4: Run the per-task tests (`tests/test_orders_web.py` and `tests/test_tickets_web.py` in full) and commit:
  `"Dashboard i18n: tickets, orders, computers and sign-in"`.

---

### Task 8: Server error messages in Persian

**Files:** `static/i18n.js` (`ERR_FA`, `ERR_PATTERNS`), `static/app.js` (`api()`), `tests/test_i18n.py`.

**Interfaces:** Consumes `errMsg` (Task 1). Produces `Error.message` already translated, and `e.raw` holding the server's English.

- [ ] **Step 1: Failing test**

```python
import ast


def _fail_messages():
    exact, built = set(), []
    for py in (STATIC.parent).glob("*.py"):
        for node_ in ast.walk(ast.parse(py.read_text())):
            if isinstance(node_, ast.Call) and getattr(node_.func, "id", None) == "fail" and len(node_.args) > 1:
                a = node_.args[1]
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value[:1].isupper():
                    exact.add(a.value)
                elif isinstance(a, ast.JoinedStr):
                    built.append("".join(v.value if isinstance(v, ast.Constant) else "X1" for v in a.values))
    return exact, built


def test_every_server_message_reads_in_persian(tmp_path):
    d = node(tmp_path)
    exact, built = _fail_messages()
    assert sorted(m for m in exact if m not in d["ERR_FA"]) == []
    pats = [re.compile(src) for src, _ in d["ERR_PATTERNS"]]
    assert [m for m in built if not any(p.search(m) for p in pats)] == []


def test_an_unknown_message_shows_as_sent(tmp_path):
    out = node(tmp_path, "fa", 'this.out = [errMsg("Brand new message."), errMsg(""), errMsg(undefined)];')
    assert out == ["Brand new message.", "", None]
```

  The second test expects `[…, "", None]`, because `JSON.stringify` turns `undefined` into `null`.
  `errMsg` returns falsy input as is.

- [ ] **Step 2: Run, expect FAIL** (empty map).
- [ ] **Step 3: Fill `ERR_FA`.** Run `pytest tests/test_i18n.py -k server_message` to see the full list of
  missing texts. Add one entry per text, translated with the glossary. Examples:
  `"Not signed in.": "وارد نشده‌اید."`,
  `"Unknown, disabled or revoked key.": "کلید ناشناخته، غیرفعال یا ابطال‌شده است."`,
  `"Wrong username or password.": "نام کاربری یا گذرواژه نادرست است."`,
  `"Admin only.": "فقط برای مدیر."`.
- [ ] **Step 4: Fill `ERR_PATTERNS`** for the f-string messages. In each pattern, `X1` in the test stands for
  each value, so match values with `(.+?)`. Example:

```js
const ERR_PATTERNS = [
  [/^No user '(.+?)'\.$|^No user (.+?)\.$/, "کاربری با نام {1}{2} نیست."],
  [/^Too many (.+?)\. Try again in (\d+)s\.$/, "{1} بیش از حد. {2} ثانیهٔ دیگر دوباره تلاش کنید."],
  [/^User '(.+?)' already exists\.$|^User (.+?) already exists\.$/, "کاربر {1}{2} از قبل وجود دارد."],
  [/^Sign-in failed: (.+)\.$/, "ورود ناموفق بود: {1}."],
  /* … one per remaining f-string message the test lists … */
];
```

  A group that didn't match is `undefined`, and `errMsg` (Task 1) already fills it with `""`. Values (names, emails) are inserted as text: the callers already `esc()` the
  message.
- [ ] **Step 5: Use it in `api()`.** Replace the `!r.ok` line with:

```js
  if (!r.ok) { const raw = (data && data.error) || `HTTP ${r.status}`; throw Object.assign(new Error(errMsg(raw)), { status: r.status, data, raw }); }
```

  Grep for `e.message ===` / `e.message !==`: only the `"signed out"` sentinel is compared, and it doesn't go
  through `errMsg`.
- [ ] **Step 6: Run** `pytest tests/test_i18n.py -q`. Expected: PASS. Then commit: `"Dashboard i18n: server messages in Persian"`.

---

### Task 9: Charts and Clerk in Persian

**Files:** `static/app.js` (`baseOption`, `timeLabel`, `axisLabels`, `stackedTime`, other `setOption` calls,
`setupClerk`), create `static/clerk-fa-IR.js`, `static/i18n.js` (`chart.*`), `tests/test_i18n.py`.

- [ ] **Step 1: Failing test**

```python
def test_clerk_persian_is_vendored_and_loaded_only_for_persian():
    vend = (STATIC / "clerk-fa-IR.js").read_text()
    assert "export { faIR }" in vend and "@clerk/localizations@4.21.2" in vend.splitlines()[0]
    js = (STATIC / "app.js").read_text()
    assert 'import("/static/clerk-fa-IR.js")' in js and "localization" in js
```

- [ ] **Step 2: Vendor the file**

```bash
{ echo '// @clerk/localizations@4.21.2 dist/fa-IR.mjs (MIT, Clerk Inc.), vendored: Clerk'"'"'s Frontend API does not serve it.'; \
  curl -fsSL https://cdn.jsdelivr.net/npm/@clerk/localizations@4.21.2/dist/fa-IR.mjs; } > src/claude_proxy/static/clerk-fa-IR.js
tail -1 src/claude_proxy/static/clerk-fa-IR.js   # expect: export { faIR };
```

- [ ] **Step 3: Load it in `setupClerk`.** Before `await window.Clerk.load(...)`, add:

```js
    const localization = LANG === "fa" ? await import("/static/clerk-fa-IR.js").then((m) => m.faIR, () => undefined) : undefined;
    await window.Clerk.load({ ui: { ClerkUI: window.__internal_ClerkUICtor }, ...(localization && { localization }) });
```

- [ ] **Step 4: Charts.**
  - `baseOption`: `textStyle.fontFamily` becomes `getComputedStyle(document.body).fontFamily`. Under RTL, `tooltip`
    gets `extraCssText: "direction: rtl; text-align: right;"`.
  - `timeLabel`/`axisLabels`: `toLocaleString(undefined, …)` and the like become `(LOC, …)`.
  - `stackedTime` series `name`: `k === OTHER ? t("chart.other") : k === NO_MODEL ? t("chart.no_model") : k`. Do the
    same for the legend `data`.
  - Every literal series, axis or `name` string in the other `setOption` calls (`renderQuota`, `renderModels`,
    `renderActivity`, `renderUser`) becomes `t("chart.*")`.
- [ ] **Step 5: Run** `pytest tests/test_i18n.py -q` and `node --check src/claude_proxy/static/app.js`. Then commit:
  `"Dashboard i18n: charts and Clerk sign-in in Persian"`.

---

### Task 10: Full pass in a real browser

**Files:**
- Create: `scripts/i18n_scan.py` (kept as a reusable check)
- Fix whatever the pass finds in `app.js` / `i18n.js` / `app.css`

- [ ] **Step 1: Start a local gateway with data.** In a temp dir:
  - Write a config from `config.example.toml`, with `dashboard_host = "127.0.0.1"`, a free port and a temp DB, and `[tickets] enabled = true`.
  - Run `claude-proxy init` to create the admin.
  - Run `claude-proxy user add alice` and `claude-proxy user add "علی رضایی"`.
  - Run `python e2e/seed.py`-style inserts, or send a few requests through `e2e/fake_anthropic.py`, so the charts have points.
  - Then run `claude-proxy serve`.
  Never point this at the production gateway.
- [ ] **Step 2: Write `scripts/i18n_scan.py`** (Playwright). For a base URL, admin password and user key, it:
  - signs in, as the admin and as the user;
  - visits each tab hash (`#overview #usage #users #user/1 #tickets #orders #pricing #quota #models #activity #sessions #errors #audit`) in `fa` (default) and `en`, setting `localStorage cp-lang` before load;
  - in `fa`, walks the text nodes under `#app` and `#login`, skipping `code, pre, bdi, [dir=ltr], script, style, .tsel-list` and `[data-tip]` attributes. It reports every node matching `/[A-Za-z]{3,}/` after removing an allowlist (`Claude|Anthropic|Opus|Sonnet|Haiku|gclaude|claude-gateway|claude-proxy|OpenCode|Muse|Meta|Google|GitHub|Clerk|API|USD|EUR|IRR`);
  - in both languages, reports text matching `/\b[a-z]+\.[a-z_]+(\.[a-z_]+)*\b/` that equals a dictionary key (a raw key), `undefined`, `NaN` and console `i18n: missing` warnings;
  - checks `document.documentElement.scrollWidth <= innerWidth` at 1280 and 375 wide;
  - opens each row-action dialog once and scans it the same way;
  - saves screenshots to a temp folder;
  - exits non-zero when anything was reported.
- [ ] **Step 3: Run it and fix until clean.** `uv run --with playwright python scripts/i18n_scan.py --base http://127.0.0.1:<port> --admin-password … --user-key …`.
  Expected at the end: `0 findings`, exit 0.
- [ ] **Step 4: Look at the screenshots.** Check `fa` light and dark, desktop and 375px. Check that:
  - the tables mirror;
  - number columns sit at the end;
  - Latin names inside Persian sentences keep their punctuation;
  - the tooltip opens to the start side;
  - the sign-in panel is on the left in RTL;
  - the header switch reads `English` in fa and `فارسی` in en.
  Then click the switch on `#authorize/ABCD-EFGH` while signed out, and confirm the code still shows after the reload.
- [ ] **Step 5: Run the whole suite.** `pytest -q`. Expected: everything passes, with the same skips as before.
- [ ] **Step 6: Commit** `scripts/i18n_scan.py` and the fixes: `"Dashboard i18n: browser scan and fixes"`. Push `master`.
