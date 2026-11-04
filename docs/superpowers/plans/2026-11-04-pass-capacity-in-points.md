# Pass Capacity in Points Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sell passes against the points of Anthropic's week that nobody has used or is owed, instead of against the sum of the shares running at one instant.

**Architecture:** A new module `src/claude_proxy/capacity.py` holds the points arithmetic: pieces per week, sold points per week, points owed to running passes, the room, a hypothetical-pass `check` (previews, sold-out flags, grants, upgrades) and a before/after `Guard` for the two multi-row changes (bonuses, queue moves). `tickets.py` keeps its transactions and calls into `capacity`; its old per-instant rule survives only as the 5-hour check. `limits.py` hands its borrowing free room to the same `room` function.

**Tech Stack:** Python 3.12, SQLite, FastAPI, pytest (`uv run --python 3.12 pytest -q`), no-build vanilla JS front end with `i18n.js` (English and Persian).

**Spec:** `docs/superpowers/specs/2026-11-04-pass-capacity-in-points.md` (revision 3, amended by Task 2 of this plan). Read it before starting; this plan argues from it. Revision 2 of this plan follows a review that walked every test by hand.

## Global Constraints

- Points are percent of Anthropic's 7-day bucket. A piece is `share × days of the week covered ÷ 7`; bonuses add their own share over their own period.
- Room `= max_sold_pct − U − Σ owed − sell_margin_pct`, unfloored inside the code; `U` is the newest `7d` snapshot's `utilization_pct`.
- Fresh report: newest `7d` snapshot younger than `cfg.quota.stale_after_s` (1800 s) and its `resets_at` in the future. Without one nothing is sold: `"No fresh usage report from Anthropic; try again in a few minutes."`
- Owed counts non-cancelled passes with `effective_end > now` and `starts_at < week_end` only; ended passes owe nothing (recycling).
- Owed and check 1 scale pieces by `limits._capacity` (`100 − unattributed`); check 2 sums unscaled pieces over sold periods and never applies `exclude`.
- Check 1 judges what a change **adds** to owed: a change that adds nothing to this week (a queue move inside the week, a cancel) passes it. Task 2 amends the spec to say so.
- Check 2 gates weeks after the current one only, against `max_sold_pct`.
- Check 3: summed 5-hour shares (`COALESCE(share_5h_pct, share_pct)`) at every start point ≤ `max_sold_5h_pct`.
- New settings: `max_sold_5h_pct` default 100 (above 0, at most 1000), `sell_margin_pct` default 5 (0 to 100).
- Hand-set `share_7d` rows are never owed, for selling or borrowing. Borrowing keeps a ceiling of 100.
- Every user-visible string exists in English and Persian in `static/i18n.js`; `tests/test_i18n.py` enforces equal keys and placeholders.
- Test snapshots are inserted in chronological order; `report()` asserts it.
- Commit messages end with the attribution line the executing session is given; the commit blocks below show one as an example.

## Review Focus

1. **A snapshot from the old week after the reset** (fresh by age, `resets_at` passed): the home page must say "Temporarily unavailable", not "Sold out", and grants must name the missing report. Test in Task 5 (`test_old_week_snapshot_after_the_reset_sells_nothing`).
2. **Outside use between preview and grant:** the preview said available, then `U` jumps; the grant re-runs the preview inside its transaction and must refuse with the points named, writing nothing. Test in Task 5 (`test_usage_jump_between_preview_and_grant_refuses_cleanly`).
3. **A buyer replacing used-up passes:** the replaced passes leave owed and the 5-hour sum at once, so the new pass fits where the old ones were. Test in Task 5 (`test_replacing_used_up_passes_frees_their_owed_points`).
4. **The public price list on every page view:** it must build at most two capacity contexts, not one per tier and length. Test in Task 6 (`test_price_table_builds_one_context_per_start`).
5. **The dashboard 5-hour share override above the ceiling:** refused with the ceiling named, so a tier can never become unsellable silently. Test in Task 1 (`test_set_share_5h_refuses_a_share_over_the_5h_ceiling`).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/claude_proxy/capacity.py` (new) | Points arithmetic, `check`, `Guard`, `context`, `summary`; holds the helpers moved from `limits` |
| `src/claude_proxy/tickets.py` | Transactions call `capacity`; `sold_at`, `check_capacity`, `verify_capacity` become the 5-hour check; `price_table` gains `unavailable` |
| `src/claude_proxy/limits.py` | Imports the moved helpers back; `_free_room` delegates to `capacity.room` |
| `src/claude_proxy/config.py` | Two new settings and their validation |
| `src/claude_proxy/orders.py` | "Temporarily unavailable" refusal |
| `src/claude_proxy/static/app.js`, `home.js`, `app.css`, `i18n.js` | Panel, badges, strings |
| `tests/tickets_helpers.py` | `live()`, `report()`, `use()`: a fresh weekly report at a given time |
| `tests/test_capacity_points.py` (new) | The spec's tests |
| `config.example.toml`, `README.md`, spec | Docs |

---

### Task 1: Settings for the 5-hour ceiling and the sell margin

**Files:**
- Modify: `src/claude_proxy/config.py:153-165` (TicketsConfig), `:369-381` (validation)
- Modify: `src/claude_proxy/tickets.py:500-516` (`set_share_5h`)
- Modify: `config.example.toml:74-79`, `README.md:336-341`
- Test: `tests/test_capacity_points.py` (create)

**Interfaces:**
- Produces: `cfg.tickets.max_sold_5h_pct: float = 100`, `cfg.tickets.sell_margin_pct: float = 5`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_capacity_points.py`:

```python
"""Passes are sold in points of the week (spec 2026-11-04-pass-capacity-in-points)."""
import pytest

from claude_proxy import tickets
from claude_proxy.config import Config, ConfigError, TicketsConfig
from tests.tickets_helpers import seeded


def test_new_settings_have_their_defaults():
    t = TicketsConfig()
    assert (t.max_sold_5h_pct, t.sell_margin_pct) == (100, 5)


@pytest.mark.parametrize("key, value, ok", [
    ("max_sold_5h_pct", "150", True), ("max_sold_5h_pct", "0", False), ("max_sold_5h_pct", "1001", False),
    ("max_sold_5h_pct", "true", False), ("sell_margin_pct", "0", True), ("sell_margin_pct", "100", True),
    ("sell_margin_pct", "-1", False), ("sell_margin_pct", "101", False)])
def test_new_settings_are_validated(tmp_path, key, value, ok):
    p = tmp_path / "c.toml"
    p.write_text(f"[tickets]\n{key} = {value}\n")
    if ok:
        assert getattr(Config.load(str(p)).tickets, key) == float(value)
    else:
        with pytest.raises(ConfigError, match=key):
            Config.load(str(p))


def test_a_tier_5h_share_over_the_5h_ceiling_is_refused(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[tickets]\nmax_sold_5h_pct = 30\n[tickets.tiers.big]\nlabel = \"Big\"\nshare_pct = 20\nshare_5h_pct = 40\n")
    with pytest.raises(ConfigError, match="max_sold_5h_pct"):
        Config.load(str(p))
    p.write_text("[tickets]\nmax_sold_5h_pct = 30\n[tickets.tiers.big]\nlabel = \"Big\"\nshare_pct = 40\n")
    with pytest.raises(ConfigError, match="max_sold_5h_pct"):   # no 5-hour share: the weekly one counts
        Config.load(str(p))


def test_set_share_5h_refuses_a_share_over_the_5h_ceiling(db):
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_5h_pct = 50
    assert tickets.set_share_5h(conn, cfg, "lite", 50, ids["admin"]) == 50
    with pytest.raises(tickets.TicketError, match="max_sold_5h_pct"):
        tickets.set_share_5h(conn, cfg, "lite", 51, ids["admin"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --python 3.12 pytest -q tests/test_capacity_points.py`
Expected: FAIL (`AttributeError: 'TicketsConfig' object has no attribute 'max_sold_5h_pct'`).

- [ ] **Step 3: Implement**

In `config.py` `TicketsConfig`, after `borrow_margin_pct`:

```python
    max_sold_5h_pct: float = 100  # ceiling on the summed 5-hour shares of passes running at once (2026-11-04); >100 oversubscribes
    sell_margin_pct: float = 5    # points of the week never sold: requests in flight, rate error, whole-percent reports (2026-11-04)
```

In the validation block, after the `borrow_margin_pct` check:

```python
    if isinstance(t.max_sold_5h_pct, bool) or not isinstance(t.max_sold_5h_pct, (int, float)) or not 0 < t.max_sold_5h_pct <= 1000:
        raise ConfigError(f"{toml_path}: [tickets].max_sold_5h_pct must be above 0 and at most 1000")
    if isinstance(t.sell_margin_pct, bool) or not isinstance(t.sell_margin_pct, (int, float)) or not 0 <= t.sell_margin_pct <= 100:
        raise ConfigError(f"{toml_path}: [tickets].sell_margin_pct must be from 0 to 100")
```

Inside the tier loop, after the `max_sold_pct` check:

```python
        s5 = tier.share_5h_pct if tier.share_5h_pct is not None else tier.share_pct
        if s5 > t.max_sold_5h_pct:
            raise ConfigError(f"{toml_path}: [tickets.tiers.{tid}] 5-hour share {s5:g} is above "
                              f"[tickets].max_sold_5h_pct {t.max_sold_5h_pct:g}, so it could never be sold")
```

In `tickets.set_share_5h`, replace the docstring and add the check after the range check:

```python
    """Change a tier's 5-hour share for future tickets. Refused above max_sold_5h_pct (2026-11-04 spec, §7): passes
    running at once may hold at most that much 5-hour share, so a larger one could never be sold."""
    ...
    if share_pct > cfg.tickets.max_sold_5h_pct + 1e-9:
        raise TicketError(f"The 5-hour share must be at most {cfg.tickets.max_sold_5h_pct:g}% (max_sold_5h_pct), "
                          "or the tier could never be sold.")
```

In `config.example.toml`, replace lines 74-75 and 78:

```toml
# max_sold_pct = 80            # points of a week passes may have; the rest is headroom for everyone else
# max_sold_5h_pct = 100        # summed 5-hour shares of passes running at once; above 100 oversubscribes the 5-hour bucket
# sell_margin_pct = 5          # points of the week never sold: requests in flight, rate error, whole-percent reports
# borrow_margin_pct = 5        # a pass across the weekly reset borrows the old week's free room, less this; 100: never
```
```toml
# share_pct = 5                # weekly share: a pass takes share × days ÷ 7 points of each week it covers
```

In `README.md` line 339, replace "and whether the period fits under `max_sold_pct`" with "and whether the pass fits: its points of this week must fit what Anthropic reports as unused and no running pass is owed, later weeks stay under `max_sold_pct`, and the 5-hour shares of passes running at once under `max_sold_5h_pct`".

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --python 3.12 pytest -q tests/test_capacity_points.py tests/test_orders_config.py tests/test_pass_borrow.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/config.py src/claude_proxy/tickets.py config.example.toml README.md tests/test_capacity_points.py
git commit -m "Tickets config gains max_sold_5h_pct and sell_margin_pct; the 5-hour share override is checked against the ceiling

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: The capacity module: pieces, sold points, owed and room

