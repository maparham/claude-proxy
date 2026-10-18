# Share limits are shares of the gateway's capacity

- **Date:** 2026-10-18
- **Status:** accepted.
- **Builds on:** `2026-10-13-ticket-weekly-pieces.md` (ticket `share_5h`/`share_7d`), hand-set share limits
  (`limits._share_state`).
- **Refines:** `2026-09-21-claude-proxy-design.md` §8 (the limits table), which defines `share_5h`/`share_7d` as
  percentage points of the account bucket; they are now points of the gateway's capacity (§1).
- **Problem:** a `share_7d = 100` user saw `week 74%` while Anthropic reported the account at 79%: 5 points were
  used outside the gateway (claude.ai). Anthropic blocks at 100% of the account, so that user would be refused
  upstream while their line still read ~95%. Every share limit has the same gap, not just 100.

## 1. Definition

A share limit of X% means X% of the **capacity available to the gateway**, not of the Anthropic account.

- **Gateway capacity** of a bucket (`5h`, `7d`) = `100 − unattributed`, where `unattributed` is
  `quota.attribution(...)["unattributed"]` for the bucket's current window: account points the gateway cannot pin
  on any of its users (outside use, usage before the window's first report).
- **Effective limit** = configured limit × capacity ÷ 100. It shrinks the moment outside usage appears, and is
  back to the configured value as soon as the window's `resets_at` has passed (§3), before any new snapshot.
- No special case for any value: 100 is just the limit that ends exactly where Anthropic blocks.

Example: account 79%, 74 attributed to the user, 5 unattributed → capacity 95. `share_7d = 100` → effective 95,
the user reads 74/95 = 78% and reaches 100% together with the account. `share_7d = 30` → effective 28.5.

## 2. Where it applies

Every share limit, with the capacity of its own bucket:

- hand-set `share_5h` / `share_7d` rows (`_share_state`);
- a ticket's `share_5h` (`_ticket_5h`) and its weekly piece `share_7d` (`_ticket_7d`): the piece cap
  (share × overlap ÷ 7 + bonuses) is scaled the same way.

The configured value stays in `LimitState.value`; `LimitState.limit` carries the effective limit, so `remaining`,
`exceeded`, status lines and 429s follow it. Whether a limit is enforced at all is unchanged (§3).

`LimitState.pct` changes for a limit of 0: it reads 100% (used up) instead of none. That is what a share reads at
capacity 0, and a usage limit set to 0, already exceeded by any use, now reads 100% too.

## 3. Stale or missing snapshots, resets

- **Capacity source:** `unattributed` from the bucket's newest snapshots, stale or not, unless the window is known
  to have reset (`resets_at` set and `<= now`). A snapshot with no `resets_at` counts as not reset. After a known
  reset, or with no snapshot at all, capacity is 100. It is clamped to `[0, 100]`.
- **At capacity 0** a share's limit is 0: it is exceeded (429) whenever its usage is measured. A share that is not
  measured keeps its usual outcome: a hand-set share is skipped while snapshots are stale, and a ticket share with
  stale snapshots and no observed rate still refuses with 503 (`NO_RATE`).
- **Enforcement while stale** is as before: hand-set shares are skipped; ticket shares are enforced from the user's
  weighted tokens at the observed rate. The effective limit applies to whichever is measured.
- **Reset transient:** after `resets_at` passes, capacity is back to 100 at once, but a hand-set share's usage
  (its attributed points) still reads the old window until the first snapshot of the new one lands.

## 4. Non-goals

- Selling tickets: grants still check sold + share against the static `max_sold_pct` ceiling
  (`tickets.check_capacity`), not the runtime capacity. Outside usage is a runtime shortfall, not a reason to
  refuse sales.
- No new settings. The admin copy that defines a share (README limits table, dashboard `kind.share_*`, `unit.pct`,
  `users.lede`) now says "of the gateway's capacity"; no other dashboard change.
