# A pass across the weekly reset borrows from its next part

- **Date:** 2026-10-30
- **Status:** accepted (revision 4: day passes only, 2026-10-30).
- **Builds on:** `2026-10-13-ticket-weekly-pieces.md` (§1.1, the prorated piece), `2026-10-18-gateway-capacity.md`
  (capacity scaling), and the per-pass limits of 2026-10-28 (`limits._ticket_7d`, `limits.serving`).
- **Overrides:** 10-13 §1.1 says a pass's weekly pieces "cannot be borrowed from". This spec allows borrowing for a
  **day pass only**, in one direction: the part before the reset takes from the part after it. A week or month pass
  keeps each week's share, as 10-13 has it. Everything else in 10-13 stays.
- **Problem:** a pass's weekly allowance is split between the Anthropic weeks it covers in proportion to its hours in
  each. A day pass bought 2 hours before the reset gets 2/24 of its allowance before the reset. That part runs out
  early, before the user has done much work, and they then wait hours for the reset. A week or month pass is sold
  by the week, so its parts are each week's share; a day pass is one day's allowance to its holder, however the
  reset cuts it.
- **Goal:** the part before the reset can use more than its share, taking it from the next part, as long as the old
  week has room that no one else is owed. That room disappears at the reset anyway. A pass never uses more than its
  total, and no other user is pushed into Anthropic's block.

## 1. Terms

All amounts are percentage points of Anthropic's 7-day bucket, scaled to the gateway's capacity (`_capacity`), as
`share_7d` limits are today.

- **Part:** a pass's piece of one Anthropic week: `_pass_cap` over the span it covers in that week, scaled to
  capacity. The **current part** is in the week that contains `now`, and the **next part** is in the week after it.
- **Own limit:** the current part minus the **debt** carried from the previous week (§3), floored at 0. When nothing
  was borrowed, it is today's `share_7d` limit.
- **Use (u):** what the pass has used since the current part began, measured as `_ticket_7d` measures it today.
- **Owed:** what others may still draw from the current week:
  - For every other non-cancelled pass on the account with time in the current week (running, or queued to start
    later in it): `max(0, its own limit − its use)`. For owed, its part counts every bonus that overlaps the week,
    not only the bonuses active now (`_pass_cap` counts only active ones).
  - For every user who isn't gated by passes and has a hand-set `share_7d` row: `max(0, limit − current)`, as
    `_share_state` computes them.
- **Free room (F):** `100 − U − Σ owed − M`, floored at 0.
  - `U` is the account's 7-day utilization from the latest snapshot. It already includes this pass's use, the
    owner's direct use, and the use the gateway cannot pin on a user.
  - `M` is a new setting, `tickets.borrow_margin_pct`, default 5. It covers requests still in flight (`U` moves only
    when they finish), error in the observed rate behind owed, and whole-percent rounding in `U`. A value of 100
    turns borrowing off.

## 2. The current part's limit

For a **day pass** whose effective end is after the current week's end (any other pass: its own limit, always):

```
limit = min(own limit + next part, max(own limit, u + F))
```

`F` already counts the pass's unused own limit as free, because nothing else counts it. `u + F` is therefore the
most this pass can have used, after this request, without eating into what others are owed. The first `min` caps
the borrowing at the next part, so the pass never draws more than its total. The `max` means borrowing never takes
away what the pass already has: when the account is full, the pass still gets its own limit, as it does today.

- **Recomputed on every request,** like every other limit. `F` shrinks as the account fills up or as other passes
  draw, so a pass using borrowed room can be blocked partway through. That is intended: the room was never reserved.
  Two passes borrowing at once both see the same `F`. Any overshoot is bounded by the requests in flight, which `M`
  covers.
- **A day pass has two parts at most**, so only the second part is ever borrowed from.
- **Computed only when needed:** only when `u ≥ own limit`. Below that, the limit is the own limit and free room is
  not computed at all.
- **No borrowing:** the limit is the own limit in any of these cases:
  - the latest 7-day snapshot is stale, its `utilization_pct` is None, or its `resets_at` is not in the future (a
    fresh snapshot from the old week after the reset);
  - there is no observed 7-day rate, including the young-account path where `_ticket_7d` uses `attributed_since`;
  - the pass ends inside the current week, or is not a day pass.
- **The 5-hour limit is unchanged.** It still caps any burst at the pass's 5-hour share.

## 3. Debt: what was borrowed, recorded at the time

Debt is not re-measured after the reset. After the reset, capacity, the observed rate and the active bonuses all
change, and a re-measured debt would drift either way. Instead, while a pass is borrowing, the gateway records how
to measure the debt, in the old week's terms:

- **Table `pass_borrows`:** `(ticket_id, week_end, own_pts, rate, updated_at)`, primary key `(ticket_id, week_end)`.
- **When it is written:** in `limits.evaluate`, after every other check has passed (including the free-credit cap),
  next to `served["ticket_id"] = t["id"]`. It is written only when the serving pass's `share_7d` state is borrowing
  (its limit is above its own limit). Display callers (`states`, `ticket_weeks`, the dashboard cards) run the same
  code and never write. `_ticket_7d` returns `own_pts` and `rate` on the state for `evaluate` to store:
  - `own_pts` is the own limit: capacity-scaled, already reduced by any earlier debt, and not floored (see the
    carry below);
  - `rate` is the observed 7-day rate used for `u`.

  The last write before the reset is the one that counts.
- **Finding the row:** Anthropic's `resets_at` can shift a little from one report to the next. The row for the week
  that has just ended is therefore the one whose `week_end` is within `quota.RESET_TOLERANCE_S` of the current
  `week_start`, not an exact match.