**Files:**
- Create: `src/claude_proxy/capacity.py`
- Modify: `src/claude_proxy/limits.py:185-191, 216-227, 265-329` (move helpers out, delegate `_free_room`)
- Modify: `docs/superpowers/specs/2026-11-04-pass-capacity-in-points.md`
- Test: `tests/test_capacity_points.py`, `tests/test_pass_borrow.py`

**Interfaces:**
- Consumes: `tickets.week_bounds`, `tickets.TICKET_COLS`, `quota.attribution_cached`, `quota.observed_rate`, `quota.weighted_sql`, `db.TICKET_EFFECTIVE_END`.
- Produces:
  - `capacity.WEEK = 7 * 86400`
  - `capacity.pieces(week: tuple[float, float], share: float, starts_at: float, ends_at: float) -> dict[float, float]`
  - `capacity.fresh(att: dict, now: float) -> bool`
  - `capacity.sold_by_week(conn, account_id: int, week: tuple[float, float]) -> dict[float, float]`
  - `capacity.owed(conn, cfg, account_id: int, week, att: dict, rate: float | None, scale: float, now: float, exclude=()) -> float`
  - `capacity.room(conn, cfg, account_id, week, att, rate, scale, now, ceiling: float, margin: float, exclude=()) -> float` (unfloored)
  - Moved unchanged, re-exported by `limits`: `_capacity`, `_weighted_since`, `_pass_cap`, `_debt`, `_week_use`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capacity_points.py`:

```python
from claude_proxy import capacity
from tests.tickets_helpers import DAY, NOW

WEEK = 7 * DAY
W = (NOW - 4 * DAY, NOW + 3 * DAY)   # the week the tests' 7-day snapshots describe


def test_pieces_split_a_period_by_week_and_project_forward():
    assert capacity.pieces(W, 20, NOW, NOW + DAY) == {W[0]: pytest.approx(20 / 7)}
    p = capacity.pieces(W, 40, NOW + 2 * DAY, NOW + 9 * DAY)          # a week pass from Thursday
    assert p == {W[0]: pytest.approx(40 / 7), W[1]: pytest.approx(240 / 7)}
    assert capacity.pieces(W, 5, NOW + 3 * DAY, NOW + 4 * DAY) == {W[1]: pytest.approx(5 / 7)}
    assert capacity.pieces(W, 5, NOW - 10 * DAY, NOW - 9 * DAY) == {W[0] - WEEK: pytest.approx(5 / 7)}
    assert capacity.pieces(W, 5, NOW, NOW) == {}


def test_fresh_needs_a_young_snapshot_with_a_future_reset():
    att = {"utilization_pct": 10.0, "stale": False, "resets_at": NOW + 10}
    assert capacity.fresh(att, NOW)
    assert not capacity.fresh(att | {"stale": True}, NOW)
    assert not capacity.fresh(att | {"resets_at": NOW - 1}, NOW)
    assert not capacity.fresh(att | {"utilization_pct": None}, NOW)
```

In `tests/test_pass_borrow.py`, seven tests use a hand-set `share_7d` row as a knob to shrink the free room (lines 63, 76, 85, 96, 105, 117, 333: `set_limit(conn, <user>, "share_7d", <v>, "pct")`). Hand-set shares no longer hold anything back, so replace each of those seven lines with the borrow margin as the knob, which gives the same free room: `cfg.tickets.borrow_margin_pct = <v> + 5` (5 is the default margin the comment next to each line already subtracts). Keep every expected number. Where the line created a user only for the row (`bob = create_user(...)` at 62, 75), delete that line too if nothing else uses `bob`. Then add:

```python
def test_a_hand_set_weekly_share_is_headroom_and_holds_nothing_back(env):
    conn, cfg, ids, t = env
    set_limit(conn, ids["admin"], "share_7d", 93.6, "pct")
    use(conn, ids["alice"], t, 300, 1.3)
    assert check(conn, cfg, ids["alice"]) is None                  # borrows as on an empty account
    assert state(conn, cfg, ids["alice"]).limit == pytest.approx(OWN + NEXT)
```

(`state` is the file's helper at line 44 that returns the `share_7d` state; if it has another name there, use that one.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --python 3.12 pytest -q tests/test_capacity_points.py tests/test_pass_borrow.py`
Expected: FAIL (`ImportError: cannot import name 'capacity'`, and the new borrow test).

- [ ] **Step 3: Create `src/claude_proxy/capacity.py`**

Move `_capacity`, `_weighted_since`, `_pass_cap`, `_debt`, `_week_use` out of `limits.py` verbatim (bodies and docstrings unchanged) into this file under the header below, then add the new functions:

```python
"""Pass capacity in points of the Anthropic week (2026-11-04 spec).

A pass takes share × days ÷ 7 points of each week it covers (its piece). The gateway sells only what Anthropic reports
as unused this week and no running or queued pass is still owed, less a margin (the room); later weeks, which have no
usage yet, are capped by their promised pieces. The 5-hour bucket keeps a per-moment ceiling (tickets.check_capacity).
The helpers used by per-pass enforcement (_capacity, _pass_cap, _debt, _week_use, _weighted_since) live here so that
limits and the selling rule measure a pass the same way; limits imports them back.
"""
from __future__ import annotations

import math
import sqlite3

from . import db, quota, tickets
from .config import Config
from .forwarder import COUNT_TOKENS_PATH

DAY = 86400
WEEK = 7 * DAY


# ---- moved from limits.py, unchanged: _capacity, _weighted_since, _pass_cap, _debt, _week_use ----


def fresh(att: dict, now: float) -> bool:
    """Anthropic's weekly report is usable for selling: a snapshot younger than stale_after_s (att["stale"] is False)
    whose window has not reset yet (spec §1, Fresh)."""
    return att["utilization_pct"] is not None and not att["stale"] and bool(att["resets_at"]) and att["resets_at"] > now


def pieces(week: tuple[float, float], share: float, starts_at: float, ends_at: float) -> dict[float, float]:
    """The unscaled points `share` over [starts_at, ends_at) takes from each Anthropic week, keyed by week start.
    Weeks project from `week` in 7-day steps, so every caller keys the same week with the same number."""
    ws = week[0]
    start = ws + math.floor((starts_at - ws) / WEEK) * WEEK
    out: dict[float, float] = {}
    while start < ends_at:
        overlap = max(0.0, min(start + WEEK, ends_at) - max(start, starts_at))
        if overlap > 0:
            out[start] = out.get(start, 0.0) + share * overlap / DAY / 7
        start += WEEK
    return out


SOLD_END = "t.ends_at + 86400 * COALESCE((SELECT SUM(b.extra_days) FROM ticket_bonuses b WHERE b.ticket_id=t.id AND b.cancelled_at IS NULL), 0)"


def _sold_rows(conn: sqlite3.Connection, account_id: int, since: float) -> list[tuple[float, float, float]]:
    """(share, starts_at, sold end) of every non-cancelled pass and share bonus of the account still selling time after
    `since`. A pass's sold end is ends_at plus its bonus days: used_up_at never shrinks it (spec §1, Sold period)."""
    rows = [(r[0], r[1], r[2]) for r in conn.execute(
        f"SELECT t.share_pct, t.starts_at, {SOLD_END} FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
        f"AND {SOLD_END} > ?", (account_id, since))]
    rows += [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT b.share_pct, b.starts_at, b.ends_at FROM ticket_bonuses b JOIN tickets t ON t.id=b.ticket_id "
        "WHERE t.account_id=? AND t.cancelled_at IS NULL AND b.cancelled_at IS NULL AND b.share_pct>0 AND b.ends_at>?",
        (account_id, since))]
    return rows


def sold_by_week(conn: sqlite3.Connection, account_id: int, week: tuple[float, float]) -> dict[float, float]:
    """Σ unscaled pieces over sold periods per week, from the current week on (spec §2 check 2, §6). No exclude: what
    a replaced pass was sold for in the weeks ahead stays sold there."""
    out: dict[float, float] = {}
    for share, s, e in _sold_rows(conn, account_id, week[0]):
        for w, p in pieces(week, share, max(s, week[0]), e).items():
            out[w] = out.get(w, 0.0) + p
    return out


def _ids(exclude) -> tuple[int, ...]:
    return () if exclude is None else (exclude,) if isinstance(exclude, int) else tuple(exclude)


def owed(conn: sqlite3.Connection, cfg: Config, account_id: int, week: tuple[float, float], att: dict,
         rate: float | None, scale: float, now: float, exclude=()) -> float:
    """What running and queued passes may still draw from the current week (spec §1, Owed): per pass, its piece over
    the week (every overlapping share bonus counted) scaled to the gateway's capacity, less its debt and its use this
    week. Use is weighted tokens at the observed rate, or the pass's attributed points without a rate. Ended passes,
    used up or not, owe nothing."""
    week_start, week_end = week
    ex = _ids(exclude)
    others = [dict(r) for r in conn.execute(
        f"SELECT {tickets.TICKET_COLS} FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
        f"AND t.starts_at<? AND {db.TICKET_EFFECTIVE_END}>?", (account_id, week_end, now)) if r["id"] not in ex]
    if not others:
        return 0.0
    ids = [p["id"] for p in others]
    uses = _week_use(conn, cfg, ids, week_start, now) if rate else {}
    bonuses: dict[int, list] = {}
    for b in conn.execute(f"SELECT ticket_id, share_pct, starts_at, ends_at FROM ticket_bonuses WHERE ticket_id IN "
                          f"({','.join('?' * len(ids))}) AND cancelled_at IS NULL AND share_pct>0", ids):
        bonuses.setdefault(b["ticket_id"], []).append(b)
    total = 0.0
    for p in others:
        span = lambda s, e: max(0.0, min(week_end, e) - max(week_start, s)) / DAY / 7   # noqa: E731
        part = p["share_pct"] * span(p["starts_at"], p["effective_end"])
        part += sum(b["share_pct"] * span(b["starts_at"], b["ends_at"]) for b in bonuses.get(p["id"], []))
        own = part * scale / 100 - _debt(conn, cfg, p, week_start, scale, now)
        use = uses.get(p["id"], 0.0) / rate if rate else att["ticket_shares"].get(p["id"], 0.0)
        total += max(0.0, own - use)
    return total


def room(conn: sqlite3.Connection, cfg: Config, account_id: int, week, att: dict, rate: float | None, scale: float,
         now: float, ceiling: float, margin: float, exclude=()) -> float:
    """ceiling − U − Σ owed − margin (spec §1, Free room), not floored: negative means the week is over-committed."""
    return ceiling - att["utilization_pct"] - owed(conn, cfg, account_id, week, att, rate, scale, now, exclude) - margin
```

In `limits.py`: delete the five moved functions, add `from .capacity import _capacity, _debt, _pass_cap, _week_use, _weighted_since, fresh, room` after the existing `from . import db, quota, tickets` line, and replace the body of `_free_room` (keep its signature, so `_ticket_7d` is untouched):

