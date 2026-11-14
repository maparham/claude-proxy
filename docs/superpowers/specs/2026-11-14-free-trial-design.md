# Free trial pass (2026-11-14)

A new user can try the gateway once, free: a short Lite-sized pass they claim themselves. Every figure is the admin's
to change on the Pricing tab.

## Settings (settings table, key `trial`; Pricing tab, "Free trial" panel)

| key            | default                     | meaning                                                                   |
|----------------|-----------------------------|---------------------------------------------------------------------------|
| enabled        | false                       | offered on the dashboard and home page; admin grants work either way      |
| duration_min   | 60                          | how long the pass runs once started (1–10080 minutes)                     |
| valid_hours    | 24                          | how long a claim waits to be started before it lapses (1–720 hours)       |
| tier           | the smallest-share tier     | the tier the pass is filed under (and its label's base)                   |
| share_pct      | null → the tier's share     | weekly share while it runs, as a sold pass of that tier (0 < x ≤ 100)     |
| share_5h_pct   | null → the tier's 5-h share | 5-hour share (0 < x ≤ 100)                                                |
| daily_cap      | 5                           | self-service claims in any rolling 24 hours (0–1000); admin grants exempt |
| follow_pct     | 10                          | discount on a first purchase after the trial (0–90; 0: none)              |
| follow_days    | 7                           | how long after the trial ends that discount lasts (1–90 days)             |

"What 1 day Lite takes in 1 hour" is Lite's own share: the trial is an hour of a Lite pass.

## Claiming and starting

- A **claim** (`trial_claims` row) is made by the user's "Start free trial" button, or by the admin for any user.
- Self-service needs: trial enabled, tickets enabled, the user enabled; no claim ever for this user id or email
  (lower-cased, kept after the account is deleted); `referrals.first_pass` (no pass ever, by id or email); the rolling
  24-hour count of self-service claims under `daily_cap`; Turnstile when the gateway has it configured.
- The admin grant skips those checks. When the user already had a trial or a pass, it needs `confirm` (409 otherwise).
  Admin grants are audited and not counted against the cap.
- A claim lapses `valid_hours` after it is made if not started. A lapsed claim still counts as the trial.
- **Starting** turns the claim into a ticket: on the user's first gateway request with no pass running (limits.evaluate),
  or the dashboard's "Start now". The ticket: `trial=1`, tier/share/5-h share from the settings (copied at start),
  `length='day'`, `days=0`, `starts_at=now`, `ends_at=now+duration`, price 0 in USD at rate 1. Capacity is checked like
  any sale; no room → the start is refused ("The free trial can't start right now; try again later.") and the claim stays.
  The first ticket removes the sign-up credit, as a sale does.
- On start the user gets the follow-up discount as a saved referral unit (`pct=follow_pct`,
  `expires_at = ends_at + follow_days`), so the existing order and payment code applies it. None when `follow_pct` is 0.
- A trial ticket cannot be upgraded or refunded (it has no payment); the admin can cancel it or add a bonus as usual.

## Where it shows

- **Dashboard (user):** with no pass and an offer open, a card "Free trial: {duration} of Lite, no payment" with a
  "Start free trial" button; after claiming, "Start now" and "Starts with your first request; claim lapses on {date}".
  The running trial reads "Free trial" in the pass card. `/dashboard?trial=1` scrolls to and highlights that card.
- **Admin:** Pricing tab panel with the settings and today's count; a "Grant trial" action on every user.
- **Home page:** with the trial enabled and today's cap not reached (`/api/pricing` → `trial: {duration_min}`), a
  "Try free for {duration}" button in the hero beside Sign in (sign-in popup, then `/dashboard?trial=1`) and the
  line "Free trial: {duration} of Lite, no payment" above the price cards. Persian: «آزمایش رایگان: {مدت} Lite، بدون پرداخت».
