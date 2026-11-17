# Free trial email (2026-11-17)

A user who claims a free trial, or is granted one by the admin, hears nothing by email today: the trial shows only on
the dashboard. An admin-granted user may never learn they have one, and a claim lapses unstarted after `valid_hours`.

## What gets built

One email per claim, sent when the claim is made (`POST /api/me/trial/claim`) or granted
(`POST /api/admin/users/{uid}/trial`). Nothing is sent when the trial starts.

- **To:** the account's email (`users.email`, else the name when it is an address). No address, or no `[email]` in the
  config: nothing is sent.
- **When:** after the response, through `mail.spawn`, as order mails go. A failure is logged and never touches the
  claim; nothing about the mail is stored.
- **Language:** the dashboard's (`lang` in the claim's body; anything but `fa` is English). An admin grant uses the
  language of the user's latest order, else English.
- **Says:** the trial's length and tier; that it starts with the first request in Claude Code or the dashboard's
  "Start now" (with the dashboard's URL); the date it must be started by; the discount on a first purchase after it
  (`follow_pct` for `follow_days`, left out at 0); the Windows setup block every pass mail ends with.
- Plain text plus an HTML copy, right to left for Persian, as buyer mails have.

## Approaches

1. **On claim / grant (picked).** Reaches the user before they have set anything up, carries the start-by date, and
   tells an admin-granted user the trial exists.
2. On start. The user is already using it by then; an admin-granted user still hears nothing until they do.
3. Both. Two emails within minutes of each other for a self-service user.

## Non-goals

No Telegram message, no reminder before a claim lapses, no mail at start or end, no record of the mail's state.

## Left to the builder

The texts' exact wording (Persian per the dashboard's trial strings: «آزمایش رایگان», «کلاد»), where the text function
lives (`trials.py`), and the tests (`tests/test_trials_web.py`, mail faked as `tests/test_orders_web.py` does).
