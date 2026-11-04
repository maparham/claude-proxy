# Admin payment links: design

Status: revision 2, 2026-11-04, after review. Builds on the ZarinPal payments design
(`2026-10-05-zarinpal-payments-design.md`) and the order requests design (`2026-10-04-order-requests-design.md`).

## 1. Goal

The admin issues a payment link for a pass to an email address, with a price and a deadline, from the Orders tab, and
sends it however they like. The recipient opens the link, signs in (or signs up) with that email through Clerk, and
pays through ZarinPal from the dashboard. The pass is granted to their account as soon as the payment is verified.

ZarinPal's own payment links are not used: their API has no documented way to create one, and a payment made through
one grants nothing by itself. ZarinPal support confirmed (2026-11-04) that the terminal's custom subdomain
(`pay.rahkar.pro`) serves only those links, and that a website's payments go to
`https://payment.zarinpal.com/pg/StartPay/`, which this design keeps.

## 2. Data

A payment link is an order with two new columns, both NULL on every other order:

- `orders.pay_code TEXT`: 22 characters of `secrets.token_urlsafe(16)`, so it cannot be guessed. SQLite cannot add a
  UNIQUE column, so the migration adds the column and then `CREATE UNIQUE INDEX idx_orders_pay_code ON orders(pay_code)`
  (as `idx_users_clerk` is done).
- `orders.pay_by INTEGER`: the deadline (Unix time) the admin set.

A link order starts as a visitor-style order: `user_id` NULL, `name` and `email` both the address the admin typed,
`ip` NULL, `currency` IRT, `lang` `fa`, `start_now` 0, `admin_mail` and `buyer_mail` `off`. The recipient claims it
when they pay (section 4), which sets `user_id`.

The order's quote is what the link charges. A paid grant already sells at the order's quote
(`tickets.grant(..., paid=True)` copies `quoted_usd` into the pass), so an edited price needs no change there.

## 3. Creating a link (admin)

`POST /api/admin/orders/link`, admin only, with `email`, `tier`, `length`, `pay_by`, an optional `amount` (Toman) and
an optional `note`. A new `orders.create_link(conn, cfg, actor, ...)` does it in one `BEGIN IMMEDIATE` transaction of
its own (it does not call `orders.create`, whose visitor and per-user limits do not apply to the admin):

1. Payments must be set up (404, "Online payment is not set up on this gateway.") and the Payment selector not
   `manual` (409, the existing `MANUAL_ONLY` message), as `payments.start` checks.
2. The IRT rate must be fresh (`payments.rate_ok`; 409, the existing `PAUSED` message), so a link never freezes a
   stale price.
