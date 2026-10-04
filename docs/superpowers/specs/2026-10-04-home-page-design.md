# Home page with marketing-style pricing

## Goal

A signed-out visitor landing on the gateway should understand what it is, see what it costs, and know how to get
in. Today `/` redirects to `/dashboard` (a sign-in screen with no pitch) and `/pricing` is a tier × length table
that reads like a spreadsheet.

## Routes

| Request | Tickets on | Tickets off |
|---|---|---|
| `/` without the session cookie | the home page | 307 to `/dashboard` (as today) |
| `/` with the session cookie | 307 to `/dashboard` | 307 to `/dashboard` |
| `/?home` (any cookie) | the home page (admin preview) | 307 to `/dashboard` |
| `/pricing` | 308 to `/#pricing` | 404 (as today, via `need_tickets()`) |

Pricing links inside the app no longer go through `/pricing`:

- The sign-in screen's foot link (`index.html`, `.signin-foot`) goes to `/#pricing`. The visitor is signed out, so `/` serves the home page.
- For a user, the dashboard header's **Pricing** button opens the Overview tab and scrolls to the in-app price list
  (`priceListCard`, which gets `id="prices"`). The visitor is signed in, so `/` would redirect.
- Admins don't get `priceListCard` (`renderOverview` skips `/api/me/tickets` for admins). Their header **Pricing** button goes to `/?home#pricing`, so they can see the public page.
- Both links stay hidden while tickets are off (`pricing-link hidden`, as today).

## Positioning

The page is low-key and framed for a team, not marketed as a public reseller. `home.html` carries
`<meta name="robots" content="noindex">`.

## Page sections

1. **Hero:** team-framed, low-key copy that matches the sign-in screen ("Use Claude Code on your team's Claude
   subscription, by the day, week or month"). It is not pitched as a public reseller. Then
   a short lede and two buttons: *See pricing* (scrolls to `#pricing`) and *Sign in* (`/dashboard`).
2. **How it works:** the three steps from the sign-in screen, as static text. Step 2 says "Run one command
   in the terminal — you get it after signing in" and shows no command. `home.js` fetches nothing from `/install`.
3. **Pricing (`id="pricing"`):** the toggle and the cards, then the rate note ("Prices converted at the rate of …"
   when `rate_set_at` is set).
4. **FAQ:** static questions: what a share means, what happens at the limit, Sonnet vs Opus, what happens at expiry (no auto-renew: access stops, then buy again),
   and privacy (linked). One entry, **How do I pay?**, is filled from `/api/pricing`'s `how_to_buy`, falling back to
   "Ask the gateway admin." when it is empty.
5. **Footer:** Sign in, Privacy.

## Cards

All data comes from `GET /api/pricing`. The API does not change.

- **Toggle:** Day / Week / Month, defaulting to Week. The toggle carries no savings figure.
- **Highlighted tier:** the second tier in config order when there are at least two, otherwise none. There is no new config key.
- **Header:** the tier `label`, then "≈ {compare}" as the headline equivalence (Anthropic plan names only, never their prices) and "{share_pct}% of the subscription" as small print.
- **Price:** the selected length's `amount`, formatted with `Intl.NumberFormat`, with "/day", "/week" or "/month".
  While a discount is live (`discount_ends_at` in the future on the server's clock), `list_amount` is struck through,
  a "−N%" ribbon (N from `usd` vs `list_usd`) sits on the card and the existing countdown runs under the price.
- **Per-card saving (week and month only):** "Save N% vs daily", where N compares the charged `usd` with the day
  ticket's charged `usd` × 7 or × 30, so discounts count on both sides. It is hidden when N ≤ 0.
- **Usage hints:** the existing wording, "{Family}: at least {per_5h} h per 5-hour window, {per_day} h per day". Each
  missing number is left out of its line, and a family with both numbers null is omitted. These are the same rules as
  `priceListCard`.
- **Sold out is per length:** when the selected length is `sold_out`, the card dims and its button reads
  "Sold out" (disabled). Switching to a length that isn't sold out restores the card.
- **"Get it" button:** links to `/dashboard?tier=<id>&length=<day|week|month>`.

## The picked tier in the dashboard

- On boot, `app.js` reads `tier`/`length` from the query and stores them in `sessionStorage`. This survives sign-in. It
  then removes them from the URL with `history.replaceState`.
- When `priceListCard` renders, the pair is shown only if `tier` is one of `tk.prices.tiers[].tier` and `length` is one of
  `day|week|month`. Invalid values are dropped silently. When valid, the card's foot line (the `how_to_buy` `<p class="sub">`, today `app.js:628`) reads
  "You picked **{label}** for **a {length}**. {how_to_buy or 'Ask the gateway admin.'}". The stored pair is cleared
  once it has been shown.

## Implementation shape

- New: `static/home.html`, `static/home.js` and `static/home.css`. The page loads `app.css` for the theme tokens and `home.css`
  on top. `web.py` serves `versioned("home.html", ("home.js", "home.css", "app.css"))`.
- The money, countdown, skew and reload-once helpers move from `pricing.js` into `home.js`. `pricing.html` and `pricing.js` are deleted.
- `web.py`: `/` follows the route table above. `/pricing` calls `need_tickets()` first, then returns a 308 to `/#pricing`.
- `index.html`/`app.js`: the pricing links change as described in Routes, `priceListCard` gets `id="prices"`, and
  the picked-tier handling is added.
- Light and dark themes come from the existing tokens in `app.css`. The layout must work at phone width: the cards stack,
  and the toggle stays visible above them.

## Testing

Existing tests that change (`tests/test_tickets_web.py`):

- `test_pricing_falls_back_to_usd_without_a_rate` asserts `/` (no cookie) serves `home.js?v=` instead of `/pricing`/`pricing.js`.
- `test_pricing_is_404_when_tickets_are_off` still holds for `/pricing`, and `/` now redirects to `/dashboard` while tickets are off.
- `test_dashboard_links_the_pricing_page_only_while_tickets_are_on` checks the new hrefs (`/#pricing` in the sign-in foot,
  the header button's admin and user targets), drops the "and back" assertion on `/pricing`, and asserts the home page links `/dashboard`.
- `test_pricing_page_counts_down_on_the_servers_clock` with `_run_pricing` points at `home.js` and its DOM ids
  (`#pricing`, the toggle and the cards).

New tests:

- `/` without a cookie serves the home page. With a cookie it gives a 307 to `/dashboard`. `/?home` serves the page even with a cookie.
  With tickets off, every form of `/` gives a 307 to `/dashboard`.
- `/pricing` gives a 308 to `/#pricing`.
- A node harness for `home.js`: switching to a sold-out length dims the card and switching back restores it; the
  saving is hidden when N ≤ 0; a family with null hours is omitted; an empty `how_to_buy` falls back.
- The `app.js` picked-tier check: an unknown tier or length is dropped and a valid pair names its label.

Manual check in the browser: switching lengths, a discount countdown, a sold-out length, phone width, dark mode,
and the Pricing buttons for an admin and for a user.

## Out of scope

Online checkout, per-visitor currency selection, analytics, SEO metadata beyond title and description.
