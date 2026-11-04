# Passes are sold in points of the week, against Anthropic's reported usage

- **Date:** 2026-11-04
- **Status:** proposed.
- **Builds on:** `2026-10-03-paid-tickets-design.md` §5 (capacity), `2026-10-13-ticket-weekly-pieces.md` §1.1 (the
  prorated piece), `2026-10-18-gateway-capacity.md` (capacity scaling), `2026-10-30-pass-borrow-across-reset.md` §1
  (free room).
- **Overrides:** 10-03 §5, "The rule": `sold(t) + share ≤ max_sold_pct` at every moment. That rule compares rates
  (shares). This spec sells points. 10-03 §8 "Capacity" panel and §9 "Sold out" change with it. 10-18 §4's non-goal
  ("grants still check sold + share against the static ceiling") is withdrawn. 10-30 §1 "Owed" loses its hand-set
  share term (§5 here).
- **Problem:** on 2026-11-04 the account read 42% used for the week, so 58 points were free until Friday's reset, yet
  the dashboard said 100% sold and the home page showed every pass as sold out. One user held four day passes at
  once (20 + 20 + 20 + 40 = 100% of share), and the rule adds shares at an instant. But a 20% day pass may draw only
  20 ÷ 7 ≈ 2.9 points of the week; the four together at most 14.3. Every pass sold since the reset came to 44.8
  points, of which 42.2 were used. The rule cannot see the points of the days already gone, and those expire at the
  reset unsold.
- **Goal:** the gateway sells what Anthropic says is left of the week and what no running pass is still owed, and
  never the same point twice. A 20% day pass costs 2.9 points of the week it runs in; seven in a row cost 20.

## 1. Terms

- **Point:** one percent of the account's weekly bucket as Anthropic reports it (`utilization_pct` of the `7d`
  snapshots). Points are used up and come back only at the weekly reset.
- **Share:** a pass's rate, `share_pct`: the fraction of the weekly bucket it may draw per week. Unchanged.
- **Piece:** the points a pass takes from one Anthropic week: `share × days of that week it covers ÷ 7`, plus the
  same for each bonus share period (10-13 §1.1, `tickets.week_split`, `limits._pass_cap`). Pieces are stated
  unscaled; where they are compared with live usage they are scaled to the gateway's capacity (10-18) as the
  enforcement scales them.
- **Week:** the current Anthropic week is `tickets.week_bounds`; later weeks follow in 7-day steps.
- **U:** the account's weekly utilization from the newest `7d` snapshot. **Fresh** means the snapshot is younger
  than `quota.stale_after_s` and its `resets_at` is in the future.
- **Owed:** what the running and queued passes of the account may still draw from the current week: for each
  non-cancelled pass with time in the week, `max(0, its piece scaled to capacity − its debt (10-30 §3) − its use this
  week)`, use measured as `limits._week_use` measures it. Hand-set `share_7d` rows are **not** owed (§5).
- **Free room (F):** `max(0, 100 − U − Σ owed − M)` with `M = tickets.sell_margin_pct` (§7). Defined only while the
  weekly report is fresh; otherwise there is no free room (§3).

## 2. The selling rule

A pass (or a bonus, or a pass moved to a new place) with share `s`, 5-hour share `s5` and period `[a, b)` may be
sold when all three hold, with the passes it replaces (`next_start`, used-up ones) left out as today:

1. **This week fits.** Its piece in the current week, scaled to capacity, is at most `F`. A pass that starts in a
   later week has no piece here and this check passes.
2. **Every week fits.** For every Anthropic week it touches, the pieces of all non-cancelled passes and bonuses in
   that week plus its own piece there stay at or under `max_sold_pct`. For the current week the sum counts every pass
   that had time in it, ended or not: what was sold is sold. So `max_sold_pct` below 100 keeps the rest of every
   week for headroom users in points, as it kept it in rate before.
3. **The 5-hour bucket is not oversubscribed.** At every moment of its period the 5-hour shares of the passes and
   bonuses covering that moment, plus `s5`, stay at or under `max_sold_5h_pct` (§7). This is today's
   `check_capacity` with 5-hour shares and a ceiling of its own: passes running at once can empty the 5-hour bucket
   within hours whatever the week says. A bonus with extra days extends the check to the new end at the pass's 5-hour
   share, as today.

The period of a week or month pass touches several weeks; check 1 charges only its current-week piece and check 2
charges each later piece to its week. A day pass that crosses the reset is charged the same way; the borrowing of
10-30 then moves use between its two parts at run time, within the free room it measures then.

