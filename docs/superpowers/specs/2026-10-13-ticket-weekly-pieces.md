# Ticket weekly pieces (approach A) + ticket terms

- **Date:** 2026-10-13
- **Status:** accepted. Supersedes the day-as-unit enforcement (`2026-10-03-paid-tickets-design.md` §§ 5, 7, 8; `2026-10-03-ticket-window-alignment-note.md` approach B).
  Approach A is the note's §2A with the note's §5 measurement: the cap moves to prorated weekly pieces, the
  token-counting model stays as today.
- **Builds on:** `2026-10-03-paid-tickets-design.md` (everything not redefined here stays),
  `2026-10-04-order-requests-design.md` (order mail flow), `2026-10-05-zarinpal-payments-design.md` (paid mail flow).
- **Goal:** a weekly ticket's share is spendable at the buyer's pace inside each Anthropic week (e.g. all of it in
  3 days), while a slice still draws at most its share from any one Anthropic week however tickets turn over.
  No real users exist yet, so live tickets need no migration: enforcement is built live on every request.

## 1. Enforcement

On every request from a ticket-gated user, `limits.evaluate` builds two share limits from the covering ticket,
in place of any `share_5h`/`share_7d` rows set for that user:

- `share_5h` = ticket share + active bonus share, against Anthropic's 5-hour window. **Unchanged**, including its
  known handover double-draw (alignment note §2:28); §2 re-acknowledges it.
- `share_7d` = the ticket's **prorated piece** of Anthropic's current weekly window, replacing `share_day`.

Nothing pins Anthropic's reset to a weekday or hour — `resets_at` comes from response headers / the usage poll —
so no string anywhere (terms, ticket card, 429) names a weekday or time except formatted from the actual `resets_at`.
The Monday 00:00 UTC in the earlier draft is removed.

### 1.0 `share_day` removal list

`share_day` was never hand-settable (`limits.UNITS` has only `share_5h`/`share_7d`, so `validate()` already rejects
it and no legacy rows can exist), but every producer/consumer goes: `limits._ticket_day`, the `share_day` branch of
`limits._describe`, `SHARE_LABELS["share_day"]`, `USER_KINDS["share_day"]`, `web._SHARE_PERIOD["day"]`, the
`period == "day"` branch of `web._status_line`'s admin formatting, `me_tickets.day_end` (→ `week_end`),
`app.js` `USER_LIMIT_KINDS`' `"today_limit"` + the `ov.today_ends`/`day_end` line (→ `ov.week_ends`/`week_end`),
`i18n.js` `kname.share_day` / `kname.today_limit` / `ov.today_ends` (both languages),
`quota.py:347` docstring ("for `share_day`" → weekly piece). Kept intentionally:
`tickets.active_bonuses`/`bonus_share` (the new cap prorates each active bonus separately),
`tickets.current_day` (display-only `week_end` fallback), `/api/admin/shares.per_day` (kept as the day-ticket
reference `share ÷ 7`, UI-relabeled "Day ticket"), `i18n.js` `home.per_day` (a price-list length, unrelated).

### 1.1 The cap

```
piece cap = ticket share × (days of this Anthropic week the ticket covers) ÷ 7
          + Σ active bonus share × (days of this week the bonus covers) ÷ 7
```

Days are fractional seconds ÷ 86400. The ticket period is `[starts_at, effective_end)` (bonus extra-days included);
a bonus period is its `[starts_at, ends_at)`; the week is §1.3. Only bonuses active **now** count: a future bonus
cannot be borrowed from, and a past bonus's draw stays drawn (its usage remains in `current`, so the week stays
accounted). Example, Lite `5%`, ticket Wed→Tue, reset Monday: `5 × 5/7 = 3.57%` spendable any time Wed–Sun,
`5 × 2/7 = 1.43%` any time Mon–Tue. A day ticket fully inside one week gets exactly `share ÷ 7` with no special
case; straddling the reset splits it. A ticket starting at the reset gets the full share in one week.

Bonus timing is safe but visible, and stays visible (dashboard §4, tests §6): only bonuses active **now** widen the
cap, while `current` keeps everything spent since the piece start. So a bonus that ends mid-week can leave the user
blocked for the rest of the week with no further use — bonus-period spend still counts against the now-narrower
ticket-only piece. Symmetrically, usage before a mid-week bonus starts eats into the bonus piece once it activates.
The week bar must show this honestly (over 100% until the reset), not reset early.

### 1.2 How usage is measured (unchanged model)

