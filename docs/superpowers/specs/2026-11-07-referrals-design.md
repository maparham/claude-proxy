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
- The code travels by cookie only: `/r/<code>` stores it, and the Clerk sign-up reads it back from that cookie. There is
  no URL fallback through the Clerk redirect (ruled at the final review): a visitor whose browser drops the cookie
  signs up with no referrer.
- Opening the link stores the code in a cookie for 30 days, then sends the visitor to the home page. The first code
  wins; a later link does not replace it.
- The referrer is fixed **when the account is created** through Clerk sign-up, and never changes after. Accounts the
  admin creates (CLI) have no referrer. An existing account cannot be attached to a referrer later.
- A user cannot refer themselves (same user id). Nothing else is checked.
- The friend keeps the referrer's code on their account even if the referrer later deletes their account
  (`db.delete_user`): the friend still gets their discount, the deleted referrer simply earns nothing.

### Which orders take part

- Only orders that have a user when they are created, for a new pass, at a normal checkout. A visitor order the admin
  later links to an account (`orders.link`) stays out. Left out of everything (no friend discount,
  no unit used, never earn a unit): upgrades (`upgrade_of` set), and the
  admin's payment links (their price is hand-set).
- During a sale (`ticket_discounts`), the referral discount applies on top of the sale price. The cost floor still
  holds, so stacking can never go below cost.

### The friend's discount

- A friend gets the **friend discount** (default 10%) on their **first pass**: the first order of an account whose
  **email** has never had a pass granted, by purchase or by the admin, on this account or any earlier one. Checking by
  email stops "delete the account, sign up again with the same email" from getting the discount again (the audit log
  already keeps the sign-up and self-delete rows needed to find earlier accounts).
- Applied when the order is created, and the discount percentage is recorded on the order.

### The discount must survive every way an order is granted

Today only an order paid through ZarinPal is sold at its stored quote (`tickets.grant` with `paid=True`,
tickets.py:833-837). An order the admin grants by hand is priced again from today's price list (tickets.py:829,
838-840), and the dashboard sends the price its preview showed (web.py:1352-1361). Without a change, a manual order
would lose its referral discount or be refused with "price changed".

Rule: whenever an order is granted, its recorded referral discount percentage is applied to the price used at that
moment, and the cost floor is checked again on that price. The admin's grant preview shows the discounted price, so
the preview and the grant agree.

### The referrer's reward: discount units

- When a friend's purchase **counts** (below), the referrer earns **one unit**.
- A unit is a percentage off one pass (default 10%). The size is set by the admin; a unit keeps the size it had when it
  was earned (my call, so changing the setting does not reprice saved units).
- **At most one unit per purchase.** Three saved units mean 10% off each of the next three passes, never 30% off one.
- A unit is used automatically on the referrer's next order (oldest unit first). It is held by the order while the
  order is open, used for good as soon as any payment on that order is verified (even if the pass then fails to start,
  `paid_unfulfilled`) or the order is granted, and returned if the order is withdrawn, declined, or closed with no
  verified payment.
- A unit held by an open order stays with that order even if it expires or the admin cancels it meanwhile; the order
  keeps its discount (my call: the buyer already saw that price).
- No double use: a user can have only one open order, checked inside one locked transaction (orders.py:159-160).
  Holding the unit happens inside that same transaction.
- Units expire after a set time (default 3 months) if unused.
- Limits: most units held at once (default 5), most units earned per calendar month in Tehran time (default 5). A friend's purchase
  over either limit earns nothing (it is logged as "over limit").

### When a friend's purchase counts

All of these:

1. It is the friend's first pass (the same purchase that got the friend discount).
2. It was paid (online through ZarinPal, or a manual order the admin marked done) and the pass was granted.
3. If approval is set to **manual**, the admin approved it. Default is **automatic**.

There is no waiting time: passes are never refunded, so the unit is earned the moment the friend's pass is granted
(or, with manual approval, when the admin approves). The check runs inside the grant itself, using the grant's
`after` hook that payments already use (tickets.py:860-861). The admin can still cancel any unused unit from the tab.

### The cost floor

- The admin sets one number: the account's cost on Anthropic per 30 days (default $100, worked out from the owner's
  figure of $20 per 30 days for Lite at its 20% share), plus the payment fee percentage (default 0).
- Cost of a pass = account cost × the tier's share at the time × days ÷ 30. Today: Lite (20%) $0.67 a day,
  Standard (40%) $1.33 a day. Using the share means changing shares on the Pricing tab keeps the floor right.
- Any discount (friend or unit) is reduced so that what the gateway keeps is at least the cost. What it keeps = the
  price before VAT, minus the payment fee, where the fee is taken on the full amount charged (VAT included), since
  ZarinPal charges on that.
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
| Approval | automatic (or manual) |
| Account cost per 30 days | $100 |
| Payment fee | 0% |

Turning referrals off stops new links, new friend discounts and new units; rewards already waiting for the
admin's approval can still be approved, and saved units stay usable unless the admin cancels them (my call). A
disabled or deleted referrer earns nothing.

**Waiting for approval** (shown only when approval is manual, or when items exist): each friend purchase waiting for a decision, with referrer, friend, pass, price paid, and Approve / Refuse.

**Referrers:** each user with a code: friends signed up, friends who bought, units saved, units used, units expired.

**History:** every event: friend signed up, waiting for approval, unit earned, refused, over limit, used (on which order,
how much off), expired, cancelled.

## User-facing

- Dashboard: a "Invite friends" card with the link, a copy button, the friend's discount, units saved (with expiry
  dates), and rewards waiting for the admin's approval. Persian and English, following the existing i18n.
- Order dialog and price list: when a discount applies, show the original price, the discount, and the final price.
  The signed-in user's price list asks for every pass's quote at once (`/api/referrals/quotes`); the order dialog's
  Pay button shows the discounted Toman amount ZarinPal will charge. During a sale the regular price stays struck
  through and the note names both discounts. The Telegram bot's purchase summary shows the same three lines.
- Telegram bot: a command or button that shows the same link and units.
- Emails: the buyer's order and receipt emails show the discount line.

## What exists to build on

- Prices: `tickets.price_now()` gives the USD price; `orders.create()` turns it into the quote
  (`quoted_usd`, `quoted_amount`) with VAT. The discount belongs here, recorded in new order columns.
- Payments: `payments.py` grants on a verified ZarinPal payment; manual orders are closed by `orders.close_for_grant()`.
- Users are created through Clerk sign-up, or by the admin. The Telegram bot never creates users; it only links a
  chat to an existing account.
- Dashboard tabs live in `static/app.js` with strings in `static/i18n.js`.

## Left to the builder

(No page orders without signing in: `POST /api/orders` still exists on the server but nothing calls it, so it is
ignored here.)

- Table and column names, and the code format.
- Exact layout of the tab and the user card, following the existing dashboard style.
