# Serving from every linked subscription (2026-11-24)

- **Status:** draft for approval.
- **Builds on:** `2026-10-13-multi-subscription-design.md` (step 1: storage + login per account). This is its deferred
  step 2 (routing, quota, dashboard), with decisions changed below.
- **Overrides in 10-13 §1:** the "global default account" is dropped (no default at all); "the grant form requires an
  explicit account pick" becomes an automatic pick for self-service sales (§4).

## 1. Why

The gateway can link several Claude subscriptions, but every request is still served from `default`. The admin
wants all linked subscriptions in use at the same time, so the gateway can sell more passes than one account holds.
A "default subscription" makes no sense under that model.

## 2. Goals, non-goals, anti-goals

**Goals**
- Every linked, healthy subscription serves traffic at the same time.
- A pass belongs to exactly one subscription. Its requests go only there.
- Users are global: one user, one key, any subscription.
- Pass types promise the same amount of usage whichever subscription they land on, across mixed plans
  (Pro, Max 5x, Max 20x).
- The admin picks a subscription in the dashboard and every per-subscription view reloads for it.

**Non-goals**
- Pooling or failover: a request never moves to another account because its own is limited, unhealthy or blocked
  (10-13 decision 1, kept).
- Prices per subscription. Prices stay per pass type and length, global.
- Measuring plan sizes from usage. The sizes are fixed numbers the admin can edit.

**Anti-goals** (this fails even if it "works")
- A pass draws more real usage on one account than on another of a different plan.
- An account is oversold because capacity was counted across accounts instead of per account.
- Existing passes change size or lose their history in the migration.

## 3. Routing: which subscription serves a request

1. **The serving pass decides.** `limits.serving` already picks the user's serving pass (oldest first, or the user's
   pick, per `2026-11-06-pass-switch-design.md`). The request goes to that pass's account. A user with running passes
   on two accounts may switch accounts when the serving pass changes; prompt cache on the other account is lost once,
   which is accepted.
2. **No running pass:** the request goes to the user's **assigned account** (`users.account`, new).
3. **No assigned account:** refused with a clear message ("No subscription is assigned to you; ask the admin.").
4. A request is never retried on another account. An unhealthy account's requests fail as one account's do today.

Applies to every key type (user keys, API-credit keys, pass app keys) and admins alike. Non-Anthropic routes
(`provider` other than `anthropic`) are untouched.

**Assignment** is set by the admin on the Users page (a column with a picker). *Builder's call, marked:* a new user
created by sign-up or `user add` is assigned the account with the most free room at that moment (as in §4), so
sign-up keeps working without admin action; the admin can change it.

## 4. Passes and plans

**Plan detection.** The gateway already reads each account's profile hourly (`Poller.check_plan`, `quota.py:482`)
and stores `rate_limit_tier`. This runs for every account, and also immediately after linking. A name-to-plan map
turns `rate_limit_tier` into Pro / Max 5x / Max 20x. The exact strings Anthropic sends must be confirmed against
the live `account_plans` rows when building. An unrecognized tier shows "plan unknown" on the Subscriptions page;
that account sells no passes until the admin picks its plan there (a manual override, stored per account, that wins
over detection).

**Plan factors.** Settings, editable on the Subscriptions page: Pro = 1, Max 5x = 5, Max 20x = 20.

**Pass size in Pro weeks.** A pass type (tier) is defined by its size in Pro weeks instead of a share of one account:
`size` (weekly) and `size_5h`. On an account with factor `f`, the share is `share_pct = size × 100 / f` (same for
`share_5h_pct`). Example: Standard = 0.5 Pro is 50% on Pro, 10% on Max 5x, 2.5% on Max 20x. Shares are copied onto
the ticket at grant as today, so a later factor or size change never touches a sold pass. The Pricing tab edits
sizes; the "equals N× Claude Pro" comparison (`compare`, `compare_ratio`) is the size itself and stops being a
separate setting. The free-trial `share_pct` / `share_5h_pct` overrides become sizes the same way.

**Capacity stays per account**: every existing check (points of the week, 5-hour room, `max_sold_pct`, borrows,
capacity scaling for usage outside the gateway) runs unchanged, but on the pass's own account, with that account's
quota. `tickets.ACCOUNT_ID` goes away.

**Placing a pass.**
- **Self-service sale** (payment, order, trial, referral reward): the gateway tries each account with a known plan,
  healthy credentials and fresh quota, keeps those where the pass fits (`capacity.check`), and picks the one with
  the most free room measured in Pro weeks (free points × f / 100), ties by name. Fits nowhere: sold out, as today.
