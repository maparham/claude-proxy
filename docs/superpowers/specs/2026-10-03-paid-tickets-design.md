# Paid tickets

- **Date:** 2026-10-03
- **Builds on:** `2026-09-21-claude-proxy-design.md` (sections 7, 8 and 17.2: quota attribution and share limits) and `2026-09-26-signup-and-browser-authorization-design.md` (sign-up credit).
- **See also:** `2026-10-03-ticket-window-alignment-note.md`, which records why the day is the unit and the alternative (prorating by window overlap) kept for later.
- **Goal:** people buy a slice of the Claude subscription for 1 day, 1 week or 1 month. Each slice is reserved, so a sold slice is always available to its buyer. The admin sets prices, discounts and bonuses from the dashboard.

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

- **Shares** stay in the config. A share change alters capacity, so it is not a dashboard click. Existing tickets keep the share they were sold with.
- **`default_usd`** seeds the prices table on first start (section 6). After that the database holds the prices.
- **Currencies.** Adding a currency is one config entry plus a daily rate. EUR is the only one for now.
- **Lengths** are fixed: day = 1 day, week = 7 days, month = 30 days. A day is 24 hours from the ticket's start, so a ticket's day boundaries follow its own start time, not the calendar.

## 4. Data

New tables:

- **`ticket_prices`:** `tier`, `length` (`day`/`week`/`month`), `usd`, `updated_at`, `updated_by`. One row per tier and length.
- **`ticket_discounts`:** `id`, `tier`, `length`, `usd`, `starts_at`, `ends_at`, `created_by`, `created_at`, `cancelled_at`. At most one active discount per tier and length at any moment; the dashboard refuses an overlapping one.
- **`fx_rates`:** `currency`, `rate` (local units per 1 USD), `set_at`, `set_by`. A history; the newest row per currency is the current rate.
- **`tickets`:** `id`, `user_id` (nullable, see section 10), `user_name` (copied at grant), `account_id`, `tier`, `share_pct` (copied at grant), `length`, `days` (1, 7 or 30), `starts_at`, `ends_at` (`starts_at` + `days` × 24 h), `list_usd` (regular price at grant), `usd` (charged, after any discount), `discount_id`, `currency`, `rate`, `amount` (local amount charged, rounded), `granted_by`, `granted_at`, `cancelled_at`, `cancelled_by`, `note`.
- **`ticket_bonuses`:** `id`, `ticket_id`, `share_pct` (0 or more), `extra_days` (whole days added to the ticket's end, 0 or more), `starts_at`, `ends_at`, `note` (shown to the user), `granted_by`, `granted_at`, `cancelled_at`.

A ticket's effective end is `ends_at` plus the `extra_days` of its non-cancelled bonuses. Bonus days continue the ticket's own day boundaries. A ticket *covers* a moment when it is at or after `starts_at` and before the effective end.

## 5. Capacity

Capacity is reserved in time. For an account, at any moment `t`:

```
sold(t) = sum of share_pct of non-cancelled tickets covering t
        + sum of share_pct of non-cancelled bonuses covering t
```

- **The rule.** Granting a ticket or a bonus is allowed only if `sold(t) + new share ≤ 100` for every `t` in its period. Extending a ticket by bonus days checks the extended period at the ticket's share.
- **Checking.** `sold` only changes at period start and end points, so the check evaluates those points within the new period.
- **Why per moment is enough.** Anthropic's weekly budget resets on its own schedule, which ticket dates do not follow. Because every ticket is enforced per day at one seventh of its share (section 7), any Anthropic week contains at most seven days of a given slice, so a slice never draws more than its share from one week however the tickets in it turn over. The note in `2026-10-03-ticket-window-alignment-note.md` works this through.
- **Queued tickets** reserve their future period the same way. Expired and cancelled ones stop counting, so slices free up without any job.
- **Atomic.** The check and the insert run in one `BEGIN IMMEDIATE` transaction, so two admins granting at once cannot oversell.
- **Sold out.** A tier and length is sold out when a ticket starting now would fail the rule.

## 6. Prices, discounts and currency

- **Regular price.** `ticket_prices`, edited in the admin's Pricing panel. Each change is written to the audit log.
- **Discount.** For one tier and length, a USD price for a period. While active it replaces the regular price everywhere a price is shown or charged. When it ends the regular price returns by itself.
- **Local price.** `round(usd × rate, round_to)`, rounding to the nearest step: $20 × 0.92 = 18.40 becomes €18.50.
- **Fixed at grant.** A ticket stores `list_usd`, `usd`, `rate` and `amount`. Later changes to prices, discounts or rates never touch existing tickets.
- **Stale rate.** A rate older than 36 hours shows a warning on the admin's rates panel and in the grant form. Granting still works after the admin confirms.
- **No rate.** A currency without any rate cannot be used for a grant and is hidden on `/pricing`.
- **No VAT** in step 1.

## 7. Enforcement

- **Who.** A user becomes ticket-gated when they are granted their first ticket. Admins, users with hand-set limits and new sign-ups on the free credit are unaffected.
- **Credit.** Granting a first ticket removes the user's `cost_total` sign-up credit row.

On every request from a ticket-gated user, `limits.evaluate` (and `limits.states`, for the dashboard) looks for the ticket covering now:

- **Active ticket.** Two share limits are built from it, in place of any `share_5h`/`share_7d` rows an admin set for that user:
  - `share_5h` = ticket share + active bonus share, against Anthropic's 5-hour window as today.
  - `share_day` = (ticket share + active bonus share) ÷ 7 (Lite: 0.71%), the slice of Anthropic's weekly budget the user may spend in the current ticket day. It is measured as the sum of the weekly-bucket share attributed to the user's requests since the current day began, not as the user's share of Anthropic's current weekly window. Each day starts at zero and the limit resets at the day boundary; unused share does not carry over.
  - Other limit rows (allowed models, per-minute caps, cost limits) still apply.
  - **Current day.** The ticket day containing now: whole 24-hour steps from `starts_at`, including bonus days.
- **No active ticket.** The request gets 402 with `"Your ticket ended on <date>. <how_to_buy>"`, or `"Your next ticket starts on <date>."` when one is queued. This includes third-party models, which share limits never covered.
- **Unchanged.** A stale account snapshot skips share limits, as today. An exceeded share returns the usual 429 with its reset time; for `share_day` that is the end of the current ticket day.

## 8. Dashboard

### Admin

- **Exchange rates.** One row per configured currency: current rate, when and by whom it was set, an input for today's rate, and a warning badge after 36 hours.
- **Pricing.** Tier by length grid of USD prices, editable. Under it, discounts: create (tier, length, price, start, end), list, cancel.
- **Tickets.**
  - **Grant form:** user, tier, length, currency, optional note. It shows the price (regular or discounted), the local amount at the current rate, the start (now, or queued after the user's last ticket) and whether the period is available.
  - **List:** active, queued and ended tickets, filterable by user.
  - **Cancel:** frees the slice and ends the ticket's bonuses. Queued tickets of that user move forward to close the gap unless the admin ticks "keep dates" (each moved ticket is re-checked against capacity; if one fails, the cancel is refused with the reason). Refunds happen outside the app.
  - **Bonus:** on a ticket, add extra share and/or extra days with a period and a note for the user. It is checked against capacity like a ticket.
- **Capacity.** Share sold on the account now, and the highest `sold(t)` over the next 30 days.

### User

- **Current ticket:** tier, end date (with "+N days bonus" when extended), any queued ticket.
- **Usage bars:** the 5-hour bar as today, and a "today" bar in place of the weekly one: share used in the current ticket day against `share_day`, with the time until the day ends.
- **Bonus badge:** "Bonus: +3% until Oct 20" with the admin's note. Both bars show the larger limit.
- **Price list:** in the user's currency, with availability, usage hints (section 9), active discounts with a live countdown, and `how_to_buy`.

Non-admins still never see the account's usage, the number of seats sold or anything about other users.

## 9. Public pricing page

`GET /pricing`, no sign-in:

- **Table:** tiers by length in EUR, with a sold-out badge per tier and length.
- **Discounts:** the regular price struck through, the discounted price, and a live countdown such as "Offer ends in 2d 14h 03m", ticking every second in the browser. At zero the page reloads with the regular price.
- **Rate date:** "Prices converted at the rate of <date>".
- **Usage hints:**
  1. **Comparison:** the tier's `compare` text, e.g. "≈ Claude Pro".
  2. **Measured estimate:** hours of active use per 5-hour window and per day, as a range, for Sonnet and Opus separately. Computed daily from the last 30 days:
     - An *active hour* is a user's clock hour with at least one forwarded Anthropic request.
     - For each active hour, the 5-hour share used in it (from `quota.attribution`) is recorded per model family, by which family carried most of that hour's weighted tokens.
     - Hours per window = tier share ÷ share per active hour, shown as the 25th–75th percentile range. Per day uses the weekly-bucket share per active hour the same way, against the tier share ÷ 7.
     - Hidden for a model family until it has at least 50 active hours of data.
- **`how_to_buy`** text.

The page shows nothing else about the account.

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

- **Capacity:** overlapping periods, queued tickets reserving their slot, bonuses counting, expiry and cancellation freeing the slice, two concurrent grants not overselling.
- **Enforcement:** tier share for the 5-hour window, `share_day` = share ÷ 7 measured over the current ticket day, the day boundary resetting it, consecutive days of one ticket independent of each other, a slice handed from one user to the next mid-week never drawing more than its share from one Anthropic week, bonus share added, bonus days continuing the day boundaries, admin share rows replaced, other limit rows kept, ended and queued messages, third-party models blocked without a ticket, ungated users unchanged, sign-up credit removed at first grant.
- **Prices:** rounding to 0.50, rate and price fixed at grant, stale-rate warning, missing rate, discount start and end, overlapping discount refused.
- **Web:** admin endpoints (rates, prices, discounts, grant, cancel, bonus), `/pricing` without sign-in and its sold-out badges, no account detail leaked to non-admins, usage estimate hidden below 50 active hours.
- **Deletion:** refused with a live ticket, tickets kept with `user_name` after deletion, typed confirmation.
