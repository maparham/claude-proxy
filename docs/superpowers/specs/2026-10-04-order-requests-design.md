# Order requests: design

Status: draft for review, 2026-10-04. Builds on the paid-tickets design (`2026-10-03-paid-tickets-design.md`).

## 1. Goal

Buying a ticket today means reading `how_to_buy` and contacting the admin by hand. Instead, a buyer files an **order request** from the price list. The admin gets an email, sees the order on a new Orders tab, and turns it into a ticket with the existing grant dialog. Payment stays manual and outside the app.

An order is a request, not a payment and not a reservation: it holds no capacity. Capacity is checked when the admin grants.

## 2. Who orders, and where

- **Signed-in users** see an **Order** button next to each price on their dashboard's price list, and on `/pricing` while signed in. The order is tied to their account; the form asks only for currency and an optional message. Their email comes from their account (`users.email`, set by Clerk sign-in); if they have none, the form asks for one.
- **Visitors** use the same button on `/pricing`. The form asks for name, email, tier, length, currency and an optional message, and carries a Cloudflare Turnstile check.
- Both forms show the price at today's rate (the same figures as the price list) and: "This is a request, not a payment. The admin will contact you with payment details."
- A tier and length shown as sold out cannot be ordered (the button is disabled, and the server refuses with 409).
- Without Turnstile configured (section 6), the visitor form is not offered: `/pricing` shows "Sign in to order" instead.

## 3. Data

New table `orders`:

| column | meaning |
|---|---|
| `id` | primary key |
| `created_at`, `updated_at` | epoch seconds |
| `user_id` | the ordering user, or the account a visitor's order was linked to; NULL for an unlinked visitor order; `ON DELETE SET NULL` |
| `name`, `email` | visitor's name and email; for a signed-in user, their user name and account email at the time of ordering |
| `tier`, `length`, `currency` | what was ordered |
| `quoted_usd`, `quoted_rate`, `quoted_amount` | the price shown on the form; for reference only, the grant re-prices as usual |
| `message` | buyer's message, at most 1000 characters |
| `status` | `new`, `contacted`, `done`, `declined`, `withdrawn` |
| `admin_note` | at most 200 characters, admins only |
| `ticket_id` | the ticket granted from this order; NULL otherwise |
| `ip` | the visitor's IP address, for the per-IP limit; NULL for signed-in orders |
| `admin_mail`, `buyer_mail` | `sent`, `failed` or `off` (no `[email]` config), per email |

Open means `new` or `contacted`. Orders are kept like tickets: never removed by retention cleanup, kept with their name and email when a user is deleted.

## 4. Limits

- A signed-in user may have one open order. A second one is refused with 409 and "You already have an open order."
- A visitor: at most 3 orders per day per IP address, and 3 per day per email address (case-insensitive). Over either: 429, "Too many orders today; try again tomorrow or sign in."
- All visitor orders together: at most 50 per day. Over it: 429, "Orders are busy today; please sign in to order."
- Message capped at 1000 characters, name at 100; email must look like an address (`something@something.something`).
- Turnstile: the server verifies the token with Cloudflare's `siteverify` endpoint before anything is stored. Failure: 400, "The verification failed; please try again." Cloudflare unreachable: 503 with `Retry-After: 30`.
- "Per day" means the 24 hours before the request.

## 5. Emails

Sent over SMTP with Python's `smtplib` (no new dependency), after the order is committed, in a background thread so a slow mail server never delays the response.

- **To the admin**, for every new order: subject `New order: <tier label> <length> from <name>`; body with buyer name, email, account (if any), tier, length, quoted amount, message, and a link to the Orders tab.
- **To the buyer**, a confirmation: subject `Your order: <tier label> <length>`; body with what they ordered, the quoted amount, the `how_to_buy` payment text, and "This is a request; the admin will contact you." It contains nothing the buyer typed except their name (no message), so the form cannot carry content to a stranger. It is sent once per order, never resent, and at most 3 times a day per address.
- A failed send records `failed` on the order and logs a warning; the order is kept. The Orders tab shows "admin email failed" or "buyer email failed".
- Without `[email]`, both are `off`; the order is stored and shown as usual.

The sender is a small module (`mail.py`) with one function, `send(to, subject, body)`, so the alerts in backlog issue #20 can reuse it later.

## 6. Configuration

