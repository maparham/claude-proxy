# Passes are sold in points of the week, against Anthropic's reported usage

- **Date:** 2026-11-04
- **Status:** accepted (revision 3, implemented 2026-11; see the plan of the same name for the amendments made while implementing).
- **Builds on:** `2026-10-03-paid-tickets-design.md` §5 (capacity), `2026-10-13-ticket-weekly-pieces.md` §1.1 (the
  prorated piece), `2026-10-18-gateway-capacity.md` (capacity scaling), `2026-10-30-pass-borrow-across-reset.md` §1
  (free room).
- **Overrides:**
  - 10-03 §5, "The rule": `sold(t) + share ≤ max_sold_pct` at every moment compares rates (shares). This spec sells
    points. 10-03 §3's definition of `max_sold_pct` ("the most that tickets and bonuses together may reserve at any
    moment"), §8 "Capacity" panel, §9 "Sold out" and the §12 capacity tests that assert the per-instant sum change
    with it.
  - 10-03 §1, "never rejects a buyer for another ticket's use": a sale can now be refused because the week is used
    up, by passes or by anyone else. What stays is that a pass already sold is never cut back by a later sale.
  - 10-13 §2, "The capacity rule (per-moment sold(t)) is unchanged … grants, cancels, queue moves and bonus checks are
    untouched": they all change. 10-13's safety argument (a slice never draws more than its share from one week) now
    rests on check 2 below directly, not on the per-moment sum.
  - 10-18 §4's non-goal ("grants still check sold + share against the static ceiling") is withdrawn.
  - 10-30 §1 "Owed" loses its hand-set share term (§5 here).
- **Problem:** on 2026-11-04 the account read 42% used for the week, so 58 points were free until Friday's reset, yet
  the dashboard said 100% sold and the home page showed every pass as sold out. One user held four day passes at
  once (20 + 20 + 20 + 40 = 100% of share), and the rule adds shares at an instant. But a 20% day pass may draw only
  20 ÷ 7 ≈ 2.9 points of the week; the four together at most 14.3. Every pass sold since the reset came to 44.8
  points, of which 42.2 were used. The rule cannot see the points of the days already gone, and those expire at the
  reset unsold.
- **Goal:** the gateway sells what Anthropic says is left of the week and what no running pass is still owed, and
  never the same point twice. A 20% day pass costs 2.9 points of the week it runs in; seven in a row cost 20. A
  pass that ends with points unused gives them back: its holder can no longer draw them, so they are free to sell.

## 1. Terms

- **Point:** one percent of the account's weekly bucket as Anthropic reports it (`utilization_pct` of the `7d`
  snapshots). Points are used up and come back only at the weekly reset.
- **Share:** a pass's rate, `share_pct`: the fraction of the weekly bucket it may draw per week. Unchanged.
- **Piece:** the points a pass takes from one Anthropic week: `share × days of that week it covers ÷ 7`, plus the
  same for each bonus share period (10-13 §1.1, `tickets.week_split`, `limits._pass_cap`). A piece is stated
  unscaled. Where a piece is compared with live usage (owed, check 1) it is scaled to the gateway's capacity
  (`100 − unattributed`, 10-18), because that is the most the pass can actually draw; the scaled piece is never
  larger than the unscaled one. Check 2 compares unscaled pieces with `max_sold_pct`.
- **Sold period:** a pass's `[starts_at, ends_at + bonus days)`, the period it was sold for. A pass marked used up
  (`used_up_at`, `tickets.end_used_up`) ends early for enforcement and for owed; its sold period is what check 2 and
  the panel's "sold" figures count. A share bonus's period is its stored `ends_at`, which `end_used_up` shortens; its
  pieces in later weeks therefore leave the sum when its pass is used up.
- **Week:** the current Anthropic week is `tickets.week_bounds`; later weeks follow in 7-day steps.
- **U:** the account's weekly utilization from the newest `7d` snapshot. **Fresh** means the snapshot is younger
  than `quota.stale_after_s` and its `resets_at` is in the future.