`current` = the user's weighted Anthropic tokens ended since `max(week_start, ticket starts_at)`, divided by the
7-day bucket's observed rate — the same estimator `share_day` used, only the window start moves from the ticket
day to the week piece. Same fallbacks in the same order: no usage + fresh snapshots → `0` (no rate needed, a young
account's first user is not locked out); no rate + fresh → `attributed_since` over the same window; no rate + stale
→ `NO_RATE`, requests refused 503 `ticket_no_data` with `Retry-After: 60`. Stale snapshots mark `no_live_data`
("estimated without live data") exactly as today.

### 1.3 The week boundaries

From the 7d snapshots' reset time (`attribution_cached`, bucket `7d`), recomputed from the latest attribution on
every evaluation — projections are never stored, and a fresh snapshot's `resets_at` always wins. The steps below
key on `resets_at` vs `now` **only**: a 2-hour-stale snapshot whose reset is still 6 days out gives perfectly known
week boundaries, and enforcement continues as a stale *estimate* per §1.2. Snapshot age (`stale_after_s`) affects
the measurement alone — never whether the week is known.

1. `resets_at` in the future: week = `[resets_at − 7d, resets_at)`, however old the snapshot.
2. `resets_at` passed: roll forward in 7-day steps until the end is past now (assumed fixed weekly cadence).
   When the next fresh snapshot lands with a different `resets_at` than the projection, the snapshot wins and the
   week is recomputed — handover accounting follows observed resets, never the projection.
3. No reset time ever reported (or no snapshots at all): the week is unknown → `NO_RATE`, requests refused 503
   `ticket_no_data` with `Retry-After: 60`, same as a young account today. There is deliberately no fallback to the
   observed window start: it is first-observed, not a reset, and rolling 7d from an arbitrary mid-week observation
   would misalign caps between tickets evaluated at different times.

### 1.4 Messages and status

- Exceeded `share_7d` → the usual 429 with `Retry-After` set to the **full** seconds-to-week-end (up to ~7 days;
  kept honest rather than capped — a client must not poll; the dashboard shows the same countdown):
  `"Weekly share of your <tier> ticket is used up. It resets at <formatted resets_at>."` where the time is the
  actual week end, e.g. `2026-10-19 00:00 UTC`, never a hardcoded weekday.
- Non-admin views: kinds `5h_limit` + `weekly_limit` (percent of the piece cap); status line reads
  `alice · 5h 0% (resets in …) · week 0% (resets in …)`. Admin views keep points + reset times.

## 2. Why it is safe

Per-week, per-slice: tickets in one slice never overlap in time, so their overlaps with any one Anthropic week add
up to at most 7 days. In integral form, with `sold(t)` the per-moment reserved share (§5 of the paid-tickets design):

```
Σ over the slice's tickets of share × overlap_days ÷ 7
  = (1/7) · ∫ over the week of sold_slice(t) dt  ≤  max_sold_pct
```

Bonus pieces sit inside their ticket's period and are reserved per moment like tickets, so the same integral covers
them. Spending is cumulative inside the piece (`current` never resets mid-week), so burst timing cannot exceed the
cap. The capacity rule (per-moment `sold(t)`) is unchanged and stays correct; `sold_out`, grants, cancels, queue
moves and bonus checks are untouched.

Explicitly out of scope, as before: the 5-hour window. `share_5h` is unchanged, so a handover inside one 5-hour
window can still draw twice from it (alignment note §2:28) — accepted in both approaches as a tiny fraction of any
ticket, bounding burst size only, never weekly totals.

## 3. Ticket terms (v1) and acceptance

Normative English text lives in `tickets.py` as `TERMS_VERSION = "v1"` / `TERMS_LINES`
(terms name the reset generically — the weekday is formatted from the live `resets_at` at display time, never stored):

```
Ticket terms (v1):
- Your weekly share refills at Anthropic's weekly reset; what you do not use by then is gone.
- Spend it at your own pace: the week's share may be used up in fewer days.
- A 5-hour share applies on top, against Anthropic's 5-hour window.
- Claude models only; anything else needs a separate cost limit.
```

- **Order dialog** (visitor `home.js` + signed-in `app.js`): required `"I accept the ticket terms"` tick showing the
  4 bullets; no tick, no order — the tick gates both the Send-request and Pay buttons. API contract:
  `POST /api/orders`, `POST /api/me/orders` **and** `POST /api/orders/pay` take `terms_version: "v1"`; the server
  checks **equality** against `tickets.TERMS_VERSION` — missing or stale →
  `400 "Ticket terms have changed; reload and accept the current terms."` (client checkbox alone is forgeable).
  Wiring: `orders.create(..., terms_version)` stores it (the `POST /api/orders/pay` path forwards the tick through
  `payments.start(..., terms_version)`); the `order_new` audit detail carries `{"terms_version": "v1", ...}`.
- **Admin grant** (offline bank-transfer path): read-only terms box + `"buyer accepted"` tick before a direct Grant;
  the tick sends `terms_version` (`tickets.grant(..., terms_version)`) with the same equality check and 400, and the
  `ticket_grant` audit detail carries it. A grant **with `order_id`** instead uses the order row's stored version and
  needs no new tick: what the buyer accepted at order time governs, so new versions apply to new orders only and an
  in-flight v1 order still grants cleanly after v2 ships.
- **Emails:** the buyer's order mail (`orders.buyer_mail_text`) and post-payment mail (`payments.mails`) append the
  terms verbatim. Admin mails unchanged. (Wired 2026-10-13; enforcement follows this spec.)
- Persian UI strings for the dialog/checkbox go in `i18n.js` with the implementation.

## 4. Dashboard and API

- **Signed-in ticket card:** `"Weekly share refills <formatted resets_at> · <piece>% of the week's budget until then"`,
  e.g. `Weekly share refills 2026-10-19 00:00 UTC · 3.57% of the week's budget until then`.
  `/api/me/tickets.current` reports `week_end` (Anthropic reset) instead of `day_end`; when the week is unknown
  (the §1.3 step-3 `NO_RATE` case) it falls back to the ticket-day end **display-only** — enforcement is a 503 then,
  the fallback never widens a limit.
- **Admin grant form:** one honesty line per grant from a `week_split(starts_at, ends_at)` helper applying the §1.1
  formula to every Anthropic week the period touches, e.g. `"Covers 2 weekly resets: 3.57% + 1.43%."` Rules: future
  week boundaries project forward from the current `resets_at` in 7-day steps (assumed cadence — the line is
  informational; enforcement always uses observed resets); values round to 2 decimals; queued starts use the
  projected starts; month tickets list all 4–5 pieces; with no `resets_at` at all the line reads
  `"Weekly split unavailable: no quota data yet."` Values round to 2 decimals, display only — enforcement keeps
  full precision (rounded pieces can sum to 4.99/5.01).
- **Signed-in price list:** the terms sentence under `how_to_buy`.
- **Public pricing page and home order prices:** unchanged — prices, sold-out badges, countdowns only.
- Usage estimates (§9 of the paid-tickets design): computation unchanged; the steady-pace hint gains one clause —
  `"…of steady use a day; the week's share may be spent in fewer days"` — since pieces (not days) are now the unit.
- Pricing shares card: `/api/admin/shares.per_day` stays `share ÷ 7` but is UI-relabeled "Day ticket" (the day-ticket
  piece); the card sub-text explains weekly pieces instead of a daily cap.

## 5. Data

- `orders.terms_version TEXT NOT NULL DEFAULT 'v1'`: add the column to the `orders` CREATE TABLE **and** to
  `db._migrate` (`if "terms_version" not in _columns(conn, "orders"): ALTER TABLE orders ADD COLUMN terms_version
  TEXT NOT NULL DEFAULT 'v1'`), because `CREATE TABLE IF NOT EXISTS` never alters existing dev/test DBs. New orders
  write the accepted version; no backfill needed — no real orders exist.
- No ticket migration: `share_pct` stays copied at grant; the kind change (`share_day` → `share_7d`) is live-built.
  `tickets.current_day` stays for the display-only `week_end` fallback.

## 6. Testing

pytest, following `tests/test_tickets_limits.py`:

- kinds built: `{"share_5h", "share_7d"}` replacing admin rows; piece-cap arithmetic (full-week, split-week,
  fractional-day overlaps, day-inside-week, straddling day, month-ticket 5-week split, bonus active/future/past).
- bonus ends mid-week with bonus-period spend above the ticket-only piece → blocked rest of week, week bar over 100%
  until the reset; bonus starts mid-week with pre-bonus spend eating the bonus piece.
- burst inside a piece allowed; spend blocked past the cap with the 429 + full-duration `Retry-After`.
- handover mid-week (A→B) draws at most the slice share from the shared week; same-user renewal gets a fresh piece.
- measurement fallbacks: fresh-zero, young-account attribution, stale estimate with rolled-forward week, `NO_RATE`
  503 when the week is unknown (no `resets_at` ever — including handover behavior under it), `no_live_data` marks.
- mails carry every `TERMS_LINES` entry and nothing the buyer typed (extends the existing evil-input tests).
- order/grant without the tick, or with a stale `terms_version`, refused (400 with the exact message); audits carry
  `terms_version`; Persian dialog strings render (both languages in `node`).
- `week_end` in `/api/me/tickets` (+ display-only fallback); status-line `week` wording; `node --check` on edited
  static files.

## 7. Rollout

No real users, tickets, or orders: deploy enforcement + terms UI + mails together, before the first real sale.
Docs to touch at implementation: paid-tickets design §§ 5, 7, 8 (day → piece), alignment note status (B → A).
