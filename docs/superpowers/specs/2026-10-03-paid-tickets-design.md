# Paid tickets

- **Date:** 2026-10-03
- **Builds on:** `2026-09-21-claude-proxy-design.md` (sections 7, 8 and 17.2: quota attribution and share limits) and `2026-09-26-signup-and-browser-authorization-design.md` (sign-up credit).
- **See also:** `2026-10-03-ticket-window-alignment-note.md`, which records why the day is the unit and the alternative (prorating by window overlap) kept for later.
- **Goal:** people buy a slice of the Claude subscription for 1 day, 1 week or 1 month. Each slice is reserved: the gateway never sells the same capacity twice and never rejects a buyer for another ticket's use. Anthropic's own 429 is still possible if the admin lets the headroom users (section 5) overrun the account. The admin sets prices, discounts and bonuses from the dashboard.

## 1. Terms

- **Account.** One Claude Max 20x subscription behind the gateway. Its 5-hour and weekly limits are 100% each. Step 1 has exactly one account (`account_id = 1`). Every capacity record carries `account_id` so more accounts can be added later without reworking data (section 11).
- **Tier.** A size of slice, as a share of the account. Two tiers:

  | Tier | Share | Compared to | 1 day | 1 week | 1 month |
  |---|---|---|---|---|---|
  | Lite | 5% | Claude Pro | $3 | $8 | $20 |
  | Standard | 25% | Claude Max 5x | $12 | $35 | $100 |

  Monthly prices equal Anthropic's. Day and week cost more per day; Anthropic sells nothing shorter than a month. The prices above are the defaults; the admin can change them (section 6). "Compared to" is approximate: a tier gets that plan's weekly budget spread evenly over its days (section 7), where the plan itself lets a subscriber spend it on any day.
- **Day.** The unit of a ticket: a tier's share for 24 hours. Every ticket is a run of consecutive days, and the share is enforced day by day.
- **Ticket.** One purchase: a tier for a number of days (1, 7 or 30), from a start time to an end time. A week or month ticket is a bundle of consecutive days sold at the bundle price. Tickets do not renew.
- **Bonus.** Extra share or extra days the admin adds to a ticket.

## 2. Payment

There is no payment provider in step 1. Users pay the admin outside the app (bank transfer or similar). The admin then grants the ticket in the dashboard. A Stripe checkout can later create the same ticket record the grant form does.

## 3. Configuration (`config.toml`)

```toml
[tickets]
how_to_buy = "Send the amount by bank transfer to … and email the admin."   # shown to users and on /pricing
max_sold_pct = 80    # ceiling on what tickets may reserve; the rest is headroom (section 5)

[tickets.tiers.lite]
label = "Lite"
share_pct = 5
compare = "Claude Pro"
default_usd = { day = 3, week = 8, month = 20 }

[tickets.tiers.standard]
label = "Standard"
share_pct = 25
compare = "Claude Max 5x"
default_usd = { day = 12, week = 35, month = 100 }

[tickets.currencies.EUR]
round_to = 0.50
```

USD is always available with an implicit rate of 1 and `round_to = 0.01`; it needs no `fx_rates` row. `max_sold_pct` is per account; with one account it is one number.

Tickets are managed from the dashboard only. The `gateway` CLI gets no ticket commands in step 1.

- **Shares** stay in the config. A share change alters capacity, so it is not a dashboard click. Existing tickets keep the share they were sold with.
- **`max_sold_pct`** is the most that tickets and bonuses together may reserve at any moment. The remainder is headroom for everyone else on the account: the admin, free-credit sign-ups and users with hand-set limits, none of whom are counted in `sold(t)`. Lowering it below what is already sold refuses new grants until tickets expire; it never cancels anything.
- **`default_usd`** seeds the prices table on first start (section 6). After that the database holds the prices.
- **Currencies.** Adding a currency is one config entry plus a daily rate. EUR is the only one for now besides USD.
- **Lengths** are fixed: day = 1 day, week = 7 days, month = 30 days. A day is 24 hours from the ticket's start, so a ticket's day boundaries follow its own start time, not the calendar.

## 4. Data

New tables:

