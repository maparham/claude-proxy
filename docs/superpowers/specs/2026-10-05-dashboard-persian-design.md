# Bilingual dashboard: Persian (default) and English

## Goal

The dashboard (`/dashboard`, `/admin`: `index.html` + `app.js` + `app.css`) shows in Persian by default, with a
switch to English. That covers the sign-in screen and every signed-in view. The home page (`home.html`), the
privacy page and server-rendered mail stay English for now.

## Decisions

| Question | Choice |
|---|---|
| Scope | Sign-in screen and signed-in dashboard |
| Default | Persian (`fa`, RTL) on a first visit |
| Switch | `فا` / `EN` button in the header and on the sign-in screen, saved in `localStorage` (`cp-lang`) |
| Numbers | Persian digits and the Persian (Solar Hijri) calendar via `Intl` with `fa-IR` |
| Latin islands | Keys, model names, commands, code, emails and URLs stay Latin and LTR |

## Strings: `static/i18n.js`

A new script loaded before `app.js`, served and versioned like it (`versioned("index.html", ("i18n.js", "app.js", "app.css"))`).
`applyStatic()` runs once from `app.js` at start-up.

- `const I18N = { en: {...}, fa: {...} }` is a flat map keyed by dotted ids (`tab.overview`, `login.title`, `tip.weighted`).
- `LANG` is `"fa"` or `"en"`. It is read from `localStorage` (`cp-lang`) inside try/catch and falls back to `"fa"`.
- `t(key, vars)` looks the key up in `I18N[LANG]`, then in `I18N.en`, then returns the key itself. `{name}`
  placeholders are filled from `vars`. Values are not escaped: callers pass already-escaped values, as they do
  in today's template literals.
- `TIPS` and `KIND_TIPS` in `app.js` become `t("tip.<key>")` lookups. Their trusted markup moves into both
  dictionaries unchanged.
- Labels inside data tables (`METRICS`, the `label` of each `VIEWS` entry, status and kind names) become keys too.
  Where code compares a label, it compares the key instead.

Plurals: Persian nouns don't change after a number, and English keeps today's wording through separate keys
(`n.requests.one` / `n.requests.other`) and a small `plural(key, n)` helper. It is used only where the code
pluralises today.

## Static HTML

Each element of `index.html` that carries text gets `data-i18n="key"`. Attributes are named in a
`data-i18n-attr="placeholder:key;aria-label:key"` list. The `applyStatic()` function in `i18n.js` fills them in on
load and again after a switch. The English text stays in the HTML as the no-JS fallback.

## Direction and layout

- `index.html` ships as `<html lang="fa" dir="rtl">`. `i18n.js` loads without `defer` in `<head>` and, when the
  saved choice is English, sets `lang="en" dir="ltr"` before the body paints.
- `setLang(lang)` saves the choice and reloads the page. The address (and so the open tab) is kept. A reload
  rebuilds the formatters, charts and the Clerk widget in the new language without any re-render code.
- The 24 `left`/`right` rules in `app.css` move to logical properties (`margin-inline-start`, `inset-inline-end`,
  `text-align: start`, `border-inline-start`). Icons with a direction (chevrons, arrows) get
  `[dir=rtl] … { transform: scaleX(-1) }`.
- Font: Vazirmatn from Google Fonts, used first only under `:lang(fa)`. The CSP adds
  `https://fonts.googleapis.com` to `style-src` and `https://fonts.gstatic.com` to `font-src`.
- Latin islands are `<bdi>` for inline values and `dir="ltr"` for `pre`/`code` blocks, the install command, and
  key and URL inputs (placeholder `sk-proxy-…`). User names and emails are always wrapped in `<bdi>`, so a Latin
  name inside a Persian sentence keeps its place.
- Tables keep their column order, and RTL mirrors them naturally. Numeric columns align to `end`.
- Tooltip and popover positioning reads `dir`, so they open on the start side in RTL.

## Numbers and dates

Every formatter in `app.js` (`nf`, `nfFull`, `fmtNum`, `fmtUsd`, `fmtPct`, `fmtShare`, `fmtDur`, `fmtAgo`,
`fmtTime`, `fmtDate`, `money`, `timeLabel`, `axisLabels`) takes its locale from `LOC` (`"fa-IR"` or `"en-US"`).

- `fmtUsd` and `fmtPrice` keep the `$` sign and format the digits with the locale's formatter.
- `fmtDur` units come from keys (`ث`/`د`/`س`/`ر` in Persian, `s`/`m`/`h`/`d` in English). The text "… ago"
  becomes `t("ago", {d})`.
- `<input type=datetime-local>` stays Gregorian, because the browser owns it. `toLocal`/`fromLocal` are unchanged.

## Charts (ECharts)

Series names, axis names, legend entries and tooltip text go through `t()` and the formatters. The axes stay LTR,
with time running left to right, as Persian finance and analytics UIs usually show it. The `textStyle.fontFamily`
follows the page font. `OTHER` ("Other") stays as the internal key, and only its displayed label is translated.

## Server messages

`api()` keeps `data.error` as the message. Before showing one, `errMsg(e)` checks a `err.*` map in the
dictionaries keyed by the exact English text, then an `ERR_PATTERNS` list of `[RegExp, key]` for the f-string
messages (`No user 'x'.`, `Too many … Try again in 12s.`), whose groups fill the key's `{1}`, `{2}`. Matches are
shown in Persian, and anything unknown is shown as-is (English). The map starts with the messages a user can hit: sign-in, key, limit, ticket and order errors.

## Clerk

Clerk's Frontend API host does not serve `@clerk/localizations`. The `faIR` object from
`@clerk/localizations@4.21.2/dist/fa-IR.mjs` (an ES module with no imports, ~100 KB) is vendored as
`static/clerk-fa-IR.js`. When `LANG` is `fa`, `setupClerk()` does `import("/static/clerk-fa-IR.js")` and passes
`localization: faIR` to `Clerk.load`. If the import fails, the widget shows in English, and sign-in still works.

## Tests

- `tests/test_i18n.py`:
  - `I18N.en` and `I18N.fa` have the same keys, and every `{placeholder}` is present in both.
  - Every `t("…")` key and every `data-i18n` key used in `app.js`/`index.html` exists in `I18N.en`.
  - No literal Persian text appears in `app.js` (it all lives in `i18n.js`).
- The existing node harnesses (`APP_FN_HARNESS`, `PICK_HARNESS`) load `i18n.js` first with `LANG = "en"`, so
  the current English assertions keep passing. String-presence checks on `app.js` that look for English labels
  (such as `label: "Orders"`) are updated to check for the key.
- CSP test: the page's CSP lists the Google Fonts hosts (`style-src` and `font-src`).
- Manual (Playwright, local gateway with seeded data): sign-in screen, Overview, Users, user detail, Sessions,
  Tickets/Orders and dialogs, each in `fa` and `en`, in light and dark, at desktop width and 375px. The check
  confirms no horizontal scroll, no stray English in `fa`, and Latin islands that read correctly.

## Out of scope

Home page, privacy page, emails, the `gclaude` CLI (Persian there is parked: terminals mix RTL and LTR badly),
and per-account language storage.
