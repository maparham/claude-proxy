# Admin payment links: design

Status: draft for review, 2026-11-04. Builds on the ZarinPal payments design (`2026-10-05-zarinpal-payments-design.md`)
and the order requests design (`2026-10-04-order-requests-design.md`).

## 1. Goal

The admin issues a payment link for a pass to anyone, by email, from the Orders tab, and sends it however they like.
The recipient opens the link without signing in, pays through ZarinPal, and the pass is granted to the account with
that email at once. They sign in with that email (Clerk) to use it.

ZarinPal's own payment links are not used: their API has no documented way to create one, and a payment made through
one grants nothing by itself. ZarinPal support confirmed (2026-11-04) that the terminal's custom subdomain
(`pay.rahkar.pro`) serves only those links, and that a website's payments must go to
`https://payment.zarinpal.com/pg/StartPay/`, which this design keeps.

## 2. Data

A payment link is an ordinary order with one new column:

- `orders.pay_code TEXT UNIQUE`: NULL for every other order. 22 characters of `secrets.token_urlsafe(16)`, so it cannot
  be guessed. Added by a migration like the other added order columns.

The order's quote (`quoted_usd`, `quoted_rate`, `quoted_amount`) is the price the link charges. A paid grant already
sells at the order's quote (`tickets.grant(..., paid=True)`), so an edited price needs no change in the grant.

## 3. Creating a link (admin)

`POST /api/admin/orders/link`, admin only, with `email`, `tier`, `length`, an optional `amount` (Toman) and an optional
`note`:

1. Payments must be on and the Payment selector must not be `manual` (409 otherwise, the messages payments already use).
2. The account: an active user whose email or name matches the email, ignoring case (as `orders.suggest_user` matches);
   else a new one named after the email with a $0 credit, exactly as the order "link → create" action does (one shared
   helper, not a copy). A revoked match is refused (409).
3. The order: `orders.create` for that account, with `currency=IRT`, `name` = the email, `lang="fa"`, `notify=False`,
   `terms_version` = the current terms version, and `start_now=False` (the pass starts at once if the account has none
   running, else after its current passes). So the usual rules hold: an unknown tier or length is 400, a sold-out pass
   is 409, and an account that already has an open order is 409 ("This account already has an open order; decline it
   first.").
4. An `amount`, if given, is a whole number of Toman from 1,000 up to the list price × 10; it replaces
   `quoted_amount`, and `quoted_usd` is recomputed from it at the order's rate so the two stay consistent. Without it
   the list price stands.
5. `pay_code` is set, `admin_note` takes the note, and the audit log records `order_paylink` with the amount and whether
   it was edited.

Steps 3 to 5 run in one transaction. The response is the order and the link: `{home_url}/pay/{pay_code}`.

## 4. The pay page

`GET /pay/{code}` is served on the home host (`[listener] home_url`, `rahkar.pro`), which is the domain registered
with ZarinPal: its Referer check on StartPay passes from there. `/pay/` joins `HomeHost.PUBLIC`. No sign-in.

What it shows, in Persian with the home page's English switch (`i18n.js`):

- **Open order:** the pass (tier and length), the price in Toman, the email partly hidden (`ma…@gmail.com`), the pass
  terms with the same tick the order dialog uses, and a Pay button. The note is never shown.
- **Paid:** "Your pass is ready" with the email partly hidden and a button to the dashboard's sign-in.
- **Anything else** (declined, withdrawn, or a code that does not exist): one sentence, "This payment link is no longer
  valid." Same page and status (404) for all, so a code's existence is not revealed.
- **After a failed or cancelled payment**, the open page with a one-line notice above it.

`POST /pay/{code}` with `terms_version` starts the payment: `payments.pay_link(conn, cfg, code, terms_version)`, which
checks as `pay_order` does (order open, IRT, payments on, selector not manual, the terms version current), then opens a
payment through the existing `_open(..., withdraw=False)`, so a ZarinPal failure leaves the order open. The stale-rate
pause does not apply: the price was fixed when the link was made. If the tick is for newer terms than the order's,
the order's `terms_version` is updated to it.

Abuse: a code is unguessable, and a link refuses a new payment while one on it was started under 30 seconds ago (409,
"A payment was just started; finish it or try again in a moment."). The response is `{"url": <StartPay address>}`
and the page redirects to it.

## 5. Coming back from ZarinPal

`/pay/callback` is unchanged except for its redirect: a payment whose order has a `pay_code` goes back to
`{home_url}/pay/{pay_code}` (which shows paid, or the open page with the failure notice); every other payment still goes
to the dashboard. The "pass granted" email goes out as for any paid order (`payments.send_mails`).

## 6. Admin dashboard

- The Orders tab gets a **Payment link** button beside the filters. Its dialog: email, tier and length (the price
  list's choices), the price prefilled from the price list for the chosen pass and editable, and a note. Create shows
  the link with a Copy button.
- An open order with a `pay_code` shows a **Copy link** button among its actions. Declining it is how the admin cancels
  a link.
- The order list returns `pay_url` for such orders.

## 7. Security

- The pay page and its POST reveal and accept nothing but what the code names; the note and full email stay hidden.
- Everything a page shows is escaped, as elsewhere.
- Creating an account from a typed email is safe for the reason the order link action gives: only the owner of that
  address can sign in to it through Clerk, and it has no credit until the pass is granted.

## 8. Testing

Tests first, in a new `tests/test_paylinks.py`, against the fake ZarinPal of `tests/test_zarinpal.py`:

- creating a link for an existing account and for a new email; a revoked account; an account with an open order;
  a sold-out pass; payments off or manual; an amount out of range; the edited amount stored with a consistent USD.
- the page for an open, paid, declined and unknown code; the email partly hidden; the note never shown.
- paying: terms tick required; the 30-second refusal; a ZarinPal failure leaves the order open.
- the callback's redirect to the pay page, and the grant at an edited price.

## 9. Not in this design

- Mailing the link from the dashboard (the admin sends it themselves).
- Links for upgrades, for other currencies, or for an amount without a pass.
- An expiry date: a link lives while its order is open.
