# Free Trial Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-service, admin-tunable free trial pass, also grantable by the admin, advertised on the home page.

**Architecture:** New module `trials.py` owns settings, claims and starting. A started trial is a normal `tickets`
row flagged `trial=1`, so limits, capacity and the pass card need almost no change. The follow-up discount is a saved
`referral_units` row, so the order code applies it unchanged.

**Tech Stack:** Python 3 / FastAPI / SQLite, vanilla JS dashboard (`app.js`, `i18n.js`), home page (`home.js`, `home.html`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-14-free-trial-design.md`

## Global Constraints

- Work on master; commit only this feature's paths.
- Every English string has a Persian twin in `i18n.js` (loanwords per house style; "کلاد" not "Claude Code").
- Tooltips use `tipI(...)`.
- Times are epoch seconds; all writes that check-then-insert run in `BEGIN IMMEDIATE`.

## Review Focus

- Two claim clicks at once → one claim (claim runs in BEGIN IMMEDIATE, checks inside).
- Deleted account re-signing with the same email → no second trial (email kept on the claim row).
- Trial start when capacity is full → refusal with the claim kept; a later start works.
- Auto-start never fires for a user who already has a running pass, or on count_tokens / third-party models.
- Admin grant to a user with a pass or past trial without `confirm` → 409, nothing written.

---

### Task 1: `trials.py` core + schema

**Files:** Create `src/claude_proxy/trials.py`; modify `src/claude_proxy/db.py` (table `trial_claims`, column
`tickets.trial INTEGER NOT NULL DEFAULT 0`); modify `tickets.upgrade_quote` (refuse a trial); test `tests/test_trials.py`.

**Produces:**
- `TrialError(status, message)`
- `settings(conn, cfg) -> Settings` (resolved: tier/share defaults filled in), `set_settings(conn, cfg, actor, changes) -> Settings`
- `open_claim(conn, user_id, now) -> dict | None`, `offer(conn, cfg, user, now) -> dict` (`{"can_claim", "reason", "claim", "duration_min"}`)
- `claim(conn, cfg, user, now) -> dict`, `admin_grant(conn, cfg, actor, user, now, confirm=False) -> dict`
- `start(conn, cfg, user_id, now) -> dict` (the ticket), `autostart(conn, cfg, user_id, now) -> str | None` (refusal message)
- `public_offer(conn, cfg, now) -> dict | None`, `admin_view(conn, cfg, now) -> dict`

Tests: defaults resolve to the Lite tier's share; claim once only (same user, same email after delete); cap counts
self claims in 24 h only; lapsed claim counts as used and can't start; start creates trial ticket of `duration_min`,
share copied, credit removed, follow unit issued; capacity full → TrialError, claim stays; admin grant needs confirm
for a past pass/trial; upgrade_quote refuses a trial.

### Task 2: request path + pass card

**Files:** `limits.evaluate` calls `trials.autostart` before the gating checks when the request is metered, not
count_tokens, not third-party; `passcard.build` adds `trial` flags to current/passes/past/queued; tests in `tests/test_trials.py`.

### Task 3: web routes

`GET/PUT /api/admin/trial`, `POST /api/admin/users/{uid}/trial`, `GET /api/me/trial`, `POST /api/me/trial/claim`
(Turnstile when on), `POST /api/me/trial/start`; `/api/pricing` gains `trial`. Tests in `tests/test_trials_web.py`.

### Task 4: dashboard UI

Admin: Pricing tab "Free trial" form (save via PUT), "Grant trial" in user actions. User: trial card above the price
list, "Free trial" name for trial passes, `?trial=1` focus. i18n en + fa.

### Task 5: home page

Hero button and pricing line from `/api/pricing.trial`; i18n en + fa. Run the full suite, commit.
