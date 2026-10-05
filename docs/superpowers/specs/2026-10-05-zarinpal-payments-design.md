# ZarinPal payments: design

Status: draft for review, 2026-10-05. Builds on the order requests design (`2026-10-04-order-requests-design.md`) and
the paid tickets design (`2026-10-03-paid-tickets-design.md`). ZarinPal API: payment request, StartPay and verify in
https://www.zarinpal.com/docs/paymentGateway/connectToGateway.html.

## 1. Goal

A signed-in user pays for a ticket online with an Iranian card through ZarinPal, and the ticket is live as soon as the
payment is verified. No admin action is needed. The manual order request flow stays as it is for everyone else
(visitors, other currencies, or when payments are off).

The ZarinPal terminal is registered for the domain `rahkar.pro`, with the server IP `3.139.146.5` (the Lightsail
static IP, from which the gateway's own calls to ZarinPal leave). Its merchant ID is pending; until it is set,
nothing in this design is visible.

## 2. Configuration

- `ZARINPAL_MERCHANT_ID` in the environment (`gateway.env`), like the other secrets.
- A `[zarinpal]` section: `sandbox = false` (true sends every call to ZarinPal's sandbox, for testing).
- Payments need IRT among `[tickets.currencies]` (`[tickets.currencies.IRT]`, `round_to = 1000`), and
  `[listener] home_url`, which carries the callback (section 4).
- **Payments are on** when tickets are enabled, the merchant ID is set and IRT is configured. A `[zarinpal]` section
  with any of these missing logs a warning at startup and leaves payments off.

The admin sets the USD→IRT rate on the Pricing tab's existing Exchange rates card, as for any currency. Payment uses
the same 36-hour stale rule as the grant form: with a stale or missing IRT rate, the Pay button is not offered and the
server refuses to start a payment (409, "Online payment is paused; try again later or send an order request.").

## 3. Data

New table `payments`, one row per attempt (an order can have several: a cancelled attempt, then a paid one):

| column | meaning |
|---|---|
| `id` | primary key |
| `order_id` | the order it pays for (orders are never deleted) |
| `user_id` | the payer (the order's user) |
| `created_at`, `updated_at` | epoch seconds |
| `amount` | Toman, an integer: the order's `quoted_amount`, fixed when the payment starts |
| `authority` | ZarinPal's authority for this attempt; unique |
| `status` | `started`, `cancelled`, `failed`, `expired`, `paid`, `paid_unfulfilled` |
| `ref_id`, `card_pan` | from a successful verify; `card_pan` is ZarinPal's masked number (`502229******5995`) |
| `error` | ZarinPal's code and message for a refused request or verify, for the admin |

Status changes: `started` → `cancelled` (callback `NOK`), `failed` (verify refused), `expired` (no callback within an
hour), `paid` (verified and the ticket granted) or `paid_unfulfilled` (verified, but the grant was refused). Only a
`started` payment changes; every other status is final.

The order keeps its own statuses: a paid payment closes it as `done` with the ticket, exactly as an admin grant does. A
`paid_unfulfilled` payment leaves the order open, so the admin can grant from it as today.

## 4. Flow

**Start.** In the dashboard's Order dialog (signed-in users only), with payments on and IRT chosen, the button reads
**Pay 1,250,000 Toman with ZarinPal** next to the existing **Send request**. It calls `POST /api/orders/pay
{tier, length}`, which:

1. creates the order with `orders.create` (same checks: one open order, 3 a day, sold out) in IRT; the order's
   `message` is empty and no "new order" emails go out yet;
2. refuses a stale or missing IRT rate (section 2);
3. calls `request.json` with `merchant_id`, `amount` (the quote), `currency: "IRT"`, a description
   ("Claude Gateway: Lite, 1 week, order #12"), `callback_url = <home_url>/pay/callback` and `metadata`
   (`email`, `order_id`);
4. stores the payment as `started` with the authority and returns `{"url": "https://payment.zarinpal.com/pg/StartPay/<authority>"}`
   (`sandbox.zarinpal.com` with `sandbox = true`); the browser goes there.

If ZarinPal refuses the request or cannot be reached (10-second timeout), the order is withdrawn, nothing is stored as
paid, and the dialog shows "ZarinPal is not available right now; try again or send an order request." (502).

The amount never comes from the browser: it is the order's quote, computed on the server.

**Callback.** ZarinPal sends the buyer to `https://rahkar.pro/pay/callback?Authority=…&Status=OK|NOK`. The home host
serves this path itself (it is added to `HomeHost.PUBLIC`), since its domain is the registered one. No session is
needed: the authority identifies the payment, and the ticket only ever goes to the payment's own user.

- Unknown authority: 404 page.
- Payment no longer `started` (a repeated callback, or a reload): no ZarinPal call; redirect to the result.
- `NOK`: `cancelled`; the order is withdrawn; redirect to the dashboard with "Payment cancelled; nothing was charged."
- `OK`, but the order is no longer open (withdrawn, declined, or granted by hand): `cancelled` without verifying
  (ZarinPal returns an unverified payment's money).
- `OK`: call `verify.json` with `merchant_id`, the stored `amount` and the authority.
  - Code 100 or 101: store `ref_id` and `card_pan`, then grant (below).
  - Any other code: `failed`, the order is withdrawn, and the buyer sees ZarinPal's message and "If money left your
    account, ZarinPal returns it within 72 hours."
  - ZarinPal unreachable (or a success without a `ref_id`): the payment stays `started` and the buyer sees "Your
    payment is being confirmed with ZarinPal. The result will be emailed to you within the hour." Reconcile (below)
    retries the verify (101 makes this safe).

**Grant.** `tickets.grant` with `order_id` and a new `paid=True`, which grants at the order's quote: `quoted_usd` and
`quoted_rate` are used as the price instead of today's, there is no stale-rate confirmation and no quote check. The
order closes as `done` and the payment becomes `paid` in the same transaction. Capacity is checked as for any grant.
If the grant is refused (capacity filled since the start, user revoked), the payment becomes `paid_unfulfilled`, the
order stays open, and the buyer sees "Your payment went through (reference 12345678), but the ticket could not be
issued automatically. The admin has been told and will sort it out."

**Result page.** The callback redirects to `<dashboard_url>/dashboard#payment/<id>`. A signed-in buyer sees the
outcome: on success "Paid. Reference 12345678." and their new ticket. A browser that is not signed in (sessions live
on the dashboard host) sees the sign-in screen first, then the same result.

**Reconcile (expiry).** Every 5 minutes, a task verifies each payment still `started` an hour after it began, with
its stored amount: 100/101 grants it (or `paid_unfulfilled`) and sends the section 5 mail once; a refusal marks it
`expired` and withdraws its order; ZarinPal unreachable leaves it `started` for the next run. With payments off, or
its order no longer open, it is closed (`expired` / `cancelled`) without verifying. ZarinPal returns money for a
payment that was never verified.

## 5. Mail

- **Admin, on `paid`:** "Paid: Lite, 1 week, 1,250,000 Toman, reference 12345678, by Alice; the ticket is live."
- **Admin, on `paid_unfulfilled`:** the same, ending "The ticket was NOT granted: <reason>. Grant it from the order or
  refund the payment."
- **Buyer, on `paid`:** a receipt with the ticket, its start and end, and the reference.

Mail failures never affect the payment, as for orders today.

## 6. Admin dashboard

No new tab. The Orders tab shows a paid order's payment: status, amount, reference and masked card. A
`paid_unfulfilled` payment shows on its open order as **Paid, ticket not granted**, and the existing grant dialog
works from it. The audit log records `payment_started`, `payment_paid`, `payment_unfulfilled`, `payment_failed`,
`payment_cancelled` and `payment_expired`.

## 7. Security

- The server computes the amount and verifies with the stored amount; the callback's query string only names the
  payment.
- A ticket is granted at most once per payment: the status change from `started` happens inside the grant's
  transaction, and a payment that is no longer `started` is never verified again.
- The merchant ID is a secret: it is never sent to the browser or logged.
- Card data: only ZarinPal's masked `card_pan` is kept; `card_hash` is not stored.

## 8. Testing

A fake ZarinPal (as `FakeUpstream` fakes Anthropic) covers: paid; cancelled (`NOK`); verify refused; a repeated
callback and a reload after paid (101, one ticket); capacity gone after the start (`paid_unfulfilled`, order open);
a stale or missing IRT rate (no payment starts); ZarinPal unreachable at start and at verify; expiry; an unknown
authority; and the amount sent to ZarinPal matching the quote. After deploying, one manual payment against the
sandbox (`sandbox = true`), then one small real payment.

## 9. Not in this design

Visitors paying without an account, refunds from the dashboard (ZarinPal's reverse), payment in currencies other than
Toman, and fee display.
