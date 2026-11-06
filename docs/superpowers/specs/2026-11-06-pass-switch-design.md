# Switching the serving pass (2026-11-06)

A user with several running passes can pick which one serves their requests, and their pick holds even when the pass
is at a temporary limit. Amends the "Several passes per user" design (2026-10-28, and §4 of
`2026-10-30-pass-borrow-across-reset.md`).

## Rule

- **Automatic (no pick):** `limits.serving` takes the first pass in `pass_order` (oldest first) that is not at a
  limit. When the serving pass reaches a limit, requests move on to the next pass that is not paused, and come back
  once it refills.
- **The user's pick ("Use this pass", `POST /api/me/tickets/pin`):** the picked pass goes first in `pass_order`
  (`users.pass_pin`).
  - Picked while it is **not** at a limit: it serves now. A limit it reaches later hands requests on to the next
    pass that is not paused, as in automatic mode, and they come back once it refills.
  - Picked while it **is** at a limit (5-hour or weekly): `users.pass_pin_hold` = the switch's time. From that moment, nothing is
    served until it refills. Requests get the usual limit 429 with the time it lifts, and no other pass is used.
    Limits are still respected: the held pass serves nothing while it is at its limit.
  - The hold ends the first time the picked pass serves a request again (`limits.evaluate` clears it). From then on
    it behaves like a pick made while it was not at a limit.
  - It also ends when any pass of the user's starts after the switch: bought or granted to start now, Start now, a
    hand-over, or a queued pass reaching its start time. `pass_pin_hold` holds the switch's time, and a running pass
    that started later voids it, so a newer pass is never left idle behind the old choice.
  - A pass that is used up for good (its weekly piece is spent and does not refill before the pass ends) never
    holds. Requests go to the other passes, and used-up passes still hand over to a queued pass
    (`limits.hand_over`).

In short: the user's choice overrides the gateway's automatic pick at the moment it is made, even for a paused pass.
After that, the gateway's automatic handover applies again.

## Dashboard

- The pass in the top slot ("hero") is the one serving. When none serves, it is the `held` pass: the picked pass at
  its limit, or otherwise the pass that refills soonest. `passcard` sends `held: true` for it, from `limits.pick`.
- A pass at a limit shows an amber chip with when it is back: "Back in 1h:43m" (Persian "۱س:۴۳د تا بازگشت"; no
  leading zeros, a zero part dropped: "59m", "2h"; waits of a day or more in words). The limit that holds it is
  marked on its own row: a red meter and a red "!" beside its name, whose tooltip reads "Limit reached: the pass
  will be usable again in 59m."
- Every other running pass that is not used up offers **Use this pass** ("از این بسته استفاده شود"). The confirm
  dialog says what happens. For a paused pass, it says requests wait from the moment you switch.

## Claude Code

The gateway's limit 429s carry `anthropic-ratelimit-unified-status: rejected`, `-reset` and
`-representative-claim` (`five_hour` / `seven_day` for a pass, "daily limit" and similar for hand-set caps, "gateway
usage limit" for the account's own). gclaude sets `CLAUDE_CODE_RETRY_WATCHDOG=1`. Claude Code then shows "5-hour limit
reached (resets …) · Retrying in …" and sleeps until the reset in the same turn, so a held pass resumes the session on
its own.