- **Owed:** what the account's running and queued passes may still draw from the current week: for each
  non-cancelled pass whose effective end is after now and that has time in the week, `max(0, its piece scaled to
  capacity − its debt (10-30 §3) − its use this week)`. Use is its weighted tokens (`limits._week_use`) at the
  observed 7-day rate; on an account with no rate yet it is the pass's attributed share (`ticket_shares` of the
  attribution), as 10-13 §1.2 measures it. A pass that has ended, used up or not, owes nothing. A queued pass owes its
  whole piece. Hand-set `share_7d` rows are **not** owed (§5).
- **Free room (F):** `max(0, max_sold_pct − U − Σ owed − M)` with `M = tickets.sell_margin_pct` (§7). The
  `100 − max_sold_pct` points above it are the headroom users' (10-03 §5), kept in points now. Defined only while
  the weekly report is fresh; otherwise there is no free room (§3).
- **Recycling:** a pass that has ended is neither owed nor sold any more in the current week. Whatever it did not use
  is not in `U`, so it is in `F` and sells again. Its holder cannot draw it, so no point is sold twice.

## 2. The selling rule

A change that adds draw to the account, with share `s`, 5-hour share `s5` and period `[a, b)`, is allowed when all
three hold. The changes are: a new pass (`grant`, `preview`, `sold_out`, order requests), a bonus's share and extra
days (`add_bonus`), an upgrade's extra share (`upgrade`), and a pass moved to a new place (`_shift`, on cancel and on
bonus days). `exclude` (the used-up passes a new pass replaces, `next_start`; a moved pass at its old place) applies
to checks 1 and 3 only.

1. **This week fits.** After the change, what it **added** to owed must fit: `Σ owed_after − Σ owed_before ≤
   max_sold_pct − U − Σ owed_before − M`, the new rows counted at their full scaled piece (they have no use yet). A
   change that adds nothing to this week's owed (a cancel, a queue move inside the week) passes this check. For a
   pass that is not written yet (`preview`, `sold_out`), this is the same as: its current-week piece, scaled, is at
   most `F`. A change that adds no time to the current week passes. Passes that have ended, used up or not, are not
   in the sum (recycling, §1); only `U` remembers them.
2. **Every later week fits.** For every Anthropic week after the current one that the change touches, the pieces
   over the sold periods of all non-cancelled passes and bonuses in that week, plus the change's own piece there,
   stay at or under `max_sold_pct`. Those weeks have no utilization yet, so promised pieces are the only measure;
   the current week is judged by check 1 alone, so that what ended passes left unused can be sold. The sum never
   applies `exclude`: a replaced pass keeps its sold period in the weeks ahead. A cancelled pass leaves the sum.
3. **The 5-hour bucket is not oversubscribed.** At every moment of the period the 5-hour shares of the passes and
   bonuses covering that moment, plus `s5`, stay at or under `max_sold_5h_pct` (§7). This is today's
   `check_capacity` over `sold_at`, reading `share_5h_pct` and falling back to `share_pct` for passes and bonuses, as
   `bonus_share` does. Extra bonus days extend the check to the new end at the pass's 5-hour share, as today.

Checks 1 and 2 are computed once per change, after every row of the change is written, inside the caller's
transaction or savepoint, so a bonus with share and days is judged as one change, not piece by piece; a failure
rolls the change back with `CapacityError`. `preview` and `sold_out` compute the same checks for a hypothetical
pass without writing.

The period of a week or month pass touches several weeks; check 1 charges only its current-week piece and check 2
charges each later piece to its week. A day pass that crosses the reset is charged the same way; the borrowing of
10-30 then moves use between its two parts at run time, within the free room it measures then. Queued passes this
week are owed their whole piece, so they are not sold twice either.