```python
def _free_room(conn, cfg, t, week, att, rate, capacity, now) -> float | None:
    """The points of the current week no one is owed, for borrowing (2026-10-30 spec §1, through the 2026-11-04 spec §5):
    100 less the account's use, what the other passes may still draw, and the borrow margin. Hand-set weekly shares are
    headroom and hold nothing back. None without live data to say so."""
    if cfg.tickets.borrow_margin_pct >= 100 or not fresh(att, now):
        return None
    return max(0.0, room(conn, cfg, t["account_id"], week, att, rate, capacity, now, ceiling=100.0,
                         margin=cfg.tickets.borrow_margin_pct, exclude=t["id"]))
```

`COUNT_TOKENS_PATH` stays imported in `limits.py` (`evaluate` uses it); `capacity.py` needs it for the moved `_weighted_since` and `_week_use`.

Amend the spec (these are the plan's deviations and corrections, recorded where the next reader looks):

- §2 check 1: replace "After the change, `Σ owed ≤ max_sold_pct − U − M`, with the new rows counted in owed at their full scaled piece (they have no use yet)." with "After the change, what it **added** to owed must fit: `Σ owed_after − Σ owed_before ≤ max_sold_pct − U − Σ owed_before − M`, the new rows counted at their full scaled piece (they have no use yet). A change that adds nothing to this week's owed (a cancel, a queue move inside the week) passes this check."
- §5: replace "they differ only in the margin they subtract (`sell_margin_pct` for sales, `borrow_margin_pct` for borrowing) and in `exclude`" with "they differ in the margin they subtract (`sell_margin_pct` for sales, `borrow_margin_pct` for borrowing), in `exclude`, and in the ceiling: sales use `max_sold_pct`, borrowing keeps 100 as 10-30 has it, since borrowing only moves a pass's own allowance earlier and never sells new points".
- §1 Sold period, append: "A share bonus's period is its stored `ends_at`, which `end_used_up` shortens; its pieces in later weeks therefore leave the sum when its pass is used up."
- §7 `max_sold_5h_pct`: replace "Above 0; it may exceed 100" with "Above 0 and at most 1000; it may exceed 100".
- §9 test 2: replace "the reason naming 0 free points and 5.71 needed" with "the reason naming 2.0 free points and 5.7 needed".
- §9 test 3: replace with "`max_sold_pct = 60`, `max_sold_5h_pct = 200`, two 40% week passes sold now with 3 days of the week left (each owes 17.1 here and promises 22.9 next week): a third fits this week (51.4 owed under 60 − 1 − 5) but refuses by check 2, next week holding 68.6 over 60."
- §9 test 14: replace "a failed move on cancel keeps the dates and cancels anyway" with "a cancel whose queue move would pull a pass from next week into this one, with no room here, keeps the dates and cancels anyway".

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --python 3.12 pytest -q tests/test_capacity_points.py tests/test_pass_borrow.py tests/test_tickets_limits.py tests/test_gateway_capacity.py tests/test_pass_accounting.py`
Expected: PASS. A borrow failure other than in the seven rewritten lines means a helper was altered while moving it.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/capacity.py src/claude_proxy/limits.py tests/test_capacity_points.py tests/test_pass_borrow.py docs/superpowers/specs/2026-11-04-pass-capacity-in-points.md
git commit -m "Capacity module: weekly pieces, sold points, owed and room; borrowing's free room no longer holds back hand-set shares

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: The per-instant rule becomes the 5-hour check

**Files:**
- Modify: `src/claude_proxy/tickets.py:574-619` (`sold_at`, `check_capacity`, `verify_capacity`)
- Test: `tests/test_capacity_points.py`

**Interfaces:**
- Produces: `tickets.sold_at(conn, account_id, t, exclude=None) -> float` now sums 5-hour shares; `tickets.check_capacity(conn, cfg, account_id, share_5h, starts_at, ends_at, exclude=None)` and `tickets.verify_capacity(conn, cfg, account_id, starts_at, ends_at)` check against `max_sold_5h_pct` and raise `CapacityError("5-hour shares would reach …% at …, over max_sold_5h_pct (…%).")`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_capacity_points.py`:

```python
def test_the_5h_sum_reads_5h_shares_and_its_own_ceiling(db):
    conn, cfg, ids = seeded(db)
    tickets.set_share_5h(conn, cfg, "lite", 30, ids["admin"])
    from tests.test_tickets_capacity import grant
    t = grant(conn, cfg, ids, tier="lite", length="day")          # weekly 5, 5-hour 30
    assert tickets.sold_at(conn, 1, NOW) == 30
    conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, share_5h_pct, extra_days, starts_at, ends_at, granted_at) "
                 "VALUES(?,?,?,?,?,?,?)", (t["id"], 10, None, 0, NOW, NOW + 3600, NOW))
    assert tickets.sold_at(conn, 1, NOW) == 40                     # a bonus without a 5-hour share counts its share
    cfg.tickets.max_sold_5h_pct = 60
    tickets.check_capacity(conn, cfg, 1, 20, NOW, NOW + DAY)       # 40 + 20 = 60: fits
    with pytest.raises(tickets.CapacityError, match="5-hour shares would reach 61% .* over max_sold_5h_pct"):
        tickets.check_capacity(conn, cfg, 1, 21, NOW, NOW + DAY)
```

This test grants without a fresh report; it passes in this task and gains `live(conn)` before the grant in Task 5.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --python 3.12 pytest -q tests/test_capacity_points.py::test_the_5h_sum_reads_5h_shares_and_its_own_ceiling`
Expected: FAIL (`assert 5 == 30`).

- [ ] **Step 3: Implement**

In `tickets.sold_at`, change both sums and the docstring:

```python
    """sold(t) for the 5-hour bucket (2026-11-04 spec, §2 check 3): 5-hour shares of the non-cancelled tickets and
    bonus shares covering `t` (half-open periods), each falling back to its weekly share. `exclude` leaves those
    tickets and their bonuses out."""
    ...
    a = conn.execute(f"SELECT COALESCE(SUM(COALESCE(t.share_5h_pct, t.share_pct)), 0) FROM tickets t WHERE ...
    b = conn.execute(f"SELECT COALESCE(SUM(COALESCE(b.share_5h_pct, b.share_pct)), 0) FROM ticket_bonuses b JOIN ...
```

(Keep the rest of both queries exactly as they are.)

Replace `check_capacity` and `verify_capacity`:

```python
def check_capacity(conn: sqlite3.Connection, cfg: Config, account_id: int, share_5h: float, starts_at: int, ends_at: int,
                   exclude=None) -> None:
    """The 5-hour check (2026-11-04 spec, §2 check 3): sold(t) + share_5h <= max_sold_5h_pct at the period's start and
    at every start inside it, else CapacityError."""
    ceiling = cfg.tickets.max_sold_5h_pct
    for point in sorted({starts_at, *_starts_within(conn, account_id, starts_at, ends_at, exclude)}):
        sold = sold_at(conn, account_id, point, exclude)
        if sold + share_5h > ceiling + 1e-9:
            raise CapacityError(f"5-hour shares would reach {sold + share_5h:g}% at {_date(point)}, over max_sold_5h_pct "
                                f"({ceiling:g}%).")


def verify_capacity(conn: sqlite3.Connection, cfg: Config, account_id: int, starts_at: int, ends_at: int) -> None:
    """The 5-hour check over rows already written, for changes of several pieces; call it inside the transaction or
    savepoint, so CapacityError undoes them."""
    ceiling = cfg.tickets.max_sold_5h_pct
    for point in sorted({starts_at, *_starts_within(conn, account_id, starts_at, ends_at)}):
        sold = sold_at(conn, account_id, point)
        if sold > ceiling + 1e-9:
            raise CapacityError(f"5-hour shares would reach {sold:g}% at {_date(point)}, over max_sold_5h_pct ({ceiling:g}%).")
```

Leave callers alone in this task; Task 5 rewires them.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --python 3.12 pytest -q tests/test_capacity_points.py`
Expected: PASS. (`tests/test_tickets_capacity.py` now fails in places; Task 6 migrates it.)

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/tickets.py tests/test_capacity_points.py
git commit -m "The per-moment capacity sum counts 5-hour shares against max_sold_5h_pct

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Test helpers for a fresh weekly report

**Files:**
- Modify: `tests/tickets_helpers.py`
- Test: `tests/test_capacity_points.py`

**Interfaces:**
- Produces: `tickets_helpers.RESET7 = NOW + 3 * DAY`, `WEEK_START = NOW - 4 * DAY`, `PTS = 1000`, `live(conn, t=NOW, util7=0.0)`, `use(conn, uid, ticket_id, points, t)`, `report(conn, ids, util7, t=NOW - 60)`.

- [ ] **Step 1: Add the helpers**

Append to `tests/tickets_helpers.py`:

```python
RESET7 = NOW + 3 * DAY       # the weekly reset the tests' snapshots report; the week is [NOW - 4d, NOW + 3d)
WEEK_START = RESET7 - 7 * DAY
PTS = 1000                   # weighted tokens per point: input tokens of claude-sonnet-5, the reference model (weight 1)


def live(conn, t=NOW, util7=0.0):
    """A 7-day snapshot 60 s before `t` reporting `util7`, its reset the first weekly reset after `t`, so a pass can be
    sold at `t` (2026-11-04 spec, §3). Insert snapshots in time order: attribution walks them sorted."""
    resets = RESET7
    while resets <= t:
        resets += 7 * DAY
    while resets - 7 * DAY > t:
        resets -= 7 * DAY
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (t - 60, "header", "7d", util7, resets))


def use(conn, uid, ticket_id, points, t):
    """`points` of use served on the pass (None: no pass), at PTS weighted tokens per point, ending at `t`."""
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, input_tokens, output_tokens, "
                 "ticket_id) VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, t - 1, t, "POST", "/v1/messages", "anthropic",
                                                            "claude-sonnet-5", int(round(points * PTS)), 0, ticket_id))


def report(conn, ids, util7, t=NOW - 60):
    """A fresh 7-day snapshot at `t` reporting `util7`, with the whole rise since the previous snapshot attributed: the
    requests recorded since then are topped up with an admin request so that the observed rate stays PTS and nothing is
    unattributed (capacity 100). Snapshots must come in time order; a `t` before the newest one is a test bug."""
    last = conn.execute("SELECT observed_at, utilization_pct FROM quota_snapshots WHERE bucket='7d' ORDER BY observed_at DESC "
                        "LIMIT 1").fetchone()
    assert last is None or t > last[0], f"report() at {t} is not after the newest snapshot at {last[0]}"
    if last is None:
        live(conn, t - 3600, 0.0)
        since, prev = t - 3660, 0.0
    else:
        since, prev = last[0], last[1]
    tokens = conn.execute("SELECT COALESCE(SUM(input_tokens), 0) FROM requests WHERE provider='anthropic' AND ended_at>? "
                          "AND ended_at<=?", (since, t)).fetchone()[0]
    top = (util7 - prev) * PTS - tokens
    assert top >= -1e-6, f"report({util7}) is below what the requests since the last snapshot already used ({tokens / PTS} points)"
    if top > 0:
        use(conn, ids["admin"], None, top / PTS, t - 1)
    live(conn, t + 60, util7)
```

- [ ] **Step 2: Write a test that pins the helpers**

Append to `tests/test_capacity_points.py`:

```python
from claude_proxy import quota
from tests.tickets_helpers import PTS, WEEK_START, live, report, use


def test_report_keeps_the_rate_and_attributes_everything(db):
    conn, cfg, ids = seeded(db)
    report(conn, ids, 1, t=WEEK_START + 60)
    report(conn, ids, 42, t=NOW - 60)
    att = quota.attribution(conn, cfg.pricing, "7d", now=NOW, stale_after_s=cfg.quota.stale_after_s)
    assert (att["utilization_pct"], att["unattributed"], att["stale"]) == (42, pytest.approx(0), False)
    assert quota.observed_rate(conn, cfg.pricing, "7d", NOW) == pytest.approx(PTS)
    with pytest.raises(AssertionError, match="not after"):
        report(conn, ids, 43, t=NOW - 3 * DAY)
```

- [ ] **Step 3: Run**

Run: `uv run --python 3.12 pytest -q tests/test_capacity_points.py`
Expected: PASS. If `observed_rate` differs from `PTS`, the model's weight is not 1 in the test pricing: read `quota.weighted_sql` and `conftest`'s pricing, set `PTS` to the weighted tokens one input token of `claude-sonnet-5` costs times 1000, and keep the helpers unchanged.

- [ ] **Step 4: Commit**

```bash
git add tests/tickets_helpers.py tests/test_capacity_points.py
git commit -m "Test helpers for a fresh weekly report with every point attributed

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: The selling rule

**Files:**
- Modify: `src/claude_proxy/capacity.py` (add `NO_REPORT`, `Context`, `context`, `check`, `Guard`)
- Modify: `src/claude_proxy/tickets.py`: `preview` (:762-781), `grant` (:835-837), `sold_out` (:876-887), `_shift` (:898-920), `cancel` (:942), `hand_over` (:975), `add_bonus` (:1013-1045), `upgrade_quote` (:1117-1121), `upgrade` (:1146)
- Test: `tests/test_capacity_points.py`

**Interfaces:**
- Consumes: Task 2 functions, Task 3 `check_capacity`/`verify_capacity`, Task 4 helpers.
- Produces:
  - `capacity.NO_REPORT: str`
  - `capacity.Context` with fields `week`, `fresh: bool`, `scale: float`, `used: float | None`, `owed: float`, `room: float | None` (unfloored), `sold: dict[float, float]`, `exclude: tuple`
  - `capacity.context(conn, cfg, account_id, now, exclude=()) -> Context`
  - `capacity.check(conn, cfg, account_id, share, share_5h, starts_at, ends_at, now, exclude=(), ctx=None) -> None`
  - `capacity.Guard(conn, cfg, account_id, now)` with `.verify(starts_at, ends_at) -> None`
  - `tickets.sold_out(...)` keeps its signature and accepts `ctx=None`; `upgrade_quote` returns `reason`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capacity_points.py`:

```python
from tests.test_tickets_capacity import add_user, grant
from tests.tickets_helpers import user


@pytest.fixture
def acct(db):
    """Shares lite 20 / standard 40 as on the live gateway, a ceiling of 100, a rate of PTS, the account at 1 since the
    week began. The snapshot is 4 days old at NOW: a test that acts at NOW first calls report() or live() for then."""
    conn, cfg, ids = seeded(db)
    cfg.tickets.max_sold_pct = 100
    tickets.set_share(conn, cfg, "lite", 20, ids["admin"])
    tickets.set_share(conn, cfg, "standard", 40, ids["admin"])
    report(conn, ids, 1, t=WEEK_START + 60)
    return conn, cfg, ids


def three_day_passes(conn, cfg, ids, used_each):
    """Three users' lite day passes from NOW - 1200, each having used `used_each` points."""
    live(conn, NOW - 1200, 1)
    out = []
    for n in range(3):
        add_user(conn, ids, f"u{n}")
        out.append(grant(conn, cfg, ids, who=f"u{n}", tier="lite", length="day", now=NOW - 1200))
        use(conn, ids[f"u{n}"], out[-1]["id"], used_each, NOW - 1000)
    return out


def test_free_room_sells_what_anthropic_reports_unused(acct):           # spec test 1
    conn, cfg, ids = acct
    three_day_passes(conn, cfg, ids, 3)                                  # each past its 2.86 piece: owed 0
    report(conn, ids, 42)
    ctx = capacity.context(conn, cfg, 1, NOW)
    assert (ctx.used, ctx.owed, ctx.room) == (42, pytest.approx(0), pytest.approx(53))
    t = grant(conn, cfg, ids, tier="standard", length="day")
    assert t["share_pct"] == 40 and tickets.sold_at(conn, 1, NOW) == 100


def test_a_full_week_refuses_with_the_points_named(acct):              # spec test 2
    conn, cfg, ids = acct
    three_day_passes(conn, cfg, ids, 3)
    report(conn, ids, 93)
    with pytest.raises(tickets.CapacityError, match=r"This week has 2\.0 points free and the pass needs 5\.7"):
        grant(conn, cfg, ids, tier="standard", length="day")
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 3


def test_owed_holds_back_what_running_passes_may_still_draw(acct):
    conn, cfg, ids = acct
    add_user(conn, ids, "bob")
    live(conn, NOW - 1200, 1)
    b = grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW - 1200)
    use(conn, ids["bob"], b["id"], 1, NOW - 1000)
    report(conn, ids, 90)
    ctx = capacity.context(conn, cfg, 1, NOW)
    assert ctx.owed == pytest.approx(40 / 7 - 1) and ctx.room == pytest.approx(100 - 90 - (40 / 7 - 1) - 5)
    assert tickets.sold_out(conn, cfg, "lite", "day", NOW)              # needs 2.86, 0.29 left


def test_later_weeks_are_capped_by_promised_pieces(acct):              # spec test 3
    conn, cfg, ids = acct
    cfg.tickets.max_sold_pct, cfg.tickets.max_sold_5h_pct = 60, 200
    report(conn, ids, 1)
    for n in ("a", "b"):
        add_user(conn, ids, n)
        grant(conn, cfg, ids, who=n, tier="standard", length="week")    # 17.1 here, 22.9 next week, each
    add_user(conn, ids, "c")
    with pytest.raises(tickets.CapacityError, match=r"The week of .* would hold 68\.6% .* over max_sold_pct"):
        grant(conn, cfg, ids, who="c", tier="standard", length="week")  # here 51.4 ≤ 60 − 1 − 5; next week 68.6 > 60
    assert capacity.context(conn, cfg, 1, NOW).sold[W[1]] == pytest.approx(2 * 40 * 4 / 7)


def test_a_week_pass_from_thursday_charges_each_week_its_piece(acct):  # spec test 4
    conn, cfg, ids = acct
    cfg.tickets.max_sold_pct = 60
    add_user(conn, ids, "a")
    thu = NOW + 2 * DAY
    live(conn, thu - 3600, 1)
    grant(conn, cfg, ids, who="a", tier="standard", length="month", now=thu - 3600, confirm_stale_rate=True)
    live(conn, thu, 1)
    assert capacity.context(conn, cfg, 1, thu).sold[W[1]] == pytest.approx(40)
    with pytest.raises(tickets.CapacityError, match=r"The week of .* would hold 74\.3%"):   # 40 + 240/7 next week
        capacity.check(conn, cfg, 1, 40, 40, thu, thu + 7 * DAY, thu)
    capacity.check(conn, cfg, 1, 20, 20, thu, thu + DAY, thu)          # this week only: fits


def test_a_pass_starting_next_week_needs_no_room_this_week(acct):      # spec test 5
    conn, cfg, ids = acct
    report(conn, ids, 99)
    capacity.check(conn, cfg, 1, 40, 40, NOW + 3 * DAY, NOW + 4 * DAY, NOW)
    with pytest.raises(tickets.CapacityError, match="points free"):
        capacity.check(conn, cfg, 1, 40, 40, NOW, NOW + DAY, NOW)


def test_unused_points_of_an_ended_pass_are_sold_again(acct):          # spec test 6, recycling
    conn, cfg, ids = acct
    add_user(conn, ids, "a")
    live(conn, NOW - 3 * DAY, 1)
    t = grant(conn, cfg, ids, who="a", tier="standard", length="day", now=NOW - 3 * DAY)
    use(conn, ids["a"], t["id"], 1, NOW - 3 * DAY + 3600)                # 1 of its 5.71
    report(conn, ids, 2)
    ctx = capacity.context(conn, cfg, 1, NOW)
    assert ctx.owed == 0 and ctx.room == pytest.approx(100 - 2 - 5)
    assert ctx.sold[W[0]] == pytest.approx(40 / 7)                      # sold, shown on the panel, not a gate


def test_a_pass_sold_now_leaves_the_room_by_its_whole_piece(acct):
    conn, cfg, ids = acct
    report(conn, ids, 1)
    before = capacity.context(conn, cfg, 1, NOW).room
    grant(conn, cfg, ids, tier="standard", length="day")
    assert capacity.context(conn, cfg, 1, NOW).room == pytest.approx(before - 40 / 7)


def test_replacing_used_up_passes_frees_their_owed_points(acct):       # Review Focus 3
    conn, cfg, ids = acct
    cfg.tickets.max_sold_5h_pct = 40
    live(conn, NOW - 3600, 1)
    a = grant(conn, cfg, ids, tier="standard", length="day", now=NOW - 3600)
    use(conn, ids["alice"], a["id"], 40 / 7, NOW - 1000)                  # used up: its whole piece
    report(conn, ids, 1 + 40 / 7)
    t = grant(conn, cfg, ids, tier="standard", length="day", start_now=True)   # replaces it: 5-hour 40 fits 40
    assert tickets.get(conn, a["id"])["used_up_at"] == NOW and t["starts_at"] == NOW


@pytest.mark.parametrize("case", ["stale", "none", "reset_passed"])
def test_no_fresh_report_sells_nothing(db, case):                       # spec test 7
    conn, cfg, ids = seeded(db)
    if case == "stale":
        live(conn, NOW - 2000)
    elif case == "reset_passed":
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                     (NOW - 60, "header", "7d", 10, NOW - 10))
    with pytest.raises(tickets.CapacityError, match="No fresh usage report"):
        grant(conn, cfg, ids)
    assert tickets.sold_out(conn, cfg, "lite", "day", NOW)
    live(conn, NOW)
    assert not tickets.sold_out(conn, cfg, "lite", "day", NOW)


def test_old_week_snapshot_after_the_reset_sells_nothing(db):           # Review Focus 1
    conn, cfg, ids = seeded(db)
    conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                 (NOW - 60, "header", "7d", 80, NOW - 30))                  # fresh by age, from the week that just ended
    assert capacity.context(conn, cfg, 1, NOW).fresh is False
    with pytest.raises(tickets.CapacityError, match="No fresh usage report"):
        grant(conn, cfg, ids)


def test_usage_jump_between_preview_and_grant_refuses_cleanly(acct):    # Review Focus 2
    conn, cfg, ids = acct
    report(conn, ids, 1, t=NOW - 120)
    assert tickets.preview(conn, cfg, user(conn, ids["alice"]), "standard", "day", "EUR", NOW)["available"]
    report(conn, ids, 97, t=NOW - 30)
    with pytest.raises(tickets.CapacityError, match="points free"):
        grant(conn, cfg, ids, tier="standard", length="day")
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_the_5h_ceiling_still_bounds_passes_running_at_once(acct):      # spec test 8
    conn, cfg, ids = acct
    report(conn, ids, 1)
    for n in range(4):
        add_user(conn, ids, f"u{n}")
        grant(conn, cfg, ids, who=f"u{n}", tier="lite", length="day")    # 4 × 20 = 80 of 5-hour share, 11.4 owed
    add_user(conn, ids, "v")
    with pytest.raises(tickets.CapacityError, match="5-hour shares would reach 120%"):
        grant(conn, cfg, ids, who="v", tier="standard", length="day")
    cfg.tickets.max_sold_5h_pct = 150
    assert grant(conn, cfg, ids, who="v", tier="standard", length="day")["share_pct"] == 40


def test_outside_use_scales_owed_and_check_1_but_not_later_weeks(acct):  # spec test 9
    conn, cfg, ids = acct
    live(conn, NOW, 11)                                                  # 10 points nobody at the gateway used
    grant(conn, cfg, ids, tier="standard", length="week")
    ctx = capacity.context(conn, cfg, 1, NOW)
    assert ctx.scale == pytest.approx(90)
    assert ctx.owed == pytest.approx(40 * 3 / 7 * 0.9)
    assert ctx.sold[W[1]] == pytest.approx(40 * 4 / 7)


def test_without_a_rate_owed_uses_the_attributed_points(acct):          # spec test 10
    conn, cfg, ids = acct
    live(conn, NOW - 1200, 1)
    t = grant(conn, cfg, ids, tier="standard", length="day", now=NOW - 1200)
    att = {"utilization_pct": 3.0, "stale": False, "resets_at": W[1], "ticket_shares": {t["id"]: 2.0}, "unattributed": 0.0}
    assert capacity.owed(conn, cfg, 1, W, att, None, 100.0, NOW) == pytest.approx(40 / 7 - 2)


def test_a_bonus_is_judged_as_one_change(acct):                         # spec test 11
    conn, cfg, ids = acct
    report(conn, ids, 1, t=NOW - 120)
    t = grant(conn, cfg, ids, tier="standard", length="day")
    report(conn, ids, 80, t=NOW - 30)                                    # room 100 − 80 − 5.71 − 5 = 9.3
    with pytest.raises(tickets.CapacityError, match="points free"):     # share over 2 days 11.4 + an extra day 5.7
        tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=40, extra_days=1, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM ticket_bonuses").fetchone()[0] == 0
    tickets.add_bonus(conn, cfg, ids["admin"], t["id"], share_pct=40, now=NOW, ends_at=NOW + DAY)   # 5.7 alone fits


def test_hand_set_weekly_shares_are_not_reserved(acct):                 # spec test 12
    conn, cfg, ids = acct
    report(conn, ids, 1)
    add_user(conn, ids, "h")
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (ids["h"], "share_7d", "*", "100", "pct"))
    assert capacity.context(conn, cfg, 1, NOW).room == pytest.approx(100 - 1 - 5)


def test_cancel_whose_move_pulls_a_pass_into_a_full_week_keeps_dates(acct):   # spec test 14
    conn, cfg, ids = acct
    report(conn, ids, 1)
    a = grant(conn, cfg, ids, tier="standard", length="week")           # NOW .. NOW + 7d
    q = grant(conn, cfg, ids, tier="standard", length="day")            # queued NOW + 7d: next week
    report(conn, ids, 99, t=NOW + 30)
    r = tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + 60)  # q would move here: 5.7 more owed, no room
    assert r["dates_kept"] and "points free" in r["reason"]
    assert tickets.get(conn, q["id"])["starts_at"] == a["ends_at"]
    assert tickets.get(conn, a["id"])["cancelled_at"] == NOW + 60


def test_cancel_whose_move_stays_inside_the_week_adds_nothing(acct):
    conn, cfg, ids = acct
    report(conn, ids, 1)
    a = grant(conn, cfg, ids, tier="standard", length="day")            # NOW .. NOW + 1d
    q = grant(conn, cfg, ids, tier="standard", length="day")            # queued NOW + 1d, still this week
    report(conn, ids, 99, t=NOW + 30)
    r = tickets.cancel(conn, cfg, ids["admin"], a["id"], now=NOW + 60)
    assert not r["dates_kept"] and tickets.get(conn, q["id"])["starts_at"] == NOW + 60


def test_lowering_max_sold_pct_refuses_and_cancels_nothing(acct):       # spec test 15
    conn, cfg, ids = acct
    report(conn, ids, 1)
    grant(conn, cfg, ids, tier="standard", length="week")
    cfg.tickets.max_sold_pct = 20
    add_user(conn, ids, "b")
    with pytest.raises(tickets.CapacityError, match="points free"):
        grant(conn, cfg, ids, who="b", tier="lite", length="day")
    assert conn.execute("SELECT COUNT(*) FROM tickets WHERE cancelled_at IS NULL").fetchone()[0] == 1


def test_upgrade_is_checked_in_points(acct):
    conn, cfg, ids = acct
    report(conn, ids, 1, t=NOW - 120)
    t = grant(conn, cfg, ids, tier="lite", length="week")               # 20: 8.6 here
    report(conn, ids, 90, t=NOW - 30)                                    # room 100 − 90 − 8.6 − 5 < 0
    q = tickets.upgrade_quote(conn, cfg, t, "standard", "EUR", NOW)
    assert q["sold_out"] and "points free" in q["reason"]
    with pytest.raises(tickets.CapacityError, match="points free"):
        tickets.upgrade(conn, cfg, ids["admin"], user(conn, ids["alice"]), t["id"], "standard", "EUR", now=NOW)
```

Also add `live(conn, NOW)` as the first line after `seeded(db)` in `test_the_5h_sum_reads_5h_shares_and_its_own_ceiling` (Task 3).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --python 3.12 pytest -q tests/test_capacity_points.py`
Expected: FAIL (`AttributeError: module 'claude_proxy.capacity' has no attribute 'context'`).

- [ ] **Step 3: Add the rule to `capacity.py`**

```python
import time
from dataclasses import dataclass

NO_REPORT = "No fresh usage report from Anthropic; try again in a few minutes."


def _day(t: float) -> str:
    return time.strftime("%d.%m", time.gmtime(t))


@dataclass
class Context:
    """One look at the account for judging hypothetical passes (preview, sold-out flags, price lists)."""
    week: tuple[float, float] | None
    fresh: bool
    scale: float
    used: float | None
    owed: float
    room: float | None          # max_sold_pct − U − owed − sell_margin_pct, not floored; None without a fresh report
    sold: dict[float, float]    # sold_by_week, no exclude
    exclude: tuple


def _att(conn, cfg, now):
    return quota.attribution_cached(conn, cfg.pricing, "7d", now=now, stale_after_s=cfg.quota.stale_after_s)


def context(conn: sqlite3.Connection, cfg: Config, account_id: int, now: float, exclude=()) -> Context:
    week = tickets.week_bounds(conn, cfg, now)
    att = _att(conn, cfg, now)
    ok = week is not None and fresh(att, now)
    scale = _capacity(att, now)
    if week is None:
        return Context(None, False, scale, att["utilization_pct"], 0.0, None, {}, _ids(exclude))
    rate = quota.observed_rate(conn, cfg.pricing, "7d", now)
    owe = owed(conn, cfg, account_id, week, att, rate, scale, now, exclude)
    room_ = cfg.tickets.max_sold_pct - att["utilization_pct"] - owe - cfg.tickets.sell_margin_pct if ok else None
    return Context(week, ok, scale, att["utilization_pct"], owe, room_, sold_by_week(conn, account_id, week), _ids(exclude))


def _week_full(w: float, total: float, ceiling: float) -> str:
    return f"The week of {_day(w)} would hold {total:.1f}% with this pass, over max_sold_pct ({ceiling:g}%)."


def _no_room(free: float, need: float) -> str:
    return f"This week has {max(0.0, free):.1f} points free and the pass needs {need:.1f}."


def check(conn: sqlite3.Connection, cfg: Config, account_id: int, share: float, share_5h: float, starts_at: float,
          ends_at: float, now: float, exclude=(), ctx: Context | None = None) -> None:
    """The three checks of spec §2 for a pass (or bonus share) not written yet; CapacityError names the first that fails."""
    ctx = ctx or context(conn, cfg, account_id, now, exclude)
    if not ctx.fresh:
        raise tickets.CapacityError(NO_REPORT)
    ps = pieces(ctx.week, share, starts_at, ends_at)
    ws = ctx.week[0]
    need = ps.get(ws, 0.0) * ctx.scale / 100
    if need > 1e-12 and need > ctx.room + 1e-9:
        raise tickets.CapacityError(_no_room(ctx.room, need))
    for w, p in sorted(ps.items()):
        if w > ws and ctx.sold.get(w, 0.0) + p > cfg.tickets.max_sold_pct + 1e-9:
            raise tickets.CapacityError(_week_full(w, ctx.sold.get(w, 0.0) + p, cfg.tickets.max_sold_pct))
    tickets.check_capacity(conn, cfg, account_id, share_5h, int(starts_at), int(ends_at), list(ctx.exclude) or None)


class Guard:
    """Spec §2 for a change written row by row (a bonus with share and days, a queue move): take it before the change,
    call verify() after every row is written, inside the caller's transaction or savepoint. The change is judged by what
    it adds: more owed this week must fit the room that was there, a later week it raises must stay under max_sold_pct,
    and the 5-hour sum over its period must stay under max_sold_5h_pct. A change that adds nothing to owed and raises
    no later week passes the first two without needing a fresh report."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config, account_id: int, now: float):
        self.conn, self.cfg, self.account_id, self.now = conn, cfg, account_id, now
        self.week = tickets.week_bounds(conn, cfg, now)
        self.owed0, self.sold0 = self._state() if self.week else (0.0, {})

    def _state(self) -> tuple[float, dict[float, float]]:
        att = _att(self.conn, self.cfg, self.now)
        rate = quota.observed_rate(self.conn, self.cfg.pricing, "7d", self.now)
        return (owed(self.conn, self.cfg, self.account_id, self.week, att, rate, _capacity(att, self.now), self.now),
                sold_by_week(self.conn, self.account_id, self.week))

    def verify(self, starts_at: float, ends_at: float) -> None:
        if self.week is not None:
            owed1, sold1 = self._state()
            ws, ceiling = self.week[0], self.cfg.tickets.max_sold_pct
            added = owed1 - self.owed0
            later = [w for w in sorted(sold1) if w > ws and sold1[w] > self.sold0.get(w, 0.0) + 1e-9]
            if added > 1e-9 or later:
                att = _att(self.conn, self.cfg, self.now)
                if not fresh(att, self.now):
                    raise tickets.CapacityError(NO_REPORT)
                free = ceiling - att["utilization_pct"] - self.owed0 - self.cfg.tickets.sell_margin_pct
                if added > 1e-9 and added > free + 1e-9:
                    raise tickets.CapacityError(_no_room(free, added))
                for w in later:
                    if sold1[w] > ceiling + 1e-9:
                        raise tickets.CapacityError(_week_full(w, sold1[w], ceiling))
        elif ends_at > starts_at:
            raise tickets.CapacityError(NO_REPORT)
        if ends_at > starts_at:
            tickets.verify_capacity(self.conn, self.cfg, self.account_id, int(starts_at), int(ends_at))
```

- [ ] **Step 4: Wire `tickets.py` to the rule**

Every edit imports lazily, as `next_start` imports `limits`: put `from . import capacity   # here, not at the top: capacity imports tickets` as the first line of each function body below.

`preview`, replace the `try: check_capacity(...)` block:

```python
    try:   # used-up tickets this one replaces end as it starts: their owed points and 5-hour share go with them
        capacity.check(conn, cfg, ACCOUNT_ID, share, share_5h, starts_at, ends_at, now, exclude=[t["id"] for t in replaces])
        available, reason = True, None
    except CapacityError as e:
        available, reason = False, str(e)
```

`grant`: it already runs `preview` inside its `BEGIN IMMEDIATE`, so the checks are made at grant time with the right `exclude`. Replace `check_capacity(conn, cfg, ACCOUNT_ID, p["share_pct"], p["starts_at"], p["ends_at"])` with:

```python
        if not p["available"]:   # judged by preview just now, inside this transaction (2026-11-04 spec, §2)
            raise CapacityError(p["reason"])
```

`sold_out`:

```python
def sold_out(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, now: float, starts_at: float | None = None,
             exclude=None, ctx=None) -> bool:
    """A tier and length is sold out when a ticket starting now, or at `starts_at`, fails spec §2, or no fresh weekly
    report exists (price_table tells that case apart as `unavailable`). `ctx`: a capacity.context to reuse."""
    from . import capacity   # here, not at the top: capacity imports tickets
    now = _now(now)
    start = now if starts_at is None else int(starts_at)
    try:
        capacity.check(conn, cfg, ACCOUNT_ID, effective_share(conn, cfg, tier), effective_share_5h(conn, cfg, tier), start,
                       start + LENGTHS[length] * DAY, now, exclude or (), ctx)
    except CapacityError:
        return True
    return False
```

`_shift`: it has no `now` today. Add the parameter, `def _shift(conn, cfg: Config, chain: list[dict], delta: int, now: float) -> None:`, pass `now` from its three callers (`cancel`: `_shift(conn, cfg, chain, shift, now)`; `hand_over`: `_shift(conn, cfg, chain, now - chain[0]["starts_at"], now)`; `add_bonus`: `_shift(conn, cfg, chain, new_end - chain[0]["starts_at"], now)`), and replace both `check_capacity(...)` calls and the trailing `verify_capacity(...)` with one guard:

```python
    conn.execute("SAVEPOINT moves")
    try:
        guard = capacity.Guard(conn, cfg, chain[0]["account_id"], now)
        for tk in (chain if delta < 0 else reversed(chain)):
            conn.execute("UPDATE tickets SET starts_at=starts_at+?, ends_at=ends_at+? WHERE id=?", (delta, delta, tk["id"]))
            conn.execute("UPDATE ticket_bonuses SET starts_at=starts_at+?, ends_at=ends_at+? WHERE ticket_id=? AND cancelled_at IS NULL",
                         (delta, delta, tk["id"]))
        guard.verify(min(tk["starts_at"] for tk in chain) + delta, max(tk["effective_end"] for tk in chain) + delta)
        conn.execute("RELEASE moves")
```

(The rest of `_shift` is unchanged. Docstring: "One savepoint, one Guard over the whole move: a move that does not fit undoes them all and raises CapacityError.")

`add_bonus`: right after `old_end, new_end, moved = ...`, add `guard = capacity.Guard(conn, cfg, t["account_id"], now)` (before the `if extra_days:` block, so the guard's "before" predates the queue move). Delete the two `check_capacity(...)` calls (`if new_end > now: check_capacity(...)` and `if e > now: check_capacity(...)`) and replace the final `verify_capacity(...)` with:

```python
        hi = max(e, new_end)
        if hi > now:
            guard.verify(max(min(s, old_end) if extra_days else s, now), hi)
```

`upgrade_quote`: replace the `try: check_capacity(...)` block with

```python
    try:
        capacity.check(conn, cfg, ticket["account_id"], share, max(0.0, share_5h), now, ticket["effective_end"], now)
        sold, reason = False, None
    except CapacityError as e:
        sold, reason = True, str(e)
```

and add `"reason": reason` to its returned dict (after `"sold_out": sold`).

`upgrade`: replace `check_capacity(conn, cfg, t["account_id"], q["share_pct"], now, t["effective_end"])` with

```python
        if q["sold_out"]:   # judged by upgrade_quote just now, inside this transaction
            raise CapacityError(q["reason"])
```

- [ ] **Step 5: Run the new tests**

Run: `uv run --python 3.12 pytest -q tests/test_capacity_points.py`
Expected: PASS. If `test_a_full_week_refuses_with_the_points_named` reports a different free figure, recompute `100 − 93 − 0 − 5 = 2.0`; another number means owed counts the three passes (their use must exceed 20/7 ≈ 2.86).

- [ ] **Step 6: Commit (the old suite is migrated in Task 6; do not push between the two)**

```bash
git add src/claude_proxy/capacity.py src/claude_proxy/tickets.py tests/test_capacity_points.py
git commit -m "Passes are sold in points of the week against Anthropic's reported usage; ended passes' unused points sell again

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Migrate the existing suite, and the public "temporarily unavailable" flag

**Files:**
- Modify: tests listed below
- Modify: `src/claude_proxy/tickets.py:1229-1264` (`price_table`), `src/claude_proxy/orders.py:148-158`
- Modify: `src/claude_proxy/static/home.js:75,83`, `src/claude_proxy/static/app.js:868,873`, `src/claude_proxy/static/i18n.js`
- Test: `tests/test_capacity_points.py`, the migrated files

**Interfaces:**
- Produces: each `price_table` length gains `"unavailable": bool`; when true, `sold_out` is false. `orders._check_on_sale(conn, cfg, tier, length, now, user_id, start_now)`.

- [ ] **Step 1: Write the failing tests for the flag**

Append to `tests/test_capacity_points.py`:

```python
def test_price_table_says_unavailable_not_sold_out_without_a_report(db):
    conn, cfg, ids = seeded(db)
    l = tickets.price_table(conn, cfg, NOW, "EUR")["tiers"][0]["lengths"]["day"]
    assert (l["sold_out"], l["unavailable"]) == (False, True)
    live(conn, NOW)
    l = tickets.price_table(conn, cfg, NOW, "EUR")["tiers"][0]["lengths"]["day"]
    assert (l["sold_out"], l["unavailable"]) == (False, False)


def test_price_table_builds_one_context_per_start(db, monkeypatch):     # Review Focus 4
    conn, cfg, ids = seeded(db)
    live(conn, NOW)
    calls = []
    real = capacity.context
    monkeypatch.setattr(capacity, "context", lambda *a, **k: calls.append(1) or real(*a, **k))
    tickets.price_table(conn, cfg, NOW, "EUR", starts_at=NOW + DAY)
    assert len(calls) == 2


def test_an_order_without_a_report_is_refused_as_unavailable(db):
    from claude_proxy import orders
    conn, cfg, ids = seeded(db)
    with pytest.raises(orders.OrderError, match="temporarily unavailable"):
        orders._check_on_sale(conn, cfg, "lite", "day", NOW, ids["alice"], False)
```

- [ ] **Step 2: Implement the flag**

In `price_table`, before the tier loop:

```python
    from . import capacity   # here, not at the top: capacity imports tickets
    ctx_now = capacity.context(conn, cfg, ACCOUNT_ID, now, exclude or ())
    ctx_later = capacity.context(conn, cfg, ACCOUNT_ID, now) if later else None
```

and replace the two flags in the length dict:

```python
                               "sold_out": ctx_now.fresh and sold_out(conn, cfg, tid, length, now, None, exclude, ctx_now),
                               "unavailable": not ctx_now.fresh,
                               **({"sold_out_later": sold_out(conn, cfg, tid, length, now, starts_at, None, ctx_later)} if later else {})}
```

In `orders.py`, move the two sold-out checks (from `mine = ...` to the second `raise OrderError(409, ...)`) into a function and call `_check_on_sale(conn, cfg, tier, length, now, user_id, start_now)` in their place:

```python
UNAVAILABLE = "Passes are temporarily unavailable; try again in a few minutes."


def _check_on_sale(conn, cfg, tier: str, length: str, now: float, user_id: int | None, start_now: bool) -> None:
    """Judged as the price list judges it (spec section 2, Sold out; 2026-11-04 spec §2): without a fresh weekly
    report nothing is on sale; else a pass starting now, without the buyer's used-up tickets, and one starting
    where the grant will put it."""
    from . import capacity
    if not capacity.context(conn, cfg, tickets.ACCOUNT_ID, now).fresh:
        raise OrderError(409, UNAVAILABLE)
    mine = [t["id"] for t in tickets.next_start(conn, cfg, user_id, now, True)[1]] if user_id is not None else None
    if tickets.sold_out(conn, cfg, tier, length, now, None, mine):
        raise OrderError(409, "That pass is sold out.")
    if user_id is not None:
        starts_at, replaces = tickets.next_start(conn, cfg, user_id, now, start_now)
        if starts_at > now and tickets.sold_out(conn, cfg, tier, length, now, starts_at, [t["id"] for t in replaces]):
            raise OrderError(409, "That pass is sold out for when your current pass ends.")
```

In `home.js` line 75 use `${l.sold_out || l.unavailable ? " sold-out" : ""}`, and lines 83-84:

```js
    ${l.sold_out || l.unavailable ? `<span class="btn get" aria-disabled="true">${t(l.unavailable ? "home.unavailable" : "home.sold_out")}</span>`
```

In `app.js` line 868: `const canOrder = (tier, k) => !!tier.lengths[k] && !tier.lengths[k].sold_out && !tier.lengths[k].unavailable;` and in line 873 replace `${l.sold_out ? ` <span class="badge">${t("price.sold_out")}</span>` : ""}` with `${l.sold_out || l.unavailable ? ` <span class="badge">${t(l.unavailable ? "price.unavailable" : "price.sold_out")}</span>` : ""}`.

In `i18n.js`, next to each language's `price.sold_out` and `home.sold_out`:

```js
    "price.unavailable": "temporarily unavailable",
    "home.unavailable": "Temporarily unavailable",
```
```js
    "price.unavailable": "موقتاً در دسترس نیست",
    "home.unavailable": "موقتاً در دسترس نیست",
```

and in the server-message map beside `"That pass is sold out."`:

```js
  "Passes are temporarily unavailable; try again in a few minutes.": "بسته‌ها موقتاً در دسترس نیستند؛ چند دقیقه دیگر دوباره امتحان کنید.",
  "No fresh usage report from Anthropic; try again in a few minutes.": "گزارش تازه‌ای از مصرف از Anthropic نرسیده است؛ چند دقیقه دیگر دوباره امتحان کنید.",
```

- [ ] **Step 3: Run the whole suite and migrate each failure**

Run: `uv run --python 3.12 pytest -q -x --no-header -p no:cacheprovider 2>&1 | tail -30`, fix, repeat until green. Apply these rules in order; each names the symptom and the fix.

1. **"No fresh usage report"** in a test that only needs a sale to go through: add `live(conn, <the grant's now>)` right before the first grant at that time (`from tests.tickets_helpers import live`). Tests on the real clock (`test_tickets_web.py`, `test_api_proxy.py`, `test_api_credit.py`, `test_payments*.py`, `test_orders_web.py`, `test_web.py`, `test_api_web.py`, `test_tickets_delete.py`, `test_tickets_upgrade.py`, `test_review_fixes.py`): `live(conn, time.time())` in the fixture that grants, or before the first grant. A test that walks time forward days needs `live(conn, t)` at each new grant time. With the defaults (`max_sold_pct` 80, margin 5, utilization 0) the room is 75, so every default-tier pass fits.
2. **A grant refused with "points free" after the test lowered `max_sold_pct`** to a value near the pass's piece (`test_tickets_web.py:178-179` and `test_orders.py:643-644` set 5; `test_tickets_limits.py:193,211` set 7): the margin now eats the ceiling. Add `cfg.tickets.sell_margin_pct = 0` next to that line; the numbers then work (room 5 ≥ a lite day's 0.71; room 7 ≥ a 7% week pass starting at the week start).
3. **A message assertion on the old wording** (`"50% is sold"`, `"75% is sold"`, `"Not enough capacity"`: `test_tickets_web.py:183`, `test_payments.py:646`, `test_tickets_capacity.py:250,366,423` and the two per-instant tests at 120-145 and 500-535): these tests exercise the per-instant rule, which is now the 5-hour check. Default tiers have no 5-hour share, so 5-hour equals weekly share and the arithmetic holds. Set `cfg.tickets.max_sold_5h_pct = cfg.tickets.max_sold_pct` at the start of the test (or to the value the test sets `max_sold_pct` to), change the match to `"5-hour shares would reach"`, and rename `test_capacity_check_samples_every_start_inside_the_period` to `test_5h_check_samples_every_start_inside_the_period`. `test_fractional_shares_against_the_ceiling`: set `cfg.tickets.max_sold_5h_pct = 80`.
4. **DID NOT RAISE where the test expected "sold out" from summed shares** (`test_tickets_used_up.py:288`, `test_tickets_capacity.py:294` `test_bonus_days_refused_when_a_moved_ticket_does_not_fit`, and any other): same cause as rule 3. Set `max_sold_5h_pct` to the ceiling the test's comment assumes (25 at 288; 50 at 294) so the 5-hour check refuses as the weekly one did.
5. **Tests that assert the old weekly rule** and cannot be kept by rules 3-4:
   - `test_gateway_capacity.py:89 test_selling_still_checks_max_sold_pct_not_runtime_capacity`: rename to `test_selling_checks_the_weeks_room_at_the_reported_capacity`; set `cfg.tickets.max_sold_5h_pct = 200`; keep the snapshot at 50 outside use (scale 50); the `env` fixture's lite week pass owes 1.07 and three standard week passes owe 3 × 10.7 × 0.5 = 16.1, so the room is `80 − 50 − 17.1 − 5 = 7.9`; assert a fourth standard week pass (5.4 scaled) still fits, then set `cfg.tickets.max_sold_pct = 70` and assert a fifth refuses with `"points free"`.
   - `test_tickets_capacity.py test_max_sold_pct_refuses_what_100_would_allow_and_lowering_it_keeps_existing_tickets` and `test_capacity_panel_reports_sold_now_and_the_peak_over_30_days`: their `tickets.capacity(...)` dict assertions move to Task 7; in this task replace the dict assertion in the first with `assert tickets.capacity(conn, cfg, NOW)["five_hour"]["now"] == 80` (after rule 3's 5-hour ceiling) and mark the panel test `@pytest.mark.skip(reason="rewritten in Task 7")`.
6. **`test_payments.py:295` and `test_payments_web.py:136`** monkeypatch `tickets.check_capacity` to make the callback's grant fail. That still works: `grant` runs `preview`, which runs `capacity.check`, which calls `tickets.check_capacity` by module attribute. Leave them; if either fails, the preview result is not being honoured in `grant` (Task 5).
7. **Any other failure** is a bug in Tasks 2-5 or an unlisted dependence on the old rule. Read the test's docstring; if it asserts behaviour the spec keeps (enforcement, borrowing, queueing), fix the code; if it asserts the per-instant weekly rule, rewrite it to the points rule and say so in the commit message.

- [ ] **Step 4: Verify**

Run: `uv run --python 3.12 pytest -q`
Expected: all pass except the one skipped panel test.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "Price lists and orders say temporarily unavailable without a fresh weekly report; the suite follows the points rule

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: The capacity panel

**Files:**
- Modify: `src/claude_proxy/capacity.py` (add `summary`), `src/claude_proxy/tickets.py:868-873` (`capacity`)
- Modify: `src/claude_proxy/static/app.js:1172-1186`, `src/claude_proxy/static/app.css` (after line 142), `src/claude_proxy/static/i18n.js`
- Test: `tests/test_capacity_points.py`, `tests/test_tickets_capacity.py` (the skipped panel test), `tests/test_tickets_web.py`

**Interfaces:**
- Produces: `capacity.summary(conn, cfg, account_id, now) -> dict` with keys `week`, `peak_week`, `five_hour`, `hand_set_7d_pct` as in spec §6; `tickets.capacity(conn, cfg, now)` returns it; `GET /api/admin/capacity` (unchanged code) returns it plus `utilization`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capacity_points.py`:

```python
def test_summary_reports_the_week_in_points(acct):                      # spec test 18
    conn, cfg, ids = acct
    add_user(conn, ids, "a")
    live(conn, NOW - 3 * DAY, 1)
    ended = grant(conn, cfg, ids, who="a", tier="standard", length="day", now=NOW - 3 * DAY)
    use(conn, ids["a"], ended["id"], 1, NOW - 3 * DAY + 3600)
    report(conn, ids, 2)
    grant(conn, cfg, ids, tier="lite", length="week")
    add_user(conn, ids, "h")
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (ids["h"], "share_7d", "*", "93", "pct"))
    s = capacity.summary(conn, cfg, 1, NOW)
    w = s["week"]
    assert (w["start"], w["end"], w["fresh"], w["used"], w["margin"], w["max_sold_pct"]) == (W[0], W[1], True, 2, 5, 100)
    assert w["owed"] == pytest.approx(20 * 3 / 7)
    assert w["free"] == pytest.approx(100 - 2 - 20 * 3 / 7 - 5)
    assert w["sold"] == pytest.approx(40 / 7 + 20 * 3 / 7)
    assert w["returned"] == pytest.approx(40 / 7 - 1)
    assert s["peak_week"] == {"start": W[1], "sold": pytest.approx(20 * 4 / 7)}
    assert s["five_hour"] == {"now": 20, "peak_30d": 20, "max_sold_5h_pct": 100}
    assert s["hand_set_7d_pct"] == 93


def test_summary_without_a_report_is_not_fresh_and_frees_nothing(db):
    conn, cfg, ids = seeded(db)
    w = capacity.summary(conn, cfg, 1, NOW)["week"]
    assert (w["fresh"], w["free"]) == (False, 0)
```

Replace the skipped `test_capacity_panel_reports_sold_now_and_the_peak_over_30_days` in `tests/test_tickets_capacity.py` with:

```python
def test_capacity_panel_reports_the_5h_sum_now_and_its_peak_over_30_days(db):
    conn, cfg, ids = seeded(db)
    bob = add_user(conn, ids, "bob")
    live(conn, NOW)
    grant(conn, cfg, ids, tier="lite", length="month")
    live(conn, NOW + 10 * DAY)
    grant(conn, cfg, ids, who="bob", tier="standard", length="day", now=NOW + 10 * DAY, confirm_stale_rate=True)
    assert tickets.capacity(conn, cfg, NOW)["five_hour"] == {"now": 5, "peak_30d": 30, "max_sold_5h_pct": 100}
    assert tickets.capacity(conn, cfg, NOW + 31 * DAY)["five_hour"]["now"] == 0
```

In `tests/test_tickets_web.py`, find the test that calls `/api/admin/capacity` (search `api/admin/capacity`) and change its expected keys to `{"week", "peak_week", "five_hour", "hand_set_7d_pct", "utilization"}`; keep its non-admin 403 assertion.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --python 3.12 pytest -q tests/test_capacity_points.py tests/test_tickets_capacity.py tests/test_tickets_web.py`
Expected: FAIL (`AttributeError: ... 'summary'`).

- [ ] **Step 3: Implement `summary`**

In `capacity.py`:

```python
def _returned(conn, cfg, account_id: int, week, att: dict, rate: float | None, now: float) -> float:
    """Unused points of passes that ended this week: their sold piece of the week less their use."""
    ws, we = week
    rows = [dict(r) for r in conn.execute(
        f"SELECT {tickets.TICKET_COLS}, {SOLD_END} AS sold_end FROM tickets t WHERE t.account_id=? AND t.cancelled_at IS NULL "
        f"AND t.starts_at<? AND {db.TICKET_EFFECTIVE_END}<=? AND {db.TICKET_EFFECTIVE_END}>?", (account_id, we, now, ws))]
    uses = _week_use(conn, cfg, [p["id"] for p in rows], ws, now) if rate and rows else {}
    total = 0.0
    for p in rows:
        piece = pieces(week, p["share_pct"], max(ws, p["starts_at"]), p["sold_end"]).get(ws, 0.0)
        use = uses.get(p["id"], 0.0) / rate if rate else att["ticket_shares"].get(p["id"], 0.0)
        total += max(0.0, piece - use)
    return total


def summary(conn: sqlite3.Connection, cfg: Config, account_id: int, now: float) -> dict:
    """The capacity panel (spec §6)."""
    from . import limits   # here, not at the top: limits imports capacity
    ctx = context(conn, cfg, account_id, now)
    att = _att(conn, cfg, now)
    rate = quota.observed_rate(conn, cfg.pricing, "7d", now) if ctx.week else None
    ws = ctx.week[0] if ctx.week else None
    later = {w: v for w, v in ctx.sold.items() if ws is not None and ws < w < now + 30 * DAY}
    peak = max(later.items(), key=lambda kv: kv[1]) if later else None
    points = {now, *tickets._starts_within(conn, account_id, now, now + 30 * DAY)}
    hand = sum(float(r["value"]) for r in conn.execute("SELECT user_id, value FROM limits WHERE kind='share_7d'")
               if not limits._gated(conn, cfg, r["user_id"]))
    return {
        "week": {"start": ws, "end": ctx.week[1] if ctx.week else None, "fresh": ctx.fresh, "used": ctx.used,
                 "owed": ctx.owed, "margin": cfg.tickets.sell_margin_pct,
                 "free": max(0.0, ctx.room) if ctx.room is not None else 0.0,
                 "sold": ctx.sold.get(ws, 0.0) if ws is not None else 0.0,
                 "returned": _returned(conn, cfg, account_id, ctx.week, att, rate, now) if ctx.week else 0.0,
                 "max_sold_pct": cfg.tickets.max_sold_pct},
        "peak_week": {"start": peak[0], "sold": peak[1]} if peak else None,
        "five_hour": {"now": tickets.sold_at(conn, account_id, now),
                      "peak_30d": max(tickets.sold_at(conn, account_id, p) for p in points),
                      "max_sold_5h_pct": cfg.tickets.max_sold_5h_pct},
        "hand_set_7d_pct": hand,
    }
```

Replace `tickets.capacity`:

```python
def capacity(conn: sqlite3.Connection, cfg: Config, now: float) -> dict:
    """The capacity panel's figures (2026-11-04 spec, §6)."""
    from . import capacity as cap   # here, not at the top: capacity imports tickets
    return cap.summary(conn, cfg, ACCOUNT_ID, _now(now))
```

`web.py` needs no change: it already merges `utilization` into `tickets.capacity(...)`.

- [ ] **Step 4: Rewrite the panel**

In `app.css` after line 142:

```css
.meter.stack { display: flex; }
.meter.stack > span { border-radius: 0; }
.meter.stack > .seg-owed { opacity: .5; }
.meter.stack > .seg-margin { background: var(--ink-2); opacity: .35; }
.meter.stack > .seg-free { background: transparent; }
.meter.stack > .seg-headroom { background: var(--ink-2); opacity: .15; }
```

In `app.js` `renderTickets`, replace the `pct` and `of` constants and the Capacity card (the first `<div class="card">` of the grid, through its `</div>`) with:

```js
  const pct = (v, max) => meter((100 * v) / max);
  const util = (b) => (cap.utilization[b].utilization_pct == null ? "—" : `${fmtPct(cap.utilization[b].utilization_pct)}${cap.utilization[b].stale ? ` (${t("quota.stale")})` : ""}`);
  const pts = (v) => new Intl.NumberFormat(LOC, { maximumFractionDigits: 1 }).format(+v);
  const w = cap.week, f = cap.five_hour;
  const seg = (cls, v) => `<span class="${cls}" style="width:${Math.max(0, Math.min(100, v))}%"></span>`;
  const weekMeter = w.fresh
    ? `<div class="meter stack">${seg("seg-used", w.used)}${seg("seg-owed", w.owed)}${seg("seg-margin", w.margin)}${seg("seg-free", w.free)}${seg("seg-headroom", 100 - w.max_sold_pct)}</div>
       <p class="sub">${t("tk.seg_legend", { used: pts(w.used), owed: pts(w.owed), margin: pts(w.margin), free: pts(w.free), headroom: pts(100 - w.max_sold_pct) })}</p>`
    : `<div class="meter"></div><p class="sub">${t("tk.no_report")}</p>`;
```

```js
      <div class="card"><h3>${t("tk.capacity")}${tipI("capacity_sold")}</h3>
        <div class="limit-row"><span>${t("tk.week")}${w.end ? ` · ${t("tk.week_resets", { date: fmtDate(w.end) })}` : ""}</span><span class="num">${w.fresh ? t("tk.free_to_sell", { v: pts(w.free) }) : "—"}</span></div>${weekMeter}
        <p class="sub">${t("tk.sold_week", { sold: pts(w.sold), returned: pts(w.returned) })}</p>
        ${cap.peak_week ? `<div class="limit-row" style="margin-top:8px"><span>${t("tk.peak_week", { date: fmtDate(cap.peak_week.start) })}</span><span class="num">${t("tk.of", { v: pts(cap.peak_week.sold), max: fmtShare(w.max_sold_pct) })}</span></div>${pct(cap.peak_week.sold, w.max_sold_pct)}` : ""}
        <div class="limit-row" style="margin-top:8px"><span>${t("tk.five_now")}</span><span class="num">${t("tk.of", { v: fmtPct(f.now, 1), max: fmtShare(f.max_sold_5h_pct) })}</span></div>${pct(f.now, f.max_sold_5h_pct)}
        <div class="limit-row" style="margin-top:8px"><span>${t("tk.five_peak")}</span><span class="num">${t("tk.of", { v: fmtPct(f.peak_30d, 1), max: fmtShare(f.max_sold_5h_pct) })}</span></div>${pct(f.peak_30d, f.max_sold_5h_pct)}
        ${cap.hand_set_7d_pct ? `<p class="sub" style="margin-top:12px">${t("tk.hand_set", { v: fmtShare(cap.hand_set_7d_pct) })}</p>` : ""}</div>
```

In `i18n.js`, delete `tk.sold_now`, `tk.peak` and `tk.headroom` in both languages (`grep -n` confirms nothing else uses them), replace `tk.lede`, `tip.capacity_sold`, `tip.capacity_util`, and add the keys below. The grant form's availability line already prints `preview.reason` verbatim (`app.js:1287`), so the failing check reaches the admin without new keys.

English:

```js
    "tk.lede": "Each pass takes points of Anthropic's week: share × days ÷ 7. The gateway sells only the points nobody has used or is owed.",
    "tk.week": "This week",
    "tk.week_resets": "resets {date}",
    "tk.free_to_sell": "{v} points free to sell",
    "tk.seg_legend": "Used {used} · owed to passes {owed} · margin {margin} · free {free} · headroom {headroom}",
    "tk.sold_week": "Sold this week: {sold} points, {returned} of them returned by passes that ended.",
    "tk.no_report": "No fresh usage report from Anthropic; nothing is on sale until the next one.",
    "tk.peak_week": "Fullest week ahead, from {date}",
    "tk.five_now": "5-hour shares now",
    "tk.five_peak": "5-hour peak, next 30 days",
    "tk.hand_set": "Hand-set weekly shares total {v}; they are not reserved.",
    "tip.capacity_sold": "Points of Anthropic's current week: what everyone has used (as Anthropic reports it), what running and queued passes may still draw, the safety margin, and what is free to sell. Unused points of passes that ended are free again. Later weeks are capped at <b>max_sold_pct</b>; passes running at once at <b>max_sold_5h_pct</b> of 5-hour share.",
    "tip.capacity_util": "What everyone has actually used, as Anthropic reports it. The points above max_sold_pct are what the admin and hand-limited users have.",
```

Persian:

```js
    "tk.lede": "هر بسته از امتیازهای هفته Anthropic برمی‌دارد: سهم × روزها ÷ ۷. سرویس فقط امتیازهایی را می‌فروشد که نه مصرف شده‌اند و نه به بسته‌ای بدهکار است.",
    "tk.week": "این هفته",
    "tk.week_resets": "تازه‌سازی {date}",
    "tk.free_to_sell": "{v} امتیاز آماده فروش",
    "tk.seg_legend": "مصرف‌شده {used} · بدهی به بسته‌ها {owed} · حاشیه {margin} · آزاد {free} · سهم دیگران {headroom}",
    "tk.sold_week": "فروخته‌شده این هفته: {sold} امتیاز، که {returned} امتیاز آن را بسته‌های پایان‌یافته پس داده‌اند.",
    "tk.no_report": "گزارش تازه‌ای از مصرف از Anthropic نرسیده است؛ تا گزارش بعدی چیزی فروخته نمی‌شود.",
    "tk.peak_week": "پرترین هفته پیش رو، از {date}",
    "tk.five_now": "سهم‌های ۵ ساعته اکنون",
    "tk.five_peak": "اوج ۵ ساعته، ۳۰ روز آینده",
    "tk.hand_set": "سهم‌های هفتگی دستی روی هم {v} است؛ برایشان رزرو نمی‌شود.",
    "tip.capacity_sold": "امتیازهای هفته جاری Anthropic: آنچه همه مصرف کرده‌اند (طبق گزارش Anthropic)، آنچه بسته‌های فعال و در صف هنوز می‌توانند مصرف کنند، حاشیه اطمینان و آنچه برای فروش آزاد است. امتیازهای مصرف‌نشده بسته‌های پایان‌یافته دوباره آزاد می‌شوند. هفته‌های بعد تا <b>max_sold_pct</b> و بسته‌های هم‌زمان تا <b>max_sold_5h_pct</b> از سهم ۵ ساعته محدودند.",
    "tip.capacity_util": "آنچه همه واقعاً مصرف کرده‌اند، طبق گزارش Anthropic. امتیازهای بالای max_sold_pct سهم ادمین و کاربران با سقف دستی است.",
```

- [ ] **Step 5: Run**

Run: `uv run --python 3.12 pytest -q`
Expected: all pass, none skipped by this plan.

- [ ] **Step 6: Look at it**

Start the app with the project's run skill or `uv run --python 3.12 python -m claude_proxy` against a copy of a dev database, sign in as admin, open the Passes tab in both languages, and check that the week meter, its legend, the fullest-week row, the two 5-hour rows and the hand-set note render without overflow at phone width.

- [ ] **Step 7: Mark the spec accepted and commit**

Change the spec's `Status` line to `accepted (revision 3, implemented 2026-11; see the plan of the same name for the amendments made while implementing).`

```bash
git add src tests docs
git commit -m "Capacity panel shows the week in points: used, owed, margin, free and headroom, with sold and returned, the fullest week ahead and the 5-hour sums

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** §1 terms, Tasks 2 and 5. §2 checks 1-3, Tasks 3 and 5; sold out and `unavailable`, Task 6; error texts, Task 5. §3 freshness and the reset, Task 5 tests. §4 recycling, Task 5 test 6. §5 borrowing, Task 2. §6 panel and payload, Task 7; the grant form's reason line already exists. §7 settings, Task 1. §8 layout, Tasks 2, 5, 7. §9 tests 1-18: 1-15 in Task 5 (13 through the existing borrow tests `test_a_pass_queued_to_start_later_this_week_is_owed` and `test_other_passes_unused_parts_are_held_back`, which now run through the shared `room`), 16 through Task 6's order test and the migrated `sold_out_later` tests, 17 in Task 1, 18 in Task 7.
- **Deviations recorded in the spec by Task 2:** check 1 judges what a change adds; borrowing keeps a ceiling of 100; a used-up pass's share bonuses lose their later-week pieces; `max_sold_5h_pct` is capped at 1000; tests 2, 3 and 14 carry corrected numbers and wording.
- **Type consistency:** `exclude` is a tuple or list of ids everywhere in `capacity`; `tickets.check_capacity` keeps `None` for no exclusion. `Context.room` is unfloored; `summary` floors it. `room()` is the only place the subtraction is written for borrowing; `context` writes it once more with the sell ceiling and margin, on purpose, so the dataclass carries the unfloored value.
- **Chronology:** every test inserts snapshots in time order; `report()` asserts it, so a reordered test fails at the helper, not in an attribution number.