```toml
[email]                          # optional; without it no mail is sent
smtp_host = "smtp.example.com"
smtp_port = 587                  # STARTTLS; 465 means implicit TLS
smtp_user = "gateway@example.com"
from = "Claude Gateway <gateway@example.com>"
admin_to = "admin@example.com"
# password: SMTP_PASSWORD in the environment

[tickets]
turnstile_site_key = "0x..."     # public; the secret is TURNSTILE_SECRET in the environment
```

Unknown keys are refused, like the other sections. `[email]` with a missing `smtp_host`, `from` or `admin_to` is a config error. `turnstile_site_key` without `TURNSTILE_SECRET` in the environment logs a warning and hides the visitor form.

## 7. Admin dashboard

A new **Orders** tab, shown while tickets are on, with a badge counting `new` orders.

- A table of orders, newest first, filterable by status (default: open). Each row: age, buyer (name, email, linked account), tier and length, quoted amount, message, status, email state, admin note.
- Actions:
  - **Contacted** (from `new`).
  - **Decline**, asking for a note.
  - **Grant ticket** (from `new` or `contacted`) opens the existing grant dialog filled in with user, tier, length and currency. When the grant succeeds, the order becomes `done` with `ticket_id` set. If the grant fails (capacity, stale rate, changed quote), the order is unchanged.
  - A visitor order has no account yet, so **Grant ticket** first asks the admin to pick an existing user or create one (named after the visitor's email). The link is stored in `user_id` before the grant dialog opens.
- Every status change, link and note writes an audit-log row.
- An order whose user is deleted keeps its name and email.

## 8. Buyer view

- A signed-in user with an open order sees it on their dashboard above the price list: "Order received: Standard, 1 week. The admin will contact you." with a **Withdraw** button while it is `new`. Withdrawing sets `withdrawn`. Order buttons are disabled while an order is open.
- After a `done` order, the dashboard shows the ticket as usual; a `declined` order shows "Your order was declined." with the admin's note withheld (the note is admin-only).
- Visitors see a confirmation on screen after submitting ("Order received. Check your email.") and nothing else.

## 9. Privacy and security

- Order data (names, emails, messages, IPs) is visible to admins only. `/api/me/orders` returns only the caller's own open or latest order, without `ip` or `admin_note`.
- `/pricing` and the public order endpoint reveal nothing about other orders, counts or capacity beyond the existing sold-out flags. Limit messages carry no numbers.
- Every order field shown in the dashboard goes through `esc()`.
- The public order endpoint is a POST with JSON and a Turnstile token; signed-in order endpoints use the existing session and CSRF header.
- Header injection: name and email are stripped of CR/LF and the email is validated before use in mail headers; the buyer email goes to the validated address only.

## 10. API

- `POST /api/orders` (public): visitor order with Turnstile token.
- `POST /api/me/orders` (signed in): order for the caller.
- `GET /api/me/orders`: the caller's open or latest order.
- `POST /api/me/orders/{id}/withdraw`.
- `GET /api/admin/orders?status=`: the list.
- `POST /api/admin/orders/{id}` with `action`: `contacted`, `decline` (with note), `link` (with `user_id` or `create: true`), `note`.
- Granting uses the existing `POST /api/admin/tickets` with an extra `order_id`; on success the order becomes `done` in the same transaction as the grant.

All return 404 while tickets are off.

## 11. Out of scope

Online payment; reminders or follow-up emails; editing an order; the signup and credit alerts from backlog issue #20 (they can reuse `mail.py`).

## 12. Testing

A fake SMTP server and a stubbed Turnstile verifier. Tests cover:

- visitor and signed-in orders stored with the quoted figures; sold-out refused
- one open order per user; withdraw; order buttons disabled while open
- per-IP, per-email and global limits, with messages that carry no numbers
- Turnstile failure (400) and unreachable (503); visitor form hidden without Turnstile
- admin and buyer email contents, including that the buyer email carries no buyer-typed text except the name; at most 3 buyer emails per address per day; CR/LF stripped
- a failed or slow SMTP send keeps the order and records `failed`; no `[email]` records `off`
- admin actions and their audit rows; linking a visitor order to an existing or new user
- grant with `order_id` marks the order `done` atomically; a failed grant leaves it unchanged
- privacy: non-admins never see other orders, IPs or admin notes; `esc()` on every field
- config: `[email]` validation, unknown keys refused