**Sold out** (10-03 §9, `/pricing`, the buyer's price list, order requests): a tier and length is sold out when a
pass starting now, or at the buyer's `starts_at`, fails any check. `sold_out_later` follows the same rule at the
later start. While the weekly report is not fresh (§3) the flag is `unavailable`, not `sold_out`, and the public
pages say "temporarily unavailable" instead of "sold out". `price_table` computes `F`, the per-week sums and the
5-hour timeline once per call and judges every tier and length against them.

**Grants and changes:** `CapacityError` names the check that failed: "This week has 12.3 points free and the pass
needs 14.3", "The week of 13.11 would hold 104% with this pass, over max_sold_pct", "5-hour shares would reach
120% at 04.11 15:56, over max_sold_5h_pct", or "No fresh usage report from Anthropic; try again in a few minutes".
There is no admin override: the poll (`quota.poll_idle_s`) brings a report within minutes. `cancel` still never
refuses: when its queue moves fail, the queued passes keep their dates, as 10-03 §8 has it.

## 3. Anthropic's report is the base

The gateway's own arithmetic decides only what is still owed to passes already sold. The amount of the week that is
gone comes from Anthropic alone, `U`, never from the gateway's token counts or rate estimates. When the gateway has
no fresh report, it does not estimate: nothing is for sale until the next snapshot. Reports arrive with every proxied
response and from the idle poll, so this is a wait of minutes, not hours.

**At the reset:** once `resets_at` passes, the newest snapshot is no longer fresh and `week_bounds` already projects
the new week, so nothing is for sale until the first snapshot of the new week lands: at once under traffic, up to
`poll_idle_s + poll_min_interval_s` on an idle account. `resets_at` drifting by up to `quota.RESET_TOLERANCE_S`
between reports moves a piece by `share × drift ÷ 7 days`, which is negligible, and `_debt` already tolerates it.

`U` already includes what the passes used, the owner's use on claude.ai, and anything the gateway cannot attribute.
Owed subtracts each pass's own use from its piece, so a pass's use is never counted both as used and as owed. A
pass's piece is scaled by `100 − unattributed` in owed and in check 1 because that is what the pass can draw under
10-18; outside use therefore both raises `U` and shrinks the pieces, which errs toward selling less, never more.

## 4. Reserved while it runs, recycled when it ends

Selling against `F` reserves the sale: once a pass is granted, its unused piece is owed and leaves `F` at once, so a
second buyer cannot take the same points while the pass runs. Two concurrent grants are serialised by the
`BEGIN IMMEDIATE` transaction, as today. `U` can still rise from outside use, which shrinks `F` for later sales but
never touches a pass already sold; at run time such a shortfall is handled as 10-18 handles it, and `M` covers the
requests in flight when a sale is checked. When the pass ends, whatever it left unused returns to `F` (§1,
Recycling). Lowering `max_sold_pct` or `max_sold_5h_pct` below what is owed or running refuses new grants until
passes end; it never cancels anything (10-03 §3).

## 5. Borrowing and the free room are one function

10-30 §1 computes free room for borrowing with a term for hand-set `share_7d` rows of ungated users. That term is
removed: hand-set shares are headroom (10-03 §5, "headroom is not enforced on its users"), and with the rows in use
on the live gateway (100% and 93% on two users without passes) it made free room zero at all times, so borrowing
never happened. Selling and borrowing now call the same function with the same definition of owed; they differ in
the margin they subtract (`sell_margin_pct` for sales, `borrow_margin_pct` for borrowing), in `exclude`, and in the
ceiling: sales use `max_sold_pct`, borrowing keeps 100 as 10-30 has it, since borrowing only moves a pass's own
allowance earlier and never sells new points. The capacity panel (§6) shows the hand-set shares' total so the admin
can see what the headroom users could draw.

## 6. Capacity panel (dashboard, Passes tab)

The Capacity card replaces "Sold now" and "Peak, next 30 days" with:

- **This week** (resets `{weekday} {time}`): a stacked meter of 100 points: used (`U`, from Anthropic), owed to
  passes, margin, free, and the headroom band `100 − max_sold_pct` at the end. The figure beside it is `F`, "free
  to sell". Below, as a line of text, "Sold this week: `Σ pieces` over sold periods, of which `Σ unused pieces of
  ended passes` returned", information only. While the report is not fresh, the meter is grey and the figure reads
  "no fresh report; nothing on sale".
- **Fullest week ahead:** the Anthropic week within 30 days with the highest `Σ pieces` over sold periods, as
  "`week start`: `Σ pieces` of `max_sold_pct`" with a meter. Omitted when no pass reaches past this week.
- **5-hour:** "`Σ 5h shares now` of `max_sold_5h_pct`" and the peak over 30 days, as the old two rows but with 5-hour
  shares and the new ceiling.
- **Headroom note:** "Hand-set weekly shares total `X%`; they are not reserved." Only when any ungated user has a
  `share_7d` row.

The Account utilization card stays. The tips `tip.capacity_sold` and `tip.capacity_util` and the keys `tk.sold_now`,
`tk.peak`, `tk.headroom` are rewritten in English and Persian; the grant form's availability line and the
`tk.sold_out_tip` wording name the failing check.

`GET /api/admin/capacity` returns `{week: {start, end, fresh, used, owed, margin, free, sold, returned, max_sold_pct},
peak_week: {start, sold} | null, five_hour: {now, peak_30d, max_sold_5h_pct}, hand_set_7d_pct, utilization}`.
Nothing of this reaches non-admins; `/pricing` still shows only the per-tier flags.

## 7. Settings (`[tickets]`)

- `max_sold_pct` (default 80): now the points of a week that passes may have: the current week's budget in `F`
  (used, owed and margin must fit under it) and the ceiling on promised pieces in each later week (§2 check 2).
  Validation unchanged: above 0, at most 100, and every tier's share at most this (a tier's week pass takes its
  full share in points, so a share over the ceiling could never be sold).
