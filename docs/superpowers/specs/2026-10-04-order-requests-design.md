# Order requests: design

Status: draft for review, 2026-10-04. Builds on the paid-tickets design (`2026-10-03-paid-tickets-design.md`) and the
home-page design (`2026-10-04-home-page-design.md`), which replaced `/pricing` with the price cards on the home page.

## 1. Goal

Buying a ticket today means reading `how_to_buy` and contacting the admin by hand. Instead, a buyer files an **order
request**. The admin gets an email, sees the order on a new Orders tab, and turns it into a ticket with the existing
grant dialog. Payment stays manual and outside the app.

An order is a request, not a payment and not a reservation: it holds no capacity. Capacity is checked when the admin
grants.

## 2. Where buyers order

- **Visitors (home page cards).** Signed-out visitors see the price cards on `/` (`home.js`). With Turnstile configured
  (section 6), each card's **Get it** button opens an order dialog on the home page instead of linking to the dashboard.
  The dialog asks for name, email, currency and an optional message, shows the chosen tier, length and price at
  today's rate, carries a Cloudflare Turnstile check, and has a link "Or sign in to order", which goes to today's
  `/dashboard?tier=<id>&length=<len>`. Without Turnstile, **Get it** keeps today's link and nothing changes on the home
  page.
- **Signed-in users (dashboard price list).** Signed-in users never see `/` (it redirects to the dashboard). Their
  price list (`priceListCard`) gets an **Order** button next to each price. The pair picked on the home page (stored in
  `sessionStorage` by the home-page design) is pre-selected. The order is tied to their account; the dialog asks only
  for currency and an optional message.
- **Email of a signed-in user.** The order uses `users.email` (set by Clerk sign-in, so verified). A user without one
  types an address in the dialog. That typed address is stored **on the order only** and is never written to
  `users.email`: `clerk_account` links a Clerk sign-in to the user whose email or name matches the verified address, so
  writing an unverified address there would let anyone take over another person's next sign-in.
- Both dialogs show the price at today's rate (the same figures as the cards) and: "This is a request, not a payment.
  The admin will contact you with payment details."
- **Sold out.** A card or price shown as sold out cannot be ordered (the button is disabled, and the server refuses
  with 409). Sold out is judged as the price list judges it: for a ticket starting now. A signed-in user with a live
  ticket might still fit as a queued ticket after it; the order is refused anyway. This is a deliberate simplification:
  such a user can ask the admin directly, and the grant dialog checks the queued start as usual.

## 3. Data

New table `orders`:

| column | meaning |
|---|---|
| `id` | primary key |
| `created_at`, `updated_at` | epoch seconds |
| `user_id` | the ordering user, or the account a visitor order was linked to; NULL for an unlinked visitor order; `ON DELETE SET NULL` |
| `name`, `email` | visitor's name and email; for a signed-in user, their user name and the email used (account or typed) |
| `tier`, `length`, `currency` | what was ordered |
| `quoted_usd`, `quoted_rate`, `quoted_amount` | the price shown in the dialog; for reference only, the grant re-prices as usual |
| `message` | buyer's message, at most 1000 characters |
| `status` | `new`, `contacted`, `done`, `declined`, `withdrawn` |
| `admin_note` | at most 200 characters, admins only |
| `ticket_id` | the ticket granted from this order; NULL otherwise |
| `ip` | the visitor's IP address, for the per-IP limit; NULL for signed-in orders |
| `admin_mail`, `buyer_mail` | `pending`, `sent`, `failed`, `off` (no `[email]` config) or `skipped` (buyer cap reached) |
| `seen_at` | when the buyer dismissed a closed order on their dashboard; NULL otherwise |

**Open** means `new` or `contacted`; **closed** means `done`, `declined` or `withdrawn`.

**Status changes** (anything else is refused with 409):

| from | to | by |
|---|---|---|
| `new` | `contacted` | admin |
| `new`, `contacted` | `declined` (with note) | admin |
| `new`, `contacted` | `done` | a grant with this order (section 7) |
| `new`, `contacted` | `withdrawn` | the buyer |

The admin note can be edited in any status.

**Retention.** Orders, like tickets, are never removed and keep their name and email when a user is deleted. The `ip`
is needed only for the per-IP limit, so it is set to NULL when an order closes and on every open order older than 30
days (in the existing maintenance task). Messages are kept with the order.

## 4. Limits

- A signed-in user may have one open order. A second is refused with 409, "You already have an open order."
- A visitor: at most 3 orders per day per IP address, and 3 per day per email address (case-insensitive). Over either:
  429, "Too many orders today; try again tomorrow or sign in."
