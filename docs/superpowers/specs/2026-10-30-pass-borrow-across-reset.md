# A pass across the weekly reset borrows from its next part

- **Date:** 2026-10-30
- **Status:** draft for review.
- **Builds on:** `2026-10-13-ticket-weekly-pieces.md` (§1.1, the prorated piece; everything not redefined here stays),
  `2026-10-18-gateway-capacity.md` (capacity scaling), the per-pass limits of 2026-10-28 (`limits._ticket_7d`).
- **Problem:** a pass's weekly allowance is split between the Anthropic weeks it covers in proportion to its hours in
  each. A day pass bought 2 hours before the reset gets 2/24 of its allowance before the reset. That part runs out
  early, before the user has done much work, and they then wait hours for the reset. The same applies to any pass
  whose first part is short, for example a week pass that starts an hour before the reset.
- **Goal:** the part before the reset can use more than its own share, taking it from the next part, as long as the
  old week has room that no one else is owed. That room disappears at the reset anyway. The pass's total never
  grows, and no other user is pushed into Anthropic's block.

## 1. Terms

All amounts are in percentage points of Anthropic's 7-day bucket, after capacity scaling (`_capacity`), as
`share_7d` limits are today.

- **Part:** a pass's piece of one Anthropic week (`_pass_cap` over the span it covers in that week), scaled to
  capacity. Parts are named by their position: the **current part** is in the week that contains `now`, and the
  **next part** is in the week that follows it.
- **Owed(p):** for a pass `p` other than the one being checked, with time in the current week (running *or* not yet
  started, so passes queued to start later this week are counted): `max(0, its current part − what it has used of it)`.
  Use is measured as `_ticket_7d` measures it (weighted tokens since the part's start ÷ the observed 7d rate). A pass
  owes nothing when it is cancelled.
- **Free room:** `100 − U − Σ owed(p) − M`, floored at 0. Here `U` is the account's current 7-day utilization from the
  latest snapshot, which already includes the checked pass's own use and the use the gateway cannot pin on users.
  `M` is a 1-point margin: Anthropic reports utilization in whole percent, so `U` can be up to a point low.

## 2. The current part's limit

For a pass whose effective end is after the current week's end:

```
extra  = min(next part, max(0, free room − max(0, own current part − own use so far)))
limit  = own current part + extra
```

The own unused part is subtracted from the free room because, until the pass uses it, it is counted in neither `U`
nor `owed`. Without that subtraction the pass would be counted twice: once for its own share, and again as free room.

Notes:

- **Recomputed on every request,** like every other limit. `extra` shrinks as the account fills up or as other passes
  start, so a pass that was over its own part can become blocked again partway through. That is intended: the room
  was never reserved.
- **Only the next part is borrowed from.** A month pass never draws three weeks ahead.
- **No borrowing (`extra = 0`)** when any of these holds: there is no fresh 7-day snapshot (`stale`, or
  `utilization_pct` is None); there is no observed 7-day rate (owed amounts can't be measured); or the pass ends
  inside the current week.
- **The 5-hour limit is unchanged.** It still caps any burst at the pass's 5-hour share.

## 3. The next part's limit

Once the reset has passed, the part that was the next part is now the current part. Its limit is reduced by what
the pass borrowed:

```
borrowed = max(0, use in the previous part − own previous part)
limit    = max(0, own current part − borrowed) + extra (§2, borrowed again from the part after it, if there is one)
```

Use in the previous part is measured over that span at today's 7d rate, as `ticket_weeks` measures past parts.
`own previous part` is the part without `extra`. Over its whole life, a pass uses at most the sum of its own parts.
The only exception is rounding in the measured rate, which the current split also has.

## 4. Display

`ticket_weeks` (status line and dashboard split bar):

- **Current part's meter:** use ÷ the §2 limit. The limit includes `extra`, so the meter shows what can be used now.
- **Split bar:** each part is sized as its own part, adjusted by what has actually been borrowed so far: the current
  part grows by `borrowed`, and the next part shrinks by it. Unused `extra` is not drawn, because it may disappear.
  The parts still add up to the whole pass.
- **Whole-pass figure (`ticket_pct`):** unchanged. It is total use ÷ the sum of the own parts.
- **No new wording.** The meter already reads "used of what's available before the reset". When borrowing makes the
  first part larger than its share, the bar shows it.

`used_up` and `hand_over` are unaffected. A pass that crosses the reset is never "used up" before the reset, because
its next part refills.

## 5. Where it lives

- `limits._free_room(conn, cfg, t, week, now) -> float`: §1 free room minus the pass's own unused part, or 0 when any
  §2 no-borrowing case holds. It iterates over the passes of the pass's account with time in the week, excluding `t`.
- `limits._ticket_7d`: adds `extra` (§2) and the `borrowed` reduction (§3). `LimitState.value` stays as the own part,
  so the 429 message still states the pass's share. `limit` carries the total.
- `limits.ticket_weeks`: §4.

## 6. Tests (`tests/test_tickets_limits.py`)

1. A day pass with 2 hours left before the reset, an empty account, and its own part used up: not exceeded. Its limit
   is its own part plus the whole next part.
2. The same pass with the account at `100 − M`: no extra. It is blocked at its own part, as today.
3. Another running pass with an unused part: that amount is held back. `extra` = free room minus that amount.
4. A pass queued to start later in the current week is counted as owed.
5. No fresh snapshot, or no rate: `extra = 0`.
6. After the reset: the next part's limit is its own part minus what was borrowed before the reset. A pass that used
   less than its own part before the reset gets its full next part.
7. A pass ending inside the current week: unchanged from today.
8. `ticket_weeks`: the split bar moves by `borrowed`, the parts add up to the whole pass, and `ticket_pct` is
   unchanged.