- `max_sold_5h_pct` (new, default 100): the ceiling on the summed 5-hour shares of passes running at once (§2
  check 3). Above 0 and at most 1000; it may exceed 100 for an admin who chooses to oversubscribe the 5-hour bucket. Every tier's
  5-hour share must be at most this, in the config and in `set_share_5h` (the dashboard override), which refuses a
  larger value with the reason.
- `sell_margin_pct` (new, default 5): `M` in §1. 0 to 100; 100 means nothing is for sale. Kept apart from
  `borrow_margin_pct`, whose value 100 means "no borrowing" and must not stop sales.
- `borrow_margin_pct`: unchanged.

The example config and the README's `[tickets]` table describe the three.

## 8. Where it lives

- New module `src/claude_proxy/capacity.py`, so neither `tickets.py` (1260 lines) nor `limits.py` grows. Imports:
  `capacity` imports `db`, `quota` and `tickets` at module level; `limits` imports `capacity` at module level (it
  already imports `tickets`); `tickets` imports `capacity` lazily inside the functions that call it, as it imports
  `limits` lazily today (`next_start`). `_week_use`, `_pass_cap`, `_debt`, `_weighted_since` and `_capacity` move
  from `limits` to `capacity`; `limits` imports them back by name, so no `limits` caller changes.
  - `pieces(conn, cfg, share, starts_at, ends_at, now) -> dict[week_start, points]`, from `week_split`'s boundaries;
  - `sold_by_week(conn, cfg, account_id, now) -> dict[week_start, points]`: Σ pieces over sold periods of the
    account's non-cancelled passes and bonuses per week, 30 days ahead; the current week's entry is for the panel
    only (with `returned`, the unused pieces of ended passes), later weeks gate check 2;
  - `owed(conn, cfg, account_id, week, att, rate, now, exclude) -> float`;
  - `free_room(conn, cfg, account_id, now, margin, exclude) -> float | None`: None without a fresh report;
  - `check(conn, cfg, account_id, share, share_5h, starts_at, ends_at, exclude) -> None`: the three checks for a
    hypothetical pass, raising `CapacityError`;
  - `verify(conn, cfg, account_id, starts_at, ends_at, exclude) -> None`: the three checks over rows already
    written, for `grant`, `add_bonus`, `upgrade` and `_shift`, inside their transaction or savepoint;
  - `context(conn, cfg, account_id, now)`: `F`, the per-week sums and the 5-hour timeline computed once, which
    `check` accepts so `price_table` evaluates every tier and length against one snapshot of the account;
  - `summary(conn, cfg, account_id, now) -> dict`: §6's payload.
- `tickets.check_capacity`, `verify_capacity`, `sold_out`, `capacity` become calls into `capacity`; `sold_at` stays
  for the 5-hour sum, reading `share_5h_pct` with the `share_pct` fallback for passes and bonuses.
- `limits._free_room` is replaced by `capacity.free_room(…, margin=cfg.tickets.borrow_margin_pct, exclude=t["id"])`.
- `web.py`: `/api/admin/capacity` returns §6; the public pages render `unavailable`; the grant form, the buyer's
  price list and `orders.py` need no change beyond the reason text they already pass through.
- `static/app.js` `renderTickets`, the home page's sold-out badge and `i18n.js`: §6 and `unavailable`.
- `config.py`: §7; `config.example.toml`, README.

