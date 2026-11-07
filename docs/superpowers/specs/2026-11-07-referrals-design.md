# Referrals

Date: 2026-11-07. Status: design, not built.

## Why

Users should bring in new users. A user shares a personal link; a friend who signs up through it and buys a pass gets
a discount, and the user who shared it earns a discount on a later pass. The system must not lose money: no discount
ever pushes a pass below what it costs the gateway on the Anthropic account.

## Goals

- Every signed-in user has a personal referral link, shown on the dashboard and in the Telegram bot.
- A friend who signs up through it gets a discount on their first pass.
- The referrer earns one discount unit for each friend whose purchase counts.
- The admin controls every number from a new **Referrals** tab and can turn the whole system off.

## Non-goals

- No cash, rial credit, or payouts of any kind. Rewards are only discounts on the referrer's own future passes.
- No fraud detection beyond the rules below. The owner decided (2026-11-07) that a determined cheater using a second
  account is acceptable: each unit still needs a real, paid pass above cost. So no card fingerprint, IP, device, or
  "suspicious" flags.
- No bonus tiers (extra rewards at 3 / 10 friends). Left out to keep it simple (my call; easy to add later).

## Anti-goals (a failure even if it works)

- A pass sold below its cost on the Anthropic account.
- A discount that can be turned into money or moved to another account.
- Rewards stacking into a large discount on one purchase.

## How it works

### The link and who counts as a friend

- Each user has a short code; the link is `https://rahkar.pro/r/<code>` (home host). The bot shows the same web link:
  the bot cannot create accounts (it only links a chat to an existing one), so a friend always signs up on the web.
- `/r/<code>` stores the code and also passes it through the Clerk sign-up redirect in the URL, so it survives even
  where the cookie does not.
- Opening the link stores the code in a cookie for 30 days, then sends the visitor to the home page. The first code
  wins; a later link does not replace it.
- The referrer is fixed **when the account is created** through Clerk sign-up, and never changes after. Accounts the
  admin creates (CLI) have no referrer. An existing account cannot be attached to a referrer later.
- A user cannot refer themselves (same user id). Nothing else is checked.

### Which orders take part

- Only orders from a signed-in user, for a new pass, at a normal checkout. Left out of everything (no friend discount,
  no unit used, never earn a unit): upgrades (`upgrade_of` set), and the
  admin's payment links (their price is hand-set).
- During a sale (`ticket_discounts`), the referral discount applies on top of the sale price. The cost floor still
  holds, so stacking can never go below cost.

### The friend's discount

- A friend gets the **friend discount** (default 10%) on their **first pass**: the first order of an account that has
  never had a pass granted (by purchase or by the admin).
- Applied when the order is created, recorded on the order, so the quote the friend sees and pays already includes it.

### The referrer's reward: discount units

- When a friend's purchase **counts** (below), the referrer earns **one unit**.
- A unit is a percentage off one pass (default 10%). The size is set by the admin; a unit keeps the size it had when it
  was earned (my call, so changing the setting does not reprice saved units).
- **At most one unit per purchase.** Three saved units mean 10% off each of the next three passes, never 30% off one.
- A unit is used automatically on the referrer's next order (oldest unit first). It is held by the order while the
  order is open, used for good when that order is paid or granted, and returned if the order is withdrawn, declined,
  or its payment fails.
- Units expire after a set time (default 3 months) if unused.
- Limits: most units held at once (default 5), most units earned per calendar month (default 5). A friend's purchase
  over either limit earns nothing (it is logged as "over limit").

### When a friend's purchase counts

All of these:

1. It is the friend's first pass (the same purchase that got the friend discount).
2. It was paid (online through ZarinPal, or a manual order the admin marked done) and the pass was granted.
3. A waiting time (default 7 days) has passed since the grant, and the order has not been withdrawn or refunded in that
   time. During the wait the reward shows as "pending" to the referrer.
4. If approval is set to **manual**, the admin approved it. Default is **automatic**.

The admin can cancel any unused unit from the tab (for example after a refund made outside the system).

### The cost floor

- The admin sets one number: the account's cost on Anthropic per 30 days (default $100, worked out from the owner's
  figure of $20 per 30 days for Lite at its 20% share), plus the payment fee percentage (default 0).
- Cost of a pass = account cost × the tier's share at the time × days ÷ 30. Today: Lite (20%) $0.67 a day,
  Standard (40%) $1.33 a day. Using the share means changing shares on the Pricing tab keeps the floor right.
- Any discount (friend or unit) is reduced so that the price before VAT, minus the payment fee, is at least the cost.
  If it is reduced to zero, the unit is not used and stays saved.
- With today's prices and defaults no pass hits the floor at 10%: the smallest room is the Standard week (38%).
- VAT (10%) is added after the discount, as today. VAT is never discounted.
- Friend discount and a unit never apply together: a first pass gets only the friend discount (my call; a new account
  has no units anyway).

## Admin: the Referrals tab

A new dashboard tab, admin only.

**Settings** (each change is logged, like the prices table):

| Setting | Default |
|---|---|
| Referrals on/off | off until the admin turns it on |
| Friend discount on first pass | 10% |
| Size of one unit | 10% |
| Most units held | 5 |
| Most units earned per month | 5 |
| Unit lifetime | 3 months |
| Waiting time before a purchase counts | 7 days |
| Approval | automatic (or manual) |
| Account cost per 30 days | $100 |
| Payment fee | 0% |

Turning referrals off stops new links, new friend discounts and new units; purchases already waiting still finish
and earn their unit, and saved units stay usable unless the admin cancels them (my call). If the referrer's or the
friend's account is deleted or disabled during the wait, no unit is earned.

**Waiting for approval** (shown only when approval is manual, or when items exist): each friend purchase that has
passed the wait, with referrer, friend, pass, price paid, and Approve / Refuse.

**Referrers:** each user with a code: friends signed up, friends who bought, units saved, units used, units expired.

**History:** every event: friend signed up, purchase pending, unit earned, refused, over limit, used (on which order,
how much off), expired, cancelled.

## User-facing

- Dashboard: a "Invite friends" card with the link, a copy button, the friend's discount, units saved (with expiry
  dates), and pending rewards. Persian and English, following the existing i18n.
- Order dialog and price list: when a discount applies, show the original price, the discount, and the final price.
- Telegram bot: a command or button that shows the same link and units.
- Emails: the buyer's order and receipt emails show the discount line.

## What exists to build on

- Prices: `tickets.price_now()` gives the USD price; `orders.create()` turns it into the quote
  (`quoted_usd`, `quoted_amount`) with VAT. The discount belongs here, recorded in new order columns.
- Payments: `payments.py` grants on a verified ZarinPal payment; manual orders are closed by `orders.close_for_grant()`.
- Users are created through Clerk sign-up and the Telegram bot.
- Dashboard tabs live in `static/app.js` with strings in `static/i18n.js`.

## Left to the builder

(No page orders without signing in: `POST /api/orders` still exists on the server but nothing calls it, so it is
ignored here.)

- Table and column names, and the code format.
- Where the "counts" check runs (on a timer, or when a related page loads).
- How a manual order's refund or withdrawal after grant is detected, if at all, beyond the admin's cancel button.
- Exact layout of the tab and the user card, following the existing dashboard style.