- All visitor orders together: at most 50 per day. Over it: 429, "Orders are busy today; please sign in to order."
- Name at most 100 characters, message at most 1000. Email must look like an address (`something@something.something`,
  no whitespace).
- Turnstile: the server verifies the token with Cloudflare's `siteverify` endpoint before anything is stored. Failure:
  400, "The verification failed; please try again." Cloudflare unreachable (10 s timeout): 503 with `Retry-After: 30`.
- "Per day" means the 24 hours before the request. Limit messages carry no numbers.
- **IP address.** `client_ip()` reads the address uvicorn reports, which trusts `X-Forwarded-For` only from
  `127.0.0.1`, matching the documented Caddy setup in front of the gateway. Behind a proxy on another host, or for
  visitors sharing one address (CGNAT, an office), many visitors look like one IP and the 3-per-day rule can block all of
  them. They can still sign in to order. The deploy README says so.

## 5. Emails

Sent over SMTP with Python's `smtplib` (no new dependency).

- **When.** The order is committed and the response sent first, with both mail columns `pending`. Then an
  `asyncio` task runs `await asyncio.to_thread(mail.send, ...)` for each email, with a 20-second socket timeout, and
  writes `sent` or `failed` back on the event loop (the shared SQLite connection is only used on the loop). A slow or
  unreachable mail server therefore never delays the response or blocks the loop. Mail left `pending` by a restart
  stays `pending` and is shown as such; it is not resent.
- **To the admin**, for every new order: subject `New order: <tier label> <length>`; body with buyer name, email,
  account (if any), tier, length, quoted amount, message, and a link to the Orders tab. The admin's own inbox is the
  only recipient of buyer-typed text.
- **To the buyer**, a confirmation: subject `Your order: <tier label> <length>`; body with what they ordered, the quoted
  amount, the `how_to_buy` payment text, and "This is a request; the admin will contact you." It contains **nothing the
  buyer typed**: no name, no message. Combined with Turnstile and the limits, the form cannot carry content to a
  stranger's inbox.