## 9. Tests

New `tests/test_capacity_points.py`; `test_tickets_capacity.py` (the per-instant sums at its capacity tests, and
"touching periods"), `test_gateway_capacity.py` (`test_selling_still_checks_max_sold_pct_not_runtime_capacity`)
and `test_pass_borrow.py` (the hand-set owed case) are rewritten to the new rule. `test_two_concurrent_grants_cannot_oversell`
stays. In every case below the snapshot is fresh and `unattributed` is 0 unless said otherwise.

1. Account at 42 with three running 20% day passes that have used 3 points each (owed 0, each past its 2.86 piece):
   `F = 100 − 42 − 0 − 5 = 53`; a 40% day pass costs 5.71 and sells, and the 5-hour sum reaches exactly 100.
2. The same with the account at 93: refused by check 1, the reason naming 2.0 free points and 5.7 needed. At 90 with
   a 40% pass that has used 1 point (owed 4.71): `F = 0.29`, refused.
3. `max_sold_pct = 60`, `max_sold_5h_pct = 200`, two 40% week passes sold now with 3 days of the week left (each owes
   17.1 here and promises 22.9 next week): a third fits this week (51.4 owed under 60 − 1 − 5) but refuses by check
   2, next week holding 68.6 over 60.
4. A week pass starting on Thursday: its current-week piece is charged to `F`, its next-week piece to next week's
   sum; next week's sum alone can refuse it.
5. A pass starting next week needs no free room this week, and a pass that ended two days ago owes nothing.
6. Recycling: a 40% week pass that ended yesterday having used 10 of its 40 points leaves `F = max_sold_pct − 10 − M`
   with no other pass, so the 30 unused points sell again; the panel reports 40 sold and 30 returned. A pass sold
   just now leaves `F` by its whole piece before it is used. A pass replaced through `exclude` (used up) leaves owed
   and the 5-hour sum at once, and its next-week piece, if any, stays in next week's sum.
7. Stale snapshot, no snapshot, or `resets_at` in the past: refused with the "no fresh report" reason; every tier is
   `unavailable` on `/pricing`, not `sold_out`; the panel says so; a fresh snapshot lifts it.
8. 5-hour ceiling: four 20% passes and one 40% at once refuse a fifth 20% at the default; `max_sold_5h_pct = 150`
   admits it; a bonus with extra days is checked to its new end; a share bonus without `share_5h_pct` counts its
   `share_pct`; `set_share_5h` refuses a share over the ceiling.
9. `unattributed = 10`: owed and check 1 use pieces scaled by 0.9; next week's `Σ pieces` for check 2 is unscaled.
10. No observed rate with use on a running pass: owed uses the attributed share per ticket.
11. A bonus with share and extra days whose two parts each fit `F` but together don't: refused as one change, nothing
    written. An `upgrade` is checked the same way.
12. Hand-set `share_7d` rows of ungated users don't reduce `F`, and the panel reports their total.
13. Borrowing (10-30 tests 1–6) still passes through the shared function, now without the hand-set term; a borrowing
    pass's unused own limit is free for a sale and a sale's owed piece is held back from borrowing.
14. Queue moves on cancel and bonus days re-check with the three checks; a cancel whose queue move would pull a pass
    from next week into this one, with no room here, keeps the dates and cancels anyway; a failed move on bonus days
    refuses the bonus and names the check.
15. `max_sold_pct` lowered below `U + Σ owed + M` refuses new grants and cancels nothing; lowered below next week's
    `Σ pieces`, the same for passes reaching into next week.
16. `sold_out_later` in `orders.py` and the buyer's price list: a pass after the buyer's queue is judged at that
    start.
17. Validation: a tier's share over `max_sold_pct`, a 5-hour share over `max_sold_5h_pct`, `sell_margin_pct` outside
    0–100.
18. `/api/admin/capacity` payload; non-admins get 403 and `/pricing` leaks nothing new.

## 10. Non-goals

- No change to enforcement: pieces, 5-hour shares, borrowing limits and the serving order stay as specified.
- No selling of points the gateway merely estimates: when Anthropic's report is missing, nothing is sold.
- No change to what buyers see beyond the per-tier flags and reasons.
- Accounts beyond `account_id = 1`: every function takes `account_id`, as today, and nothing more.