**Sold out** (10-03 §9, `/pricing`, the buyer's price list, order requests): a tier and length is sold out when a
pass starting now, or at the buyer's `starts_at`, fails any check, or when the weekly report is not fresh (§3).
`sold_out_later` follows the same rule at the later start.

**Grants** (`tickets.grant`, `preview`, bonuses, queue moves on cancel and bonus days): the same checks, inside the
same transactions, raising `CapacityError` with the check that failed: "This week has 12.3 points free and the pass
needs 14.3", "Week of 13.11 would hold 104% with this pass, over max_sold_pct", "5-hour shares would reach 120% at
04.11 15:56, over max_sold_5h_pct", or "No fresh usage report from Anthropic; try again in a few minutes". There is
no admin override: the poll (`quota.poll_idle_s`) brings a report within minutes.

## 3. Anthropic's report is the base

The gateway's own arithmetic decides only what is still owed to passes already sold. The amount of the week that is
gone comes from Anthropic alone, `U`, never from the gateway's token counts or rate estimates. When the gateway has
no fresh report, it does not estimate: nothing is for sale until the next snapshot. Reports arrive with every proxied
response and from the idle poll, so this is a wait of minutes, not hours.

`U` already includes what the passes used, the owner's use on claude.ai, and anything the gateway cannot attribute.
Owed subtracts each pass's own use from its piece, so a point is never counted both as used and as owed.

## 4. What is sold is sold

Selling against `F` reserves the sale: once a pass is granted, its unused piece is owed and leaves `F` at once, so a
second buyer cannot take the same points. `U` can still rise from outside use, which shrinks `F` for later sales but
never touches a pass already sold; at run time such shortfall is handled as 10-18 handles it, and `M` covers the
requests in flight when a sale is checked. Lowering `max_sold_pct` or `max_sold_5h_pct` below what is sold refuses
new grants until passes end; it never cancels anything (10-03 §4).

## 5. Borrowing and the free room are one function

10-30 §1 computes free room for borrowing with a term for hand-set `share_7d` rows of ungated users. That term is
removed: hand-set shares are headroom (10-03 §5, "headroom is not enforced on its users"), and with the rows in use
on the live gateway (100% and 93% on two users without passes) it made free room zero at all times, so borrowing
never happened. Selling and borrowing now call the same function with the same definition of owed; they differ only
in the margin they subtract (`sell_margin_pct` for sales, `borrow_margin_pct` for borrowing) and in `exclude`. The
capacity panel (§6) shows the hand-set shares' total so the admin can see what the headroom users could draw.

## 6. Capacity panel (dashboard, Passes tab)

The Capacity card replaces "Sold now" and "Peak, next 30 days" with:

- **This week** (resets `{weekday} {time}`): a stacked meter of 100 points: used (`U`, from Anthropic), owed to
  passes, margin, free. The figure beside it is `F`, "free to sell". Below: "Sold this week: `Σ pieces` of
  `max_sold_pct`" as a second meter. While the report is not fresh, the meter is grey and the figure reads "no fresh
  report; nothing on sale".
- **Next weeks:** for each Anthropic week within 30 days that has any piece, "`week start`: `Σ pieces` of
  `max_sold_pct`", the highest one as a meter. Nothing to show when no pass reaches past this week.
- **5-hour:** "`Σ 5h shares now` of `max_sold_5h_pct`" and the peak over 30 days, as the old two rows but with 5-hour
  shares and the new ceiling.
- **Headroom note:** "Hand-set weekly shares total `X%`; they are not reserved." Only when any ungated user has a
  `share_7d` row.

The Account utilization card stays. The tips `tip.capacity_sold` and `tip.capacity_util` and the keys `tk.sold_now`,
`tk.peak`, `tk.headroom` are rewritten in English and Persian; the grant form's availability line and the
`tk.sold_out_tip` wording name the failing check.

`GET /api/admin/capacity` returns `{week: {start, end, fresh, used, owed, margin, free, sold, max_sold_pct},
weeks: [{start, sold}], five_hour: {now, peak_30d, max_sold_5h_pct}, hand_set_7d_pct, utilization}`. Nothing of
this reaches non-admins; `/pricing` still shows only the sold-out flags.

## 7. Settings (`[tickets]`)

- `max_sold_pct` (default 80): now the ceiling on the points sold in any one Anthropic week (§2 check 2). Validation
  unchanged: above 0, at most 100, and every tier's share at most this (a tier's week pass takes its full share in
  points, so a share over the ceiling could never be sold).
- `max_sold_5h_pct` (new, default 100): the ceiling on the summed 5-hour shares of passes running at once (§2
  check 3). Above 0; it may exceed 100 for an admin who chooses to oversubscribe the 5-hour bucket. Every tier's
  5-hour share must be at most this.