- **Admin grant:** the same pick is preselected in the grant form; the admin may choose another account (capacity
  is checked on it).
- **Queued passes** (a pass after the one running) are placed independently; consecutive passes may sit on different
  accounts.
- **Bonuses** go on their pass's account.
- **Moving a pass** (admin action, from 10-13 decision 2): rechecks capacity on the target account with the share
  recomputed for its plan; the pass's size in Pro weeks is kept. This is how passes leave an account that is lost.

**Builder's calls after review (marked; the admin may override):**
- **Two factors per plan**, weekly and 5-hour (both default 5 and 20), since Anthropic's two windows need not scale
  alike. `share_5h_pct = size_5h × 100 / f_5h`.
- **Sizes live in the database** (Pricing tab). TOML tiers switch from `share_pct` to `size` / `size_5h`; the live
  config is rewritten in the deploy. A tier too big for a plan (share over `max_sold_pct` there) is allowed and is
  simply never placed on such accounts; config validation only rejects a tier that fits no plan at all.
- **Capacity knobs stay global** (`max_sold_pct`, `max_sold_5h_pct`, `sell_margin_pct`, `borrow_margin_pct`): they are
  percentages of an account and apply to each account alike.
- **Plan change detected on an account:** sold passes keep their stored shares (no automatic recompute or move).
  The account stops selling until the admin acknowledges the new plan on the Subscriptions page; a downgrade also
  raises a dashboard warning that the account may be oversold, and the admin moves passes off it if needed.
- **A moved pass keeps what it used:** its usage so far is converted to Pro weeks and carried over (converted back
  into the new account's points), so moving never refills or shrinks a pass. Borrows move with it the same way.
- **Units across accounts:** per-account views (quota, capacity, requests) show that account's points. Views that sum
  across accounts (the Users table's usage, a user's own charts spanning passes on different accounts) show Pro
  weeks, since points of different plans can't be added. A user's pass bars stay "% of this pass", as today.

## 5. Data

- `requests.account` (text slug; NULL for requests refused before routing). Usage attribution, observed rate,
  estimates and every per-account query filter on it.
- `quota_snapshots.account`, `account_plans.account`, `account_profile` keyed by account, `usage_estimates.account`,
  `pass_borrows` follows its ticket (no column needed).
- `tickets.account_id` becomes the account slug (or `credentials` gains a stable integer id; builder's choice, 10-13
  §2 notes both). `1` maps to `default`.
- `users.account` (assigned account, nullable).
- Settings: `plan_factors`; per-account plan override.
- **Migration:** every existing row belongs to `default`; every existing user is assigned `default`. Tier sizes are
  computed from today's shares and `default`'s recorded plan: `size = share_pct × f(default) / 100`, so every pass type
  keeps its current usage on `default`. Existing tickets keep their stored shares.
- The name `default` stays as an ordinary account name; it has no special meaning afterwards.

## 6. Serving machinery

One `OAuthBackend` and one `Poller` per linked account, created at start and when an account is linked or removed
(no restart needed). `/health` is healthy only when every account is. The CLI `status` reports each account.

**Removing an account** is refused while it has running or queued passes or assigned users; the admin moves them
first.

## 7. Dashboard

- **Account picker** at the top of the admin dashboard, listing linked accounts with plan and state. The choice is
  remembered in the browser. Changing it reloads every per-account view.
- **Per account:** quota bars and overview, capacity, passes list, requests, sessions, errors, models and usage
  charts. Admin APIs take `?account=<slug>`.
- **Global:** Users (with the assigned-account column), Pricing (sizes in Pro weeks, prices), orders and payments,
  referrals, inbox, Subscriptions (now with a Plan column: detected plan, or the manual pick).
- **Users' own page:** shows each pass's own account's figures without naming the account. Users never see account
  names or emails.

## 8. Approaches considered

- **Shard inside one gateway (picked).** Every account-related table and component becomes per account; users,
  payments and pricing stay shared. Matches "users are global, passes belong to one account".
- **One gateway per account.** Simple per instance, but splits users, keys, payments and pricing across instances,
  which contradicts global users.
- **Pooling (any request to any account).** Rejected in 10-13; a pass's limits are a share of one account's quota,
  so pooling breaks capacity accounting.

For pass sizing across plans: **Pro as the reference unit with fixed plan factors (picked)**, over a share table per
(tier, plan) (more to maintain, easy to make inconsistent) and factors measured from usage (noisy; would change a
pass's cost mid-run).

## 9. Left to the builder

- The exact `rate_limit_tier` strings and the map; table/column shapes; how the account picker is drawn; delivery
  order (data → serving → selling → dashboard is the natural split); tests.