- **`ticket_prices`:** `tier`, `length` (`day`/`week`/`month`), `usd`, `updated_at`, `updated_by`. One row per tier and length.
- **`ticket_discounts`:** `id`, `tier`, `length`, `usd`, `starts_at`, `ends_at`, `created_by`, `created_at`, `cancelled_at`. At most one active discount per tier and length at any moment; the dashboard refuses an overlapping one.
- **`fx_rates`:** `currency`, `rate` (local units per 1 USD), `set_at`, `set_by`. A history; the newest row per currency is the current rate.
- **`tickets`:** `id`, `user_id` (nullable, see section 10), `user_name` (copied at grant), `account_id`, `tier`, `share_pct` (copied at grant), `length`, `days` (1, 7 or 30), `starts_at`, `ends_at` (`starts_at` + `days` × 24 h), `list_usd` (regular price at grant), `usd` (charged, after any discount), `discount_id` (nullable), `currency`, `rate`, `amount` (local amount charged, rounded), `granted_by`, `granted_at`, `cancelled_at`, `cancelled_by`, `note`.
- **`ticket_bonuses`:** `id`, `ticket_id`, `share_pct` (0 or more), `extra_days` (whole days added to the ticket's end, 0 or more), `starts_at`, `ends_at`, `note` (shown to the user), `granted_by`, `granted_at`, `cancelled_at`, `cancelled_by`. At least one of `share_pct` and `extra_days` is above zero. `share_pct` applies only between `starts_at` and `ends_at`; `extra_days` extend the ticket at the ticket's own share.
- **`usage_estimates`:** `computed_at`, `family`, `bucket`, `busy_hours`, `p75_share_per_hour`. Written by the daily task of section 9 and read by `/pricing`, so the page never computes anything itself.

There is no stored gating flag. A user is *ticket-gated* when they have at least one ticket row with `ungated_at` NULL, cancelled or not (section 7, Exit). Granting, cancelling, bonusing and ungating (section 7) each write an audit-log row.

A discount is *active* at `t` when `cancelled_at` is NULL and `starts_at ≤ t < ends_at`. The overlap check on create is an interval overlap against all non-cancelled discounts of that tier and length, not a check at the current moment.

A ticket's effective end is `ends_at` plus the summed `extra_days` of its non-cancelled bonuses. Bonus days continue the ticket's own day boundaries. All periods are half-open: a ticket *covers* `t` when `starts_at ≤ t < effective end`, and a bonus share covers `t` when `starts_at ≤ t < ends_at`. Two periods that touch at one instant therefore never both count at that instant. A bonus's share period must lie within its ticket's effective period; the dashboard clamps it.

## 5. Capacity

Capacity is reserved in time. For an account, at any moment `t`:

```
sold(t) = sum of share_pct of non-cancelled tickets covering t
        + sum of share_pct of non-cancelled bonuses covering t
```

- **The rule.** Granting a ticket or a bonus is allowed only if `sold(t) + new share ≤ max_sold_pct` for every `t` in its period. Extending a ticket by bonus days checks the extended period at the ticket's share.
- **One ticket at a time.** A user's tickets never overlap, bonus days included. A new ticket starts at the later of now and the effective end of the user's last non-cancelled ticket, so a returning user's ticket never starts in the past and a user with a live ticket queues. Bonus days that would run into the user's next queued ticket move that ticket and any after it forward, only as far as needed for it to start where the extended ticket now ends; each moved ticket is re-checked against capacity, and if one fails the bonus is refused with the reason. Bonus days that would run into a ticket that has already started are refused; days that stop short of it are not. So at most one ticket covers any moment for a user.
- **Checking.** With half-open periods `sold(t)` is a step function that can only rise at a start point. The check evaluates `sold` at the new period's own start and at every ticket or bonus start that falls inside the new period, and each value plus the new share must stay within `max_sold_pct`.
- **Why per moment is enough.** Anthropic's weekly budget resets on its own schedule, which ticket dates do not follow. Because every ticket is enforced per day at one seventh of its share (section 7), any Anthropic week contains at most seven days of a given slice, so a slice never draws more than its share from one week however the tickets in it turn over. The note in `2026-10-03-ticket-window-alignment-note.md` works this through.
- **Queued tickets** reserve their future period the same way. Expired and cancelled ones stop counting, so slices free up without any job.
- **Atomic.** Every change to reservations, a grant, a cancel, a bonus and any queued tickets it moves, runs its checks and writes in one `BEGIN IMMEDIATE` transaction, so two admins acting at once cannot oversell and a half-applied move cannot be observed.
- **Sold out.** A tier and length is sold out when a ticket starting now would fail the rule.
- **Headroom is not enforced on its users.** Non-ticket users keep whatever limits the admin set. `max_sold_pct` only bounds what is sold; if the admin sets generous hand limits the account can still run hot, and the capacity panel (section 8) shows the actual account utilization next to the sold share so this is visible.

## 6. Prices, discounts and currency

- **Regular price.** `ticket_prices`, edited in the admin's Pricing panel. Each change is written to the audit log.
- **Discount.** For one tier and length, a USD price for a period. It must be lower than the regular price at the time it is created, and the dashboard refuses it otherwise. While active it replaces the regular price everywhere a price is shown or charged. When it ends the regular price returns by itself. If the regular price is later lowered below an active discount, the lower of the two is charged and no strike-through is shown.
- **Local price.** `round(usd × rate, round_to)`, rounding to the nearest step with ties rounded up: $20 × 0.92 = 18.40 becomes €18.50, and 18.25 becomes €18.50.
- **Fixed at grant.** A ticket stores `list_usd`, `usd`, `rate` and `amount`. Later changes to prices, discounts or rates never touch existing tickets.
- **Stale rate.** A rate older than 36 hours shows a warning on the admin's rates panel and in the grant form. Granting still works after the admin confirms.
- **No rate.** A currency without any rate cannot be used for a grant and is hidden on `/pricing`.
- **No VAT** in step 1.

## 7. Enforcement

- **Who.** A user is ticket-gated while they have at least one ticket row not marked `ungated_at` (section 4). Until then nothing changes for anyone: admins, users with hand-set limits and new sign-ups on the free credit are governed by their existing limits only. Grants to revoked or disabled users are refused.
- **Credit.** Granting a first ticket removes the user's `cost_total` sign-up credit row and writes an audit-log row saying so.
- **Other rows at first grant.** The ticket replaces the user's share rows (below) but their token, request and cost rows stay. A hand-set `tokens_daily` would then throttle a paying buyer, so the grant form lists the user's remaining limit rows with a tick box, on by default, to remove them with the grant. Each removal is audited.
- **Exit.** Gating has no automatic end: a user whose tickets have all ended gets the "ticket ended" refusal until they buy again. The Users page has an **Ungate** action for a user with no active or queued ticket: it marks every one of their tickets `ungated_at` so they stop counting toward gating, and opens the Limits dialog so the admin sets hand limits. The tickets remain as sales records. `tickets` gains `ungated_at` (nullable) for this.

On every request from a ticket-gated user, `limits.evaluate` (and `limits.states`, for the dashboard) looks for the ticket covering now:

- **Active ticket.** Two share limits are built from it, in place of any `share_5h`/`share_7d` rows an admin set for that user. They are evaluated in the engine's existing order, after `enabled` and `allowed_models` and alongside the other rows:
  - `share_5h` = ticket share + active bonus share, against Anthropic's 5-hour window as today.
  - `share_day` = (ticket share + active bonus share) ÷ 7 (Lite: 0.71%), the slice of Anthropic's weekly budget the user may spend in the current ticket day. It is measured from the user's own weighted tokens on Anthropic routes since the current day began, converted to points of the account-wide 7-day bucket (`seven_day`, not the per-model 7-day buckets) at that bucket's observed rate (below), whether snapshots are fresh or stale; not as the user's share of Anthropic's current weekly window. Why not the share attributed to the user's requests: Anthropic reports utilization in whole-percent steps, and a Lite day (0.71 points) is smaller than one step, so attribution would read 0 for most of a day and then jump by a full point. Each day starts at zero and the limit resets at the day boundary; unused share does not carry over. While snapshots are fresh and the user has used nothing yet today, the day reads 0 without needing a rate. `share_5h` stays on attribution while snapshots are fresh: a 5-hour share of several points spans whole steps.
  - Other limit rows (allowed models, per-minute caps, cost limits) still apply.
  - **Current day.** The ticket day containing now: whole 24-hour steps from `starts_at`, including bonus days.
- **No active ticket.** The request gets a 403 `permission_error` with `"Your ticket ended on <date>. <how_to_buy>"`, or `"Your next ticket starts on <date>."` when one is queued. 403 matches the used-up sign-up credit: a client should show the message and not retry.
- **Third-party models** are not part of a ticket. Their cost is real money per request and the ticket price does not cover it. A ticket-gated user's request to a third-party route is refused with the same 403 and `"Your ticket covers Claude models only."`, unless the admin has set a cost limit for that user, in which case that limit governs the request as it does today.
- **Stale snapshot.** Today a stale account snapshot (none newer than 30 minutes) skips share limits. For ticket-gated users that would mean no limit at all during an outage of the quota feed, so instead the share is estimated from the user's weighted tokens at the account's observed rate. The rate, per bucket, is the median over the last 7 days of `weighted_tokens ÷ rise` over each rise of utilization above its high-water mark, the weighted tokens being those of every forwarded Anthropic request since the previous rise (whole-percent steps leave most snapshot pairs with no rise; their tokens carry to the next one). A reset drops the carried tokens. Weighted tokens are counted the same way everywhere they are divided by the rate: list-price cost in reference-input tokens, raw tokens for a model without a price. The estimate is `weighted tokens the user sent in the counting period ÷ rate`. For `share_5h` the counting period starts at the last known window start, or at the stale snapshot's `resets_at` once that time has passed; for `share_day` it starts at the day's start. The dashboard and status line mark such values "estimated without live data". If no rate has ever been observed, the ticket user's requests are refused with a 503, `Retry-After: 60` and `"Usage data is unavailable. Please retry in a minute."`; this can only happen on an account that has never served a request.
- **Assumption for step 1.** The gateway polls Anthropic's usage endpoint every 10 minutes when no responses arrive, so reports go stale only while Anthropic itself is failing, when requests fail anyway. The stale-snapshot estimate above is a safeguard, not a path to tune. Its rate falls back to the whole retained history when the last 7 days hold no usable pair.
- **Exceeded.** An exceeded `share_5h` returns the usual 429 with its reset time. An exceeded `share_day` returns 429 with `"Today's share of your <tier> ticket is used up. It resets at <time>."`, the time being the end of the current ticket day, and the same `Retry-After`.
- **Tickets switched off.** With `[tickets]` disabled, a user who is still ticket-gated is refused with a 403 `permission_error`, `"Tickets are paused; ask the admin."`, on every request. Their sign-up credit was removed with their first ticket and they may have no other limit, so switching tickets off must not open the account to them. They stay refused until the admin ungates them.
- **Non-ticket users** keep today's stale-snapshot behaviour: share limits skipped, token limits applied. Ticket users stay limited during an outage because their limit is the product; a hand-limited user's admin accepted the looser rule when they chose share limits.

## 8. Dashboard

### Admin

- **Exchange rates.** One row per configured currency: current rate, when and by whom it was set, an input for today's rate, and a warning badge after 36 hours.
- **Pricing.** Tier by length grid of USD prices, editable. Under it, discounts: create (tier, length, price, start, end), list, cancel.
- **Tickets.**
  - **Grant form:** user, tier, length, currency, optional note. It shows the price (regular or discounted), the local amount at the current rate, the start (now, or queued after the user's last ticket) and whether the period is available. A tier can show sold out on `/pricing`, which asks about a ticket starting now, while a grant to a user with a live ticket succeeds, because that ticket starts later; the form explains this when it happens.
  - **List:** active, queued and ended tickets, filterable by user.
  - **Cancel:** frees the slice and ends the ticket's bonuses. Queued tickets of that user move forward to close the gap, each re-checked against capacity. A cancel is never refused: if a moved ticket would not fit, the queued tickets keep their dates instead and the form says so. There is no "keep dates" option, since a gap left on purpose could never be filled: new tickets always start after the user's last one. Refunds happen outside the app.
  - **Bonus:** on a ticket, add extra share and/or extra days with a period and a note for the user. It is checked against capacity like a ticket. The form says when extra days will move the user's queued tickets.
- **Capacity.** Share sold on the account now and the highest `sold(t)` over the next 30 days, both against `max_sold_pct`, next to the account's actual 5-hour and weekly utilization. The panel labels the two: sold is what tickets may use, utilization is what everyone has used, and the gap between `max_sold_pct` and 100 is what the headroom users have.
- **Users page.** An **Ungate** action (section 7) on users with tickets but none active or queued.

### User

- **Current ticket:** tier, end date (with "+N days bonus" when extended), any queued ticket.
- **Usage bars:** the 5-hour bar as today, and a "today" bar in place of the weekly one: share used in the current ticket day against `share_day`, with the time until the day ends.
- **Bonus badge:** "Bonus: +3% until Oct 20" with the admin's note. While a share bonus is active, both bars use the ticket share plus the bonus share as their denominator.
- **Price list:** in the user's currency, with availability, usage hints (section 9), active discounts with a live countdown, and `how_to_buy`.

Non-admins still never see the account's usage, the number of seats sold or anything about other users.

## 9. Public pricing page

`GET /pricing`, no sign-in:

- **Table:** tiers by length in EUR, with a sold-out badge per tier and length. The badge is the one thing the page says about the account's state, and it says only whether a purchase is possible.
- **Discounts:** the regular price struck through, the discounted price, and a live countdown such as "Offer ends in 2d 14h 03m", ticking every second in the browser. At zero the page reloads with the regular price.
- **Rate date:** "Prices converted at the rate of <date>".
- **Usage hints:**
  1. **Comparison:** the tier's `compare` text, e.g. "≈ Claude Pro".
  2. **Measured estimate:** "at least N hours of steady use" per 5-hour window and per day, for Sonnet and Opus separately. It is a lower bound: the page must not promise more than a heavy user will get. Recomputed once a day by the gateway's daily maintenance task, the one that also applies retention, from the last 30 days:
     - A *busy hour* is a user's UTC clock hour with at least 20 minutes between its first and last forwarded Anthropic request, or at least 10 such requests. Hours with a single short burst are left out, because counting them would make an hour look cheap and the estimate too generous.
     - For each busy hour and each model family, the share attributed to that family's requests in the hour is recorded (from `quota.attribution`, 5-hour bucket and weekly bucket). An hour with both families contributes to both, each with its own share, so Opus hours are never filed under Sonnet.
     - Hours per window = tier share ÷ the 75th percentile of share per busy hour. Per day uses the weekly-bucket share per busy hour against the tier share ÷ 7. One number, not a range, worded "at least".
     - Hidden for a model family until it has at least 50 busy hours of data; the tier then shows only its comparison text.
- **`how_to_buy`** text.

Beyond the sold-out badges, the page shows nothing about the account: no utilization, no number of seats, no users.

## 10. Deleting users

Deleting a user stays hard:

1. **Revoke first.** Unchanged: only a revoked user can be deleted.
2. **No live tickets.** Deletion is refused while the user has an active or queued ticket. The admin must cancel it first.
3. **Records stay.** Tickets and bonuses are sales records and are never deleted. Deleting the user sets `tickets.user_id` to NULL; `user_name` keeps who it was.
4. **Typed confirmation.** The dashboard asks the admin to type the user's name. The CLI asks the same unless `--yes` is passed.

Each step is in the audit log.

## 11. Later: more accounts

Not in step 1. Multi-account needs one stored login, one quota poller and one set of quota snapshots per account, routing each user's requests to their ticket's account, and placing new tickets on the account with the most room. The `account_id` on tickets is the only part this design adds for it.

## 12. Testing

pytest, following `tests/test_limits.py` and `tests/test_web.py`:

- **Capacity:** overlapping periods, queued tickets reserving their slot, bonuses counting, expiry and cancellation freeing the slice, two concurrent grants not overselling, `max_sold_pct` refusing a grant that 100 would allow, lowering `max_sold_pct` below sold refusing new grants without touching existing tickets, bonus days moving queued tickets and refused when a moved ticket fails, cancel closing the gap for queued tickets, a user never holding two tickets covering one moment, a returning user's ticket starting now rather than at their old end, touching periods not double-counted at the shared instant, a cancel that cannot move queued tickets succeeding with dates kept, a bonus and its queue move in one transaction.
- **Enforcement:** tier share for the 5-hour window, `share_day` = share ÷ 7 measured over the current ticket day, the day boundary resetting it, consecutive days of one ticket independent of each other, a slice handed from one user to the next mid-week never drawing more than its share from one Anthropic week, bonus share added, bonus days continuing the day boundaries, admin share rows replaced, other limit rows kept, ended and queued messages as 403, third-party models refused for a ticket user without a cost limit and governed by one when set, stale snapshot estimating share from weighted tokens at the observed rate and refusing with 503 when no rate exists, ungated users unchanged under a stale snapshot, sign-up credit removed at first grant with an audit row, gating derived from ticket rows and surviving ticket expiry, Ungate clearing it and refused while a ticket is live, grants to revoked or disabled users refused, remaining limit rows listed and removed at grant when ticked.
- **Prices:** rounding to 0.50 with ties rounded half up, USD at implicit rate 1, rate and price fixed at grant, stale-rate warning, missing rate, discount start and end, overlapping discount refused, discount at or above the regular price refused, lower of discount and later-lowered regular price charged.
- **Web:** admin endpoints (rates, prices, discounts, grant, cancel, bonus, ungate) each writing an audit row, `/pricing` without sign-in and its sold-out badges, `/pricing` reading only `usage_estimates`, no account detail leaked to non-admins, usage estimate hidden below 50 busy hours, busy-hour rule excluding single-burst hours, mixed hours counted for both families, estimate equal to share ÷ 75th-percentile share per busy hour.
- **Deletion:** refused with a live ticket, tickets kept with `user_name` after deletion, typed confirmation.