3. `email` is a valid address (`orders._email`), `tier` is a configured tier and `length` one of day, week, month
   (400 otherwise). Sold out is checked as for a visitor order (`tickets.sold_out` for a pass starting now; 409, "That
   pass is sold out."). It is not checked again when the link is paid: a pass that cannot be granted then ends as
   `paid_unfulfilled`, which the admin already handles (refund mail, section 4 of the payments design).
4. `pay_by` is in the future and at most 30 days away (400 otherwise). The dialog offers 3 days by default.
5. The price: the list price for a pass bought now (`tickets.price_now`, `round_local`, VAT included). An `amount`, if
   given, is whole Toman from 1,000 to ten times the list price (400 otherwise) and replaces `quoted_amount`; then
   `quoted_usd = amount / (rate × (1 + vat_percent / 100))`, so the pass's recorded USD excludes VAT as every other
   pass's does.
6. The row is inserted complete (section 2) with `admin_note` = the note and `status` `new`, and audited as
   `order_paylink` with the admin as actor, the amount, whether it was edited, and the deadline.

The response is the order and its link: `{dashboard_url}/pay/{pay_code}`.

## 4. Paying

**The link.** `GET /pay/{code}` on either host redirects (302) to `{dashboard_url}/dashboard#paylink/{code}`, with no
lookup, so it says nothing about the code. The dashboard keeps `#paylink/<code>` through Clerk's sign-in and sign-up,
as it keeps `#authorize/…`, and opens it once signed in.

**What the signed-in user sees.** `GET /api/me/paylinks/{code}` answers one of:

- `invalid` (404): no such code, the order is no longer open (declined or withdrawn), or the deadline has passed.
  The dashboard says "This payment link is no longer valid." The same answer for all of them.
- `paid`: a payment on the order is `paid` or `paid_unfulfilled`. The dashboard says the payment was received and,
  for `paid_unfulfilled`, that the admin will contact them. Never offered again (this also closes a gap in today's
  `pay_order`, below).
- `other_email` (403): the signed-in account's email differs from the link's, ignoring case. The dashboard says "This
  link is for ma…@gmail.com; sign in with that email." The email is masked: the first two characters of the part before
  `@` (one if it has only one or two), then `…`, then `@` and the domain.
- `other_order` (409): the link is not yet claimed and the user has another open order. The dashboard says to cancel
  that order first.
- `open`: the pass, the price, the deadline, the terms and start line exactly as the order dialog shows them for this
  account (`tickets.next_start` for this user, `week_split`, the VAT line), the terms tick, and a Pay button. The
  admin's note is never sent.

**Pay.** `POST /api/me/paylinks/{code}/pay` with `terms_version` runs `payments.pay_link(conn, cfg, user, code,
terms_version)`:

1. The same checks as the GET, refusing with the same answers; plus the user must be enabled (409, "Your account is
   disabled; contact the admin.") and not revoked.
2. The terms version must be current (400, the existing `TERMS_CHANGED`); a newer tick updates the order's
   `terms_version`.
3. If the link is unclaimed, it is claimed in the same transaction as the checks: `user_id` = the user, `name` = the
   account's name, audited `order_paylink_claimed`. From then on it is the user's open order and the dashboard's order
   card shows it like any other, with Pay and Cancel (`withdraw`, which ends the link).
4. Payments set up and not `manual`, as in section 3. The stale-rate check does not apply: the price was fixed when the
   link was made, and the deadline bounds how old it can be.
5. A payment is opened through the existing `_open(..., withdraw=False)`, so a ZarinPal failure leaves the order open.

The response is `{"url": <StartPay address>}`, and the dashboard goes there with `goPay`, which sends the
`Referer: https://dash.rahkar.pro` that ZarinPal checks (the server's `Referrer-Policy: no-referrer` would otherwise
strip it). Coming back, `/pay/callback` is unchanged: it redirects to `#payment/<id>` and the existing notice.

**The dashboard's own Pay button** on a claimed link order goes through `pay_order` as for any order. Two changes
there, which apply to every order: `pay_order` refuses (409, the `paid` answer's message) when the order already has
a `paid` or `paid_unfulfilled` payment, so no order can be paid twice; and for an order with a `pay_code` it skips the
stale-rate check and refuses past `pay_by` (the `invalid` answer), as `pay_link` does.

**Several payments at once.** A link can have more than one `started` payment (two tabs). Money is safe by the
existing rule: the first payment to verify grants, and a later one is cancelled unverified (`_cancel_if_order_closed`)
or, if both verified before either wrote, recorded `paid_unfulfilled`. This design relies on that, not on any rate
limit.

## 5. The deadline

Past `pay_by`, the link answers `invalid` and nothing can start a payment on it. The hourly `reconcile` then closes it:
an open order with a `pay_by` more than an hour past, and no payment still `started` (reconcile expires those first),
becomes `declined` with the note "Payment link expired", audited with no actor. A payment started before the deadline
and verified after it still grants, because the order is still open until then.

## 6. Admin dashboard

- The Orders tab gets a **Payment link** button beside the filters. Its dialog: email, tier and length (the price
  list's choices), the price prefilled with the list price for the chosen pass and editable, the deadline (a date and
  time, 3 days ahead by default), and a note. Create shows the link with a Copy button.
- The order list returns `pay_url` and `pay_by` for link orders. Such a row shows "Link, pay by <date>" under the
  buyer, a **Copy link** button among its actions while it is open, and "Unclaimed" while `user_id` is NULL.
  Declining it is how the admin cancels a link.
- No mail is sent when a link is made, claimed, declined or expired (`admin_mail` and `buyer_mail` are `off`). A paid
  link sends the payment emails every paid order sends.

## 7. Security

- Only a signed-in user with the link's email can see its pass and price or pay it. Anyone else with the code sees, at
  most, that it exists, and its masked email.
- Clerk verifies the email, so the right person pays and the pass lands on their account; an admin's typo ends in
  `other_email`, never in someone else's account.
- `GET /pay/{code}` redirects without looking the code up.

## 8. Testing

Tests first, in `tests/test_paylinks.py` (and `tests/test_payments.py` for `pay_order`), with the fake ZarinPal used by
the payment tests:

- creating: valid; payments off (404); manual (409); stale rate (409); bad email, tier, length, deadline or amount
  (400); sold out (409); an edited amount stored with `quoted_usd` net of VAT; the audit row.
- viewing: `invalid` for an unknown, declined, withdrawn or expired code; `paid`; `other_email` with the masked
  address; `other_order`; `open` with no note in it.
- paying: claims on first pay (user, name, audit); terms required and updated; a disabled user refused; the stale-rate
  check skipped; a ZarinPal failure leaves the order open; a second pay after `paid_unfulfilled` refused.
- `pay_order`: refuses an order with a `paid_unfulfilled` payment; skips the rate check and enforces `pay_by` for a
  link order.
- reconcile: declines an expired link order an hour after its deadline, but not one with a payment still started.
- the edited price is what the granted pass records.
- `GET /pay/{code}` redirects without a lookup; the dashboard keeps `#paylink/<code>` through sign-in (a test like the
  existing `#authorize` one).

## 9. Not in this design

- Mailing the link from the dashboard: the admin sends it themselves.
- Links for upgrades, for other currencies, or for an amount without a pass.
- Re-checking sold out at pay time (section 3, step 3).