- **Buyer cap.** At most 3 buyer emails per address per day; past it the order is stored with `buyer_mail = skipped`.
  For visitors this repeats the per-email order limit; it exists for signed-in users, who have no per-email order limit
  (a user without an account email could type the same stranger's address on several accounts).
- **Header safety.** Name and email are stripped of CR and LF before use, and the buyer email goes only to the
  validated address.
- A failed send records `failed` and logs a warning; the order is kept. The Orders tab shows the mail state.
- Without `[email]`, both mail columns are `off`; the order is stored and shown as usual.

The sender is a small module (`mail.py`) with one function, `send(cfg, to, subject, body)`, so the alerts in backlog
issue #20 can reuse it.

## 6. Configuration

```toml
[email]                          # optional; without it no mail is sent
smtp_host = "smtp.example.com"   # required
smtp_port = 587                  # default 587 with STARTTLS; 465 means implicit TLS
smtp_user = "gateway@example.com"   # optional; without it no AUTH is attempted
from = "Claude Gateway <gateway@example.com>"   # required
admin_to = "admin@example.com"   # required
# password: SMTP_PASSWORD in the environment (required when smtp_user is set)

[tickets]
turnstile_site_key = "0x..."     # public; the secret is TURNSTILE_SECRET in the environment
```

Unknown keys are refused, like the other sections. A missing required key in `[email]` is a config error.
`turnstile_site_key` without `TURNSTILE_SECRET` in the environment logs a warning and the home page keeps today's
**Get it** link.

## 7. Admin dashboard

A new **Orders** tab, shown while tickets are on, with a badge counting `new` orders (refreshed with the dashboard's
usual reload).

- A table of orders, newest first, filterable by status (default: open). Each row: age, buyer (name, email, linked
  account), tier and length, quoted amount, message, status, mail state, admin note.
- **Actions**, following the status table in section 3:
  - **Contacted** (from `new`).
  - **Decline** (from `new` or `contacted`), asking for a note.
  - **Note**: edit the admin note, any status.
  - **Grant ticket** (from `new` or `contacted`) opens the existing grant dialog filled in with user, tier, length and
    currency, and sends `order_id` with the grant.
- **Linking a visitor order.** A visitor order has no account, so **Grant ticket** first asks the admin to link one:
  - The dialog suggests an existing user whose `users.email` or name matches the order's email (case-insensitive), but
    never links automatically; the admin confirms.
  - Or the admin creates a user named after the order's email. If that name or email is already taken, the server
    refuses with 409 and the dialog offers the existing account instead.
  - Creating an account named after an unverified address is safe: only the owner of that address can later sign in to
    it through Clerk, because `clerk_account` matches the verified sign-in address against the name.
  - The link is stored in `user_id` before the grant dialog opens.
- **Grant with `order_id`.** `POST /api/admin/tickets` accepts an optional `order_id`. In the same transaction as the
  grant, the server checks that the order is open and that its `user_id` equals the grant's user; otherwise it refuses
  with 409 ("This order was withdrawn or changed; reload.") and grants nothing. On success the order becomes `done` with
  `ticket_id` set. If the grant itself fails (capacity, stale rate, changed quote) the order is unchanged. This stops a
  withdraw that races the open dialog from producing a `done` order with no buyer.
- Every status change, link and note writes an audit-log row.

## 8. Buyer view

- A signed-in user with an open order sees it on their dashboard above the price list: "Order received: Standard,
  1 week. The admin will contact you." with a **Withdraw** button while the order is open (`new` or `contacted`).
  Order buttons are disabled while an order is open.
- When the latest order is closed and not yet dismissed (`seen_at` NULL), the dashboard shows a one-line notice with a
  **Dismiss** button that sets `seen_at`:
  - `done`: "Your order is done." (the ticket shows as usual);
  - `declined`: "Your order was declined." (the admin note is never shown to the buyer);
  - `withdrawn`: no notice.
  After a dismiss, or when a new order is placed, nothing about the old order is shown.
- Visitors see a confirmation in the dialog after submitting ("Order received. Check your email.") and nothing else.

## 9. Privacy and security

- Order data (names, emails, messages, IPs) is visible to admins only. `/api/me/orders` returns only the caller's own
  open order, or their latest closed order while it is undismissed, without `ip`, `admin_note` or mail state.
- The home page, `/api/pricing` and the public order endpoint reveal nothing about other orders, counts or capacity
  beyond the existing sold-out flags. Limit messages carry no numbers.
- Every order field shown in the dashboard or the home page goes through `esc()`.
- The public order endpoint is a POST with JSON and a Turnstile token. Signed-in order endpoints use the existing
  session and CSRF header.
- A typed email is never written to `users.email` (section 2).
- Header injection is prevented as in section 5.

## 10. API

- `POST /api/orders` (public): a visitor order with a Turnstile token.
- `POST /api/me/orders` (signed in): an order for the caller.
- `GET /api/me/orders`: the caller's open order, or latest undismissed closed order.
- `POST /api/me/orders/{id}/withdraw` and `POST /api/me/orders/{id}/dismiss`.
- `GET /api/admin/orders?status=`: the list.
- `POST /api/admin/orders/{id}` with `action`: `contacted`, `decline` (with note), `note`, `link` (with `user_id`, or
  `create: true`).
- `POST /api/admin/tickets` gains the optional `order_id` (section 7).
- `/api/pricing` gains `turnstile_site_key` (public by design) when Turnstile is configured.

All order endpoints return 404 while tickets are off.

## 11. Out of scope

Online payment; reminders or follow-up emails; editing an order; resending mail; the signup and credit alerts from
backlog issue #20 (they can reuse `mail.py`).

## 12. Testing

A fake SMTP server (including a slow one) and a stubbed Turnstile verifier. Tests cover:

- **Home page:** with Turnstile, **Get it** opens the order dialog and offers "Or sign in to order"; without Turnstile,
  **Get it** keeps today's link; sold-out cards cannot be ordered.
- **Dashboard (buyer):** Order buttons on the price list, pre-selected from the home page pick, disabled when sold out
  and while an order is open; withdraw while `new` or `contacted`; the closed-order notice and its dismiss.
- **Orders:** visitor and signed-in orders stored with the quoted figures; sold-out refused (409); one open order per
  user; a typed email stored on the order and never on `users.email`.
- **Limits:** per-IP, per-email and global, with messages that carry no numbers; Turnstile failure (400) and
  unreachable (503).
- **Mail:** admin and buyer contents; the buyer email carries no buyer-typed text; buyer cap gives `skipped`; CR/LF
  stripped; mail sent after the response; a slow or failing server leaves the order stored and records `failed`;
  no `[email]` records `off`.
- **Admin:** each action follows the status table and writes an audit row; invalid changes give 409; linking to an
  existing user, the suggested match never auto-linked, creating a user, and the 409 on a taken name; the `new` badge
  count.
- **Grant with `order_id`:** marks the order `done` atomically; refused with 409 when the order is withdrawn, closed or
  linked to a different user; a failed grant leaves the order unchanged.
- **Retention:** `ip` cleared when an order closes and on open orders older than 30 days.
- **Privacy:** non-admins never see other orders, IPs, admin notes or mail state; `esc()` on every field.
- **Config:** `[email]` validation and defaults, unknown keys refused, missing `TURNSTILE_SECRET` falls back.