- `sell_margin_pct` (new, default 5): `M` in §1. 0 to 100; 100 means nothing is for sale.
- `borrow_margin_pct`: unchanged.

The example config and the README's `[tickets]` table describe the three.

## 8. Where it lives

- New module `src/claude_proxy/capacity.py`, so neither `tickets.py` (1260 lines) nor `limits.py` grows:
  - `pieces(conn, cfg, share, starts_at, ends_at, now) -> dict[week_start, points]`, from `week_split`'s boundaries;
  - `sold_by_week(conn, cfg, account_id, now, exclude) -> dict[week_start, points]`: Σ pieces of the account's
    non-cancelled passes and bonuses per week, within 30 days;
  - `owed(conn, cfg, account_id, week, now, exclude) -> float`, using `_week_use`, `_pass_cap` and `_debt`, which
    move here from `limits` (`limits` imports them back);
  - `free_room(conn, cfg, account_id, now, margin, exclude) -> float | None`: None without a fresh report;
  - `check(conn, cfg, account_id, share, share_5h, starts_at, ends_at, exclude) -> None`: the three checks of §2,
    raising `CapacityError`; `verify(conn, cfg, account_id, starts_at, ends_at)` for changes already written, as
    `verify_capacity` is today;
  - `summary(conn, cfg, account_id, now) -> dict`: §6's payload.
- `tickets.check_capacity`, `verify_capacity`, `sold_out`, `capacity` become calls into `capacity`; `sold_at` stays
  for the 5-hour sum, reading `share_5h_pct` (falling back to `share_pct`) for passes and bonuses.
- `limits._free_room` is replaced by `capacity.free_room(…, margin=cfg.tickets.borrow_margin_pct, exclude=t["id"])`.
- `web.py`: `/api/admin/capacity` returns §6; the grant form, `/pricing`, the buyer's price list and `orders.py`
  need no change beyond the reason text they already pass through.
- `static/app.js` `renderTickets` and `i18n.js`: §6.
- `config.py`: §7; `config.example.toml`, README.

## 9. Tests (`tests/test_capacity_points.py`; `test_tickets_capacity.py` and `test_gateway_capacity.py` updated
where they assert the old rule)

1. Account at 42 with four running day passes (20, 20, 20, 40) that have used 3 points each: a 40% day pass sells;
   `F` ≈ 100 − 42 − (14.3 − 12) − 5 and the reason text, when it refuses, names the points.
2. The same passes with the account at 90: refused by check 1 with the points named.
3. Fresh report at 0 with a week pass already sold and unused: a second week pass of the same share refuses when the
   two pieces pass `max_sold_pct`, and sells with `max_sold_pct` raised.
4. A week pass starting on Thursday: its current-week piece is charged to `F`, its next-week piece to next week's
   sum; next week's sum alone can refuse it.
5. A pass starting next week needs no free room this week.
6. Sold points stay sold: a pass that ended two days ago at full use counts in this week's `Σ pieces`, and a pass sold
   just now leaves `F` by its whole piece before it is used.
7. Stale snapshot, no snapshot, or `resets_at` in the past: refused with the "no fresh report" reason; every tier is
   sold out on `/pricing`; the panel says so; a fresh snapshot lifts it.
8. 5-hour ceiling: four 20% passes and one 40% at once refuse a fifth 20% at the default; `max_sold_5h_pct = 150`
   admits it; a bonus with extra days is checked to its new end.
9. Hand-set `share_7d` rows of ungated users don't reduce `F`, and the panel reports their total.
10. Borrowing (10-30 tests 1–6) still passes through the shared function, now without the hand-set term; a borrowing
    pass's unused own limit is free for a sale and a sale's owed piece is held back from borrowing.
11. Queue moves on cancel and bonus days re-check with the three checks; a move that fails names the check.
12. `max_sold_pct` lowered below this week's `Σ pieces` refuses new grants and cancels nothing.
13. Validation: a tier's share over `max_sold_pct`, a 5-hour share over `max_sold_5h_pct`, `sell_margin_pct` outside
    0–100.
14. `/api/admin/capacity` payload; non-admins get 403 and `/pricing` leaks nothing new.

## 10. Non-goals

- No change to enforcement: pieces, 5-hour shares, borrowing limits and the serving order stay as specified.
- No selling of points the gateway merely estimates: when Anthropic's report is missing, nothing is sold.
- No change to what buyers see beyond the sold-out flags and reasons.
- Accounts beyond `account_id = 1`: every function takes `account_id`, as today, and nothing more.