- **The debt for that week:** `max(0, the pass's weighted tokens with ended_at in (the part's start, stored week_end] ÷ rate − own_pts)`.
  Tokens are counted as `_weighted_since` counts them, at lookup time, so requests that finished after the last write
  are included. Once the week is over the tokens are fixed, so the debt is fixed too.
- **A pass with no row** has no debt.
- **Nothing carries further:** a day pass has no third part. Debt larger than the second part, which requests in
  flight or a rate change can cause, leaves that part at 0; the overshoot is bounded by the requests in flight.
- **Batched lookups:** the owed calculation needs every pass's debt and bonuses. It gets them with one query for the
  debt rows and one for the bonuses, not one query per pass.

## 4. Several passes per user

`serving` picks the pass for the next request in two rounds:

1. Every running pass in `pass_order`, limited to its own limit. The first one with room serves the request, as
   today.
2. Only if none has room: every pass again in the same order, now with §2 borrowing. "Room" still means that
   neither limit is exceeded: round two reuses round one's `share_5h` state and recomputes only `share_7d`. The first
   pass with room serves.

So a pinned pass never borrows from next week while another of the user's passes still has room this week. That
room would otherwise be held back from the borrowing as owed. When every pass is at its limit, the "frees up soonest"
answer is built from round two's states, so a borrowing limit that refills sooner counts.

**Cards:** the dashboard pass cards (`web.py` around line 1374) show the serving pass with the states `serving`
returned, including any borrowing, so they don't mark a pass as at its limit while it is serving. The other passes
show `pass_states` (without borrowing), as today.

## 5. Display

`ticket_weeks` (status line and dashboard split bar):

- **Current part's meter:** use ÷ the §2 limit, so it shows what can be used now.
- **Split bar:** each part is drawn as its part, adjusted by debt. A past part grows by the debt it ran up, and the
  part after it shrinks by the same amount. The current part grows by what it has borrowed so far
  (`max(0, u − own limit)`), and the next part shrinks by that. Unused borrowable room is not drawn, because it may
  disappear. Each part is clamped at 0 or above. The parts still add up to the whole pass.
- **Whole-pass figure (`ticket_pct`):** unchanged. It is total use ÷ the sum of the parts.
- **Status-line 80% warning** (`web._ticket_weeks_text`): it turns on when use reaches 80% of the own limit, because
  the borrowing limit can shrink suddenly. It stays on past the own limit. The figure it prints is the current
  part's meter (use ÷ the §2 limit), which never goes past 100%. The wording is the same, so nothing new needs
  translating.
- **429 message:** unchanged. The `share_7d` description never prints the share, and it still says when to retry.

`used_up` and `hand_over` are unaffected. A pass that crosses the reset is never "used up" before it, because its next
part refills.

## 6. Where it lives

- `config.TicketsConfig.borrow_margin_pct: float = 5`.
- `db`: table `pass_borrows` (§3).
- `limits._debt(conn, cfg, t, week_start) -> float`: the §3 debt for the week that ends at `week_start`.
- `limits._week_use(conn, cfg, account_id, week_start, now) -> dict[ticket_id, weighted]`: one grouped
  `SUM(weighted) … GROUP BY ticket_id` query for the account's passes, for owed. `_free_room` takes it as an argument,
  so `serving` runs the query once per call, not once per pass.
- `limits._free_room(conn, cfg, t, week, uses, now) -> float | None`: `F` from §1, or None when a §2 no-borrowing case
  holds.
- `limits._ticket_7d(…, borrow: bool)`: the own limit, with the debt taken off; with `borrow`, the §2 limit. It sets
  `own_pts` and `rate` on the state, and never writes. `pass_states` and `used_up` call it without `borrow`.
- `limits.serving`: the two rounds of §4.
- `limits.evaluate`: the §3 upsert, for the pass that serves, only when it is borrowing.
- `web` pass cards: §4.
- `limits.ticket_weeks` and `web._ticket_weeks_text`: §5.

## 7. Tests (`tests/test_tickets_limits.py`)

1. A day pass with 2 hours left before the reset, an empty account, and its own limit used up: not exceeded. Its
   limit is its own part plus the next part.
2. Use rises past the own limit while `U` rises with it: the pass is blocked at `u = u₀ + F₀`, not halfway there.
3. The account is at `100 − M`: no borrowing, and the pass is blocked at its own limit.
4. Another running pass with unused room: that amount is held back. The same holds for a queued pass starting later
   this week, a bonus starting later this week, and an ungated user with a `share_7d` row.
5. Two passes borrowing at once: together they stay within `F`.
6. Cases where nothing is borrowed:
   - a stale snapshot;
   - no rate;
   - the young-account path;
   - a fresh snapshot whose `resets_at` has already passed;
   - a pass that ends inside the current week;
   - `borrow_margin_pct = 100`.
7. Debt after the reset: the next part's own limit is its part minus the recorded debt. Capacity jumping at the
   reset and a bonus that ended before it don't change the debt. A pass that never borrowed has its full next part.
8. A week pass across the reset never borrows, room or no room; nothing is recorded for it.
9. Two passes on one user: the second pass's own room is used before the first one borrows.
10. `ticket_weeks`: the split bar moves by the debt and by what has been borrowed, the parts add up to the whole,
    `ticket_pct` is unchanged, and the 80% warning follows the own limit.
11. `resets_at` moved by 60 seconds after the reset: the debt row is still found.
12. Only `evaluate` writes `pass_borrows`. Calling `states`, `ticket_weeks` or rendering the dashboard leaves the
    table empty, and so does a request refused by another limit.
13. Debt larger than the second part leaves it at 0 and carries nowhere.
14. Round two: a pass at its 5-hour limit doesn't serve even though it could borrow. With every pass at its limit,
    the retry time comes from round two.
15. The dashboard card of a borrowing pass that is serving isn't marked as at its limit.
