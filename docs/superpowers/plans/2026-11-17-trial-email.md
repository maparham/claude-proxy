# Free Trial Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Email the user when a free trial is claimed or granted to them.

**Architecture:** `trials.claim_mail_text` builds (subject, body); `trials.send_claim_mail` sends it with `mail.send`
off the loop; the two routes `mail.spawn` it after the response, as order mails go.

**Tech Stack:** Python, FastAPI, smtplib (through `mail.send`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-17-trial-email-design.md`

## Global Constraints

- One mail per claim; none at start. No `[email]` or no address: nothing is sent. A failure is logged, never raised.
- Language: the claim body's `lang` (`fa` or English); an admin grant uses the user's latest order's `lang`, else `en`.
- Persian: «آزمایش رایگان», «کلاد» for Claude Code, Persian digits (`tickets.fa_num`), Tehran dates (`tickets.terms_date`).
- The HTML copy for Persian comes from `mail.message` (right to left); nothing extra.

## Review Focus

- A user with no `users.email` whose name is not an address: no mail, no error.
- A failing SMTP server: the claim still returns 200.
- `follow_pct` 0: no discount line.
- A duration that isn't whole hours (90 min): "90 minutes", not "1.5 hours".
- An admin grant to a user whose last order was Persian: the mail is Persian.

---

### Task 1: Trial claim email

**Files:**
- Modify: `src/claude_proxy/trials.py` (new section "the claim's email")
- Modify: `src/claude_proxy/web.py` (`admin_user_trial`, `my_trial_claim`)
- Test: `tests/test_trials_web.py`

**Interfaces:**
- Produces: `trials.claim_mail_text(conn, cfg, claim: dict, lang: str) -> tuple[str, str]`,
  `async trials.send_claim_mail(conn, cfg, claim_id: int, lang: str | None) -> None` (`None`: the latest order's lang).

- [ ] **Step 1: Write the failing tests** — fixture `sent` fakes `mail.send` (as `tests/test_orders_web.py`), sets
  `users.email='alice@example.com'`; await `web._mail_tasks`. Tests:
  `test_claim_mails_the_user` (one mail to alice, English subject has "free trial", body has "1 hour", "Lite", the
  dashboard URL when set, "10%"); `test_claim_mail_in_persian` (`lang: "fa"` → body has «آزمایش رایگان» and «کلاد»);
  `test_admin_grant_mails_in_the_last_order_language`; `test_no_address_no_mail`; `test_mail_failure_keeps_the_claim`;
  `test_no_discount_line_at_zero_follow_pct`; `test_minutes_when_not_whole_hours` (90 → "90 minutes").
- [ ] **Step 2: Run** `pytest tests/test_trials_web.py -q` — new tests FAIL.
- [ ] **Step 3: Implement** the two functions; the routes call `mail.spawn(trials.send_claim_mail(...))` when
  `cfg.email` is set. Body ends with `orders.windows_setup(cfg, {"lang": lang})`.
- [ ] **Step 4: Run** `pytest tests/test_trials_web.py tests/test_trials.py -q` — PASS; then the full suite.
- [ ] **Step 5: Commit** the spec, plan, code and tests.
