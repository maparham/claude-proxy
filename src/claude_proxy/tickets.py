"""Paid tickets (design 2026-10-03): reserved slices of the account sold by the day, week or month.

Prices, discounts and exchange rates (spec section 6); capacity reserved in time (section 5); grants, cancels,
bonuses and the Ungate action (sections 5, 7, 8). Every change to reservations runs in one BEGIN IMMEDIATE
transaction, so two admins acting at once cannot oversell.
"""
from __future__ import annotations

import html
import re
import math
import sqlite3
import time
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from . import db, plans, quota
from .config import LENGTHS, Config
from .forwarder import COUNT_TOKENS_PATH

DAY = 86400

STALE_RATE_S = 36 * 3600
USD_ROUND_TO = 0.01
MAX_USD = 100_000          # generous ceilings: anything above is a typo, not a price
MAX_RATE = 1e9             # local units per 1 USD; leaves room for currencies counted in the millions
NOTE_MAX = 200             # the dashboard's maxlength

TERMS_VERSION = "v1"
# Ticket terms, accepted at order time and repeated in the buyer's emails (order requests design, section 5).
# Plain text: it goes into emails as is. Names no weekday: Anthropic's reset comes from live quota data, so the
# refill moment is formatted from resets_at at display time (2026-10-13 spec, §1).
# The order dialog's terms (i18n.js ord.split*, ord.terms_l1, ord.terms_l3), repeated in the buyer's emails in the
# language of the page they ordered from. Dates in Tehran time: the dialog shows the buyer's own clock.
TERMS = {
    "en": {"head": "Pass terms:",
           "split": "Your allowance renews on a fixed weekly schedule, so your pass is used in {n} parts: {pieces}. "
                    "Whatever is left of a part when the next one starts is lost.",
           "split_one": "Your whole pass is usable until {date}.", "open": "Your whole pass is usable until it ends.",
           "sep": ", ", "and": " and ",
           "nums": {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}, "week": "{n} part: {pct}",
           "halves": ("First part: {pct}", "Second part: {pct}"),
           "nth": ("First", "Second", "Third", "Fourth", "Fifth"),
           "l1": "Your pass is usable at your own pace until it ends. Your allowance renews on a fixed weekly schedule, so a "
                 "pass that runs past it is used in parts; whatever is left of a part when the next one starts is lost.",
           "l3": "You can use up to a set amount in every 5 hours.", "tz": "Times are Tehran time."},
    "fa": {"head": "شرایط بسته:",
           "split": "سهمیه شما طبق یک برنامه هفتگی ثابت تازه می‌شود، برای همین بسته شما در {n} قسمت قابل استفاده است: {pieces}. "
                    "هر چه از یک قسمت تا شروع قسمت بعد مصرف نشود از بین می‌رود.",
           "split_one": "کل بسته شما تا {date} قابل استفاده است.", "open": "کل بسته شما تا پایانش قابل استفاده است.",
           "sep": "، ", "and": " و ",
           "nums": {2: "دو", 3: "سه", 4: "چهار", 5: "پنج", 6: "شش"}, "week": "قسمت {n}: {pct}",
           "halves": ("قسمت اول: {pct}", "قسمت دوم: {pct}"),
           "nth": ("اول", "دوم", "سوم", "چهارم", "پنجم"),
           "l1": "بسته‌تان را تا پایانش با سرعت خودتان مصرف می‌کنید. سهمیه شما طبق یک برنامه هفتگی ثابت تازه می‌شود، برای همین "
                 "بسته‌ای که از آن زمان بگذرد در چند قسمت مصرف می‌شود؛ هر چه از یک قسمت تا شروع قسمت بعد مصرف نشود از بین می‌رود.",
           "l3": "در هر ۵ ساعت تا سقف مشخصی می‌توانید مصرف کنید.", "tz": "ساعت‌ها به وقت تهران است."},
}
TEHRAN = ZoneInfo("Asia/Tehran")
FA_DIGITS = str.maketrans("0123456789,.", "۰۱۲۳۴۵۶۷۸۹٬٫")
FA_MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")
FA_WEEKDAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")   # from Monday, as date.weekday()


def fa_num(s) -> str:
    return str(s).translate(FA_DIGITS)


def jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Gregorian to the Solar Hijri calendar, what fa-IR dates use (jdf.scr.ir's conversion)."""
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd
            + (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def terms_date(ts: float, lang: str) -> str:
    """As the dialog shows a date (weekday, month, day, hour:minute), in Tehran time."""
    from datetime import datetime
    t = datetime.fromtimestamp(ts, TEHRAN)
    if lang == "fa":
        _, jm, jd = jalali(t.year, t.month, t.day)
        return f"{FA_WEEKDAYS[t.weekday()]} {fa_num(jd)} {FA_MONTHS[jm - 1]}، ساعت {fa_num(t.strftime('%H:%M'))}"
    return t.strftime("%A, %B ") + str(t.day) + t.strftime(" at %I:%M %p")


SPLIT_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4")   # app.css --s1..--s5, light: a month spans up to 5 weeks


def _terms(conn, cfg: Config, lang: str, length: str, starts_at: float | None, now: float | None, rich: bool,
           account: str | None) -> str:
    """`starts_at` or `account` None: not known yet (an order waiting for its grant), so nothing dated, no split."""
    w = TERMS["fa" if lang == "fa" else "en"]
    ws = order_split(conn, cfg, starts_at, now, account=account) if starts_at is not None and account is not None else None
    parts, days = (ws or {}).get(length) or [], LENGTHS[length]
    esc = html.escape if rich else str

    def pct(v):
        s = "<1%" if 0 < v < 0.01 else f"{round(v * 100)}%"
        return fa_num(s).replace("%", "٪") if lang == "fa" else s

    bar = ""
    if not parts:   # no quota data, or no start yet: a day pass is one allowance whatever it crosses (2026-10-30)
        split = esc(w["open"] if length == "day" else w["l1"])
    elif len(parts) == 1:
        split = esc(w["split_one"].format(date=terms_date(starts_at + days * DAY, lang)))
    else:
        def name(v, i):   # parts, never weeks: the buyer bought a length, not Anthropic's weeks
            return esc(w["week"].format(n=w["nth"][i] if i < len(w["nth"]) else i + 1, pct=pct(v)))

        pieces = [name(v, i) for i, v in enumerate(parts)]   # never a weekly reset's date: that is the account's
        n = w["nums"].get(len(parts)) or (fa_num(len(parts)) if lang == "fa" else str(len(parts)))
        split = esc(w["split"]).replace("{n}", esc(n)).replace(
            "{pieces}", esc(w["sep"]).join(pieces[:-1]) + esc(w["and"]) + pieces[-1])
        def label(v, i):   # as the dialog's: week and percent, a narrow part's percent only, a sliver's nothing
            if v >= 0.2:
                return name(v, i)
            return esc(pct(v)) if v >= 0.1 else "&nbsp;"

        if rich:   # a table, which mail clients lay out: each part as wide as its share, 2px apart, its label above it
            gap = '<td width="2" style="font-size:0;line-height:0">&nbsp;</td>'
            widths = [f"{max(v * 100, 1):.1f}%" for v in parts]
            labels = gap.join(f'<td width="{wd}" style="font-size:12px;font-weight:bold;line-height:16px;padding-bottom:2px;text-align:center;'
                              f'white-space:nowrap">{label(v, i)}</td>'
                              for i, (wd, v) in enumerate(zip(widths, parts)))
            cells = gap.join(f'<td width="{wd}" style="height:10px;font-size:0;line-height:0;background:'
                             f'{SPLIT_COLORS[i % len(SPLIT_COLORS)]};border-radius:2px">&nbsp;</td>' for i, wd in enumerate(widths))
            bar = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                   f'style="max-width:420px;margin:8px 0 4px;border-collapse:collapse"><tr>{labels}</tr><tr>{cells}</tr></table>')
    lines = [esc(w["head"]), f"- {split}" + bar, f"- {esc(w['l3'])}"] + ([esc(w["tz"])] if len(parts) == 1 else [])   # only the one-part line names a date
    return "\n".join(lines).replace("</table>\n", "</table>") + "\n"   # the table already breaks the line


def terms_text(conn, cfg: Config, lang: str, length: str, starts_at: float | None, now: float | None = None,
               account: str | None = None) -> str:
    """The order dialog's terms for a `length` ticket starting at `starts_at`, as one block for the buyer's emails:
    how it splits across Anthropic's weeks (each piece's share), else the general line without
    quota data; then the 5-hour line. `account`: the pass's, whose weeks it splits across."""
    return _terms(conn, cfg, lang, length, starts_at, now, rich=False, account=account)


def terms_html(conn, cfg: Config, lang: str, length: str, starts_at: float, now: float | None = None,
               account: str | None = None) -> str:
    """terms_text as HTML for the email's HTML copy, with the split drawn under its sentence: a bar, each part's
    percent on it."""
    return _terms(conn, cfg, lang, length, starts_at, now, rich=True, account=account)


def mail_html(conn, cfg: Config, lang: str, length: str, starts_at: float | None, body: str, account: str | None = None) -> str:
    """A buyer email's HTML copy: the plain `body`, its terms block (for that ticket) swapped for the drawn one, right
    to left for Persian."""
    lang = "fa" if lang == "fa" else "en"
    d = "rtl" if lang == "fa" else "ltr"
    terms = terms_text(conn, cfg, lang, length, starts_at, account=account)
    rich = terms_html(conn, cfg, lang, length, starts_at, account=account)
    inner = html.escape(body).replace(html.escape(terms), rich, 1)
    inner = re.sub(r"https?://[^\s<\"]+/blog/install-claude-code-windows-mac(?![\w-])", lambda m: f'<a href="{m[0]}">{m[0]}</a>', inner)   # orders.setup_guide's link
    return f'<div dir="{d}" style="text-align:{"right" if d == "rtl" else "left"};white-space:pre-wrap">{inner}</div>\n'


TERMS_CHANGED = "Pass terms have changed; reload and accept the current terms."


def _check_terms(terms_version) -> str:
    """The equality check every order and direct grant goes through (2026-10-13 spec, §3)."""
    if terms_version != TERMS_VERSION:
        raise TicketError(TERMS_CHANGED)
    return terms_version


def week_bounds(conn, cfg: Config, now: float, *, account: str) -> tuple[float, float] | None:
    """The account's current Anthropic weekly window from its 7d snapshots' reset time (2026-10-13 spec, §1.3).

    Recomputed from the latest attribution on every call — projections are never stored, and a fresh
    snapshot's `resets_at` always wins. Keys on `resets_at` vs `now` only: a stale snapshot with a future
    reset still gives known boundaries. `resets_at` passed rolls forward in 7-day steps (assumed fixed
    cadence); no reset time ever reported → None (the caller refuses 503 NO_RATE)."""
    att = quota.attribution_cached(conn, cfg.pricing, "7d", now=now, stale_after_s=cfg.quota.stale_after_s, account=account)
    resets = att["resets_at"]
    if resets is None:
        return None
    if resets > now:
        return resets - 7 * DAY, resets
    steps = int((now - resets) // (7 * DAY)) + 1
    end = resets + steps * 7 * DAY
    return end - 7 * DAY, end


def week_split(conn, cfg: Config, share_pct: float, starts_at: int, ends_at: int, now: float | None = None, *,
               account: str) -> list[float] | None:
    """The §1.1 piece caps for `[starts_at, ends_at)` in every Anthropic week it touches, full precision.

    Future week boundaries project forward from the current `resets_at` in 7-day steps (assumed cadence —
    informational; enforcement always uses observed resets). None when no `resets_at` is known at all."""
    now = _now(now)
    att = quota.attribution_cached(conn, cfg.pricing, "7d", now=now, stale_after_s=cfg.quota.stale_after_s, account=account)
    resets = att["resets_at"]
    if resets is None:
        return None
    end = resets
    if end <= now:
        end += (int((now - end) // (7 * DAY)) + 1) * 7 * DAY
    start = end - 7 * DAY
    while end <= starts_at:
        end += 7 * DAY
        start += 7 * DAY
    while start > starts_at:
        end -= 7 * DAY
        start -= 7 * DAY
    out = []
    while start < ends_at:
        overlap = max(0, min(start + 7 * DAY, ends_at) - max(start, starts_at))
        if overlap > 0:
            out.append(share_pct * overlap / DAY / 7)
        start += 7 * DAY
    return out



def order_split(conn, cfg: Config, starts_at: float, now: float | None = None, *, account: str) -> dict | None:
    """What a buyer is told before ordering: per length, the fraction of the ticket each Anthropic week holds if it
    starts at `starts_at`, and `resets`: the weekly resets between its parts, after `starts_at` and within the longest
    length, so the terms can show when a part ends (asked for 2026-11-03). Informational, like week_split: boundaries
    project forward in 7-day steps. None without a known weekly reset."""
    now = _now(now)
    if (wb := week_bounds(conn, cfg, now, account=account)) is None:
        return None
    first = wb[1] + max(0, -(-(starts_at - wb[1]) // (7 * DAY))) * 7 * DAY   # the first reset at or after the start
    out = {"starts_at": starts_at, "resets": [first + k * 7 * DAY for k in range(6) if first + k * 7 * DAY < starts_at + max(LENGTHS.values()) * DAY]}
    for length, days in LENGTHS.items():
        parts = week_split(conn, cfg, 7 / days, int(starts_at), int(starts_at) + days * DAY, now, account=account) or []
        if length == "day" and parts:
            parts = [1.0]   # it borrows its next part as it needs it (2026-10-30): one allowance, whatever reset it crosses
        out[length] = parts   # share 7/days per week makes the pieces fractions of the whole ticket
    return out

class TicketError(Exception):
    """A refused ticket operation. The message is written for the admin who asked."""


class CapacityError(TicketError):
    """The period would push what is sold over max_sold_pct."""


class FiveHourFull(CapacityError):
    """Anthropic's current 5-hour window lacks the pass's 5-hour share; it fits again at `resets_at`."""

    def __init__(self, message: str, resets_at: float):
        super().__init__(message)
        self.resets_at = resets_at


class WeekFull(CapacityError):
    """This week's room (what is left of the weekly limit after reserved points and the margin) lacks the pass's
    points; the admin may grant anyway (over_week), later weeks' max_sold_pct still applying."""


class SubscriptionEnds(CapacityError):
    """The pass would run past the subscription's end (capacity.check_sub_end); buyers are told only that it is back
    on sale at `resets_at`, never about the subscription."""

    def __init__(self, message: str, resets_at: float):
        super().__init__(message)
        self.resets_at = resets_at


class QuoteChanged(TicketError):
    """The price or rate moved between the grant form's preview and the grant."""


def _date(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def _now(now) -> int:
    return int(time.time() if now is None else now)


def _positive(v, what: str, ceiling: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise TicketError(f"{what} must be a number above 0.")
    if v >= ceiling:
        raise TicketError(f"{what} must be below {ceiling:,.0f}.")
    return float(v)


def _usd(v, what: str) -> float:
    """A USD amount, rounded to cents."""
    usd = round(_positive(v, what, MAX_USD), 2)
    if usd <= 0:
        raise TicketError(f"{what} must be at least $0.01.")
    return usd


def _note(note) -> str | None:
    note = (note or "").strip()
    if len(note) > NOTE_MAX:
        raise TicketError(f"A note is at most {NOTE_MAX} characters.")
    return note or None


# ---------- money (spec section 6) ----------

def round_local(usd: float, rate: float, round_to: float, vat_pct: float = 0) -> float:
    """round(usd × rate (+ VAT), round_to), ties rounded up: $20 at 0.92 with a 0.50 step is €18.50, and so is 18.25."""
    step = Decimal(str(round_to))
    vat = 1 + Decimal(str(vat_pct)) / 100
    steps = (Decimal(str(usd)) * Decimal(str(rate)) * vat / step).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(steps * step)


def vat_percent(cfg: Config) -> float:
    """[zarinpal] vat_percent, added on top of every price in every currency: what is shown, quoted and charged includes it."""
    return cfg.zarinpal.vat_percent if cfg.zarinpal else 0


def currencies(cfg: Config) -> dict[str, float]:
    """Currency code -> rounding step. USD is always there, at an implicit rate of 1."""
    return {"USD": USD_ROUND_TO, **{c: cur.round_to for c, cur in cfg.tickets.currencies.items()}}


def current_rate(conn: sqlite3.Connection, currency: str) -> dict | None:
    """The newest rate for a currency (local units per 1 USD), USD's built-in one, or None when none was ever set."""
    if currency == "USD":
        return {"currency": "USD", "rate": 1.0, "set_at": None, "set_by": None}
    row = conn.execute("SELECT currency, rate, set_at, set_by FROM fx_rates WHERE currency=? ORDER BY set_at DESC, id DESC LIMIT 1",
                       (currency,)).fetchone()
    return dict(row) if row else None


def rate_is_stale(rate: dict, now: float) -> bool:
    return rate["set_at"] is not None and now - rate["set_at"] > STALE_RATE_S


def set_rate(conn: sqlite3.Connection, cfg: Config, currency: str, rate, actor: int | None, now: float | None = None) -> dict:
    if currency not in cfg.tickets.currencies:
        raise TicketError(f"{currency!r} is not a configured currency; add [tickets.currencies.{currency}] to the config first.")
    rate = _positive(rate, "The rate (local units per 1 USD)", MAX_RATE)
    conn.execute("INSERT INTO fx_rates(currency, rate, set_at, set_by) VALUES(?,?,?,?)", (currency, rate, _now(now), actor))
    db.audit(conn, actor, "rate_set", currency, {"rate": rate})
    return current_rate(conn, currency)


# ---------- prices and discounts ----------

def _check_tier_length(cfg: Config, tier: str, length: str) -> None:
    if tier not in cfg.tickets.tiers:
        raise TicketError(f"Unknown tier {tier!r}.")
    if length not in LENGTHS:
        raise TicketError(f"Unknown length {length!r}; one of {', '.join(LENGTHS)}.")


def regular_price(conn: sqlite3.Connection, tier: str, length: str) -> float:
    row = conn.execute("SELECT usd FROM ticket_prices WHERE tier=? AND length=?", (tier, length)).fetchone()
    if row is None:
        raise TicketError(f"{tier} {length} has no price, so it is not on sale.")
    return row[0]


def set_price(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, usd, actor: int | None, now: float | None = None) -> None:
    """Set a regular price; None takes the tier and length off sale (the pages leave it out, orders are refused)."""
    _check_tier_length(cfg, tier, length)
    if usd is None:
        conn.execute("DELETE FROM ticket_prices WHERE tier=? AND length=?", (tier, length))
        db.audit(conn, actor, "price_cleared", f"{tier}:{length}")
        return
    usd = _usd(usd, "The price")
    conn.execute("INSERT OR REPLACE INTO ticket_prices(tier, length, usd, updated_at, updated_by) VALUES(?,?,?,?,?)",
                 (tier, length, usd, _now(now), actor))
    db.audit(conn, actor, "price_set", f"{tier}:{length}", {"usd": usd})


def prices(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_prices ORDER BY tier, length")]


def active_discount(conn: sqlite3.Connection, tier: str, length: str, now: float) -> dict | None:
    """The discount covering `now`: not cancelled and starts_at <= now < ends_at."""
    row = conn.execute("SELECT * FROM ticket_discounts WHERE tier=? AND length=? AND cancelled_at IS NULL AND starts_at<=? AND ends_at>? "
                       "ORDER BY id LIMIT 1", (tier, length, now, now)).fetchone()
    return dict(row) if row else None


def create_discount(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, usd, starts_at, ends_at, actor: int | None,
                    now: float | None = None, pct=None) -> dict:
    """A sale: either a fixed discounted price (`usd`), or with `pct` a percentage off whatever the regular price is
    while it runs (`usd` is then ignored and stored as the price it gives at creation, for the record)."""
    now = _now(now)
    _check_tier_length(cfg, tier, length)
    regular = regular_price(conn, tier, length)
    if pct is not None:
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct) or not 0 < pct < 100:
            raise TicketError("A percentage discount must be above 0 and below 100.")
        pct = round(float(pct), 2)
        usd = _pct_price(regular, pct)
    else:
        usd = _usd(usd, "The discounted price")
    starts_at, ends_at = int(starts_at), int(ends_at)
    if starts_at >= ends_at:
        raise TicketError("A discount must start before it ends.")
    if ends_at <= now:
        raise TicketError("A discount must end in the future.")
    if usd >= regular:
        raise TicketError(f"A discount must be below the regular price (${regular:g}).")
    # Interval overlap against every non-cancelled discount of the tier and length, not a check at `now`.
    clash = conn.execute("SELECT id FROM ticket_discounts WHERE tier=? AND length=? AND cancelled_at IS NULL AND starts_at<? AND ends_at>? "
                         "ORDER BY id LIMIT 1", (tier, length, ends_at, starts_at)).fetchone()
    if clash:
        raise TicketError(f"Discount #{clash[0]} already covers part of that period; cancel it first.")
    cur = conn.execute("INSERT INTO ticket_discounts(tier, length, usd, pct, starts_at, ends_at, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (tier, length, usd, pct, starts_at, ends_at, actor, now))
    db.audit(conn, actor, "discount_create", f"{tier}:{length}",
             {"id": cur.lastrowid, "usd": usd, "pct": pct, "starts_at": starts_at, "ends_at": ends_at})
    return dict(conn.execute("SELECT * FROM ticket_discounts WHERE id=?", (cur.lastrowid,)).fetchone())


def _pct_price(list_usd: float, pct: float) -> float:
    """`list_usd` less `pct` percent, to the cent and never below $0.01."""
    return max(0.01, round(list_usd * (1 - pct / 100), 2))


def cancel_discount(conn: sqlite3.Connection, discount_id: int, actor: int | None, now: float | None = None) -> None:
    row = conn.execute("SELECT tier, length FROM ticket_discounts WHERE id=? AND cancelled_at IS NULL", (discount_id,)).fetchone()
    if row is None:
        raise TicketError("No such active discount.")
    conn.execute("UPDATE ticket_discounts SET cancelled_at=? WHERE id=?", (_now(now), discount_id))
    db.audit(conn, actor, "discount_cancel", f"{row['tier']}:{row['length']}", {"id": discount_id})


def discounts(conn: sqlite3.Connection, now: float, include_ended: bool = False) -> list[dict]:
    """Non-cancelled discounts, current and future; with `include_ended`, everything, newest first."""
    if include_ended:
        return [dict(r) for r in conn.execute("SELECT * FROM ticket_discounts ORDER BY starts_at DESC, id DESC")]
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_discounts WHERE cancelled_at IS NULL AND ends_at>? ORDER BY starts_at, id", (now,))]


def price_now(conn: sqlite3.Connection, tier: str, length: str, now: float) -> dict:
    """The regular price, what is charged now, and the discount when it is the lower one. A regular price since
    lowered to or below an active discount is charged as is, and no strike-through is shown."""
    list_usd = regular_price(conn, tier, length)
    d = active_discount(conn, tier, length, now)
    if d and d.get("pct") is not None:   # a percentage follows the regular price as it is now
        d["usd"] = _pct_price(list_usd, d["pct"])
    if d and d["usd"] < list_usd:
        return {"list_usd": list_usd, "usd": d["usd"], "discount_id": d["id"], "discount_ends_at": d["ends_at"]}
    return {"list_usd": list_usd, "usd": list_usd, "discount_id": None, "discount_ends_at": None}


def seed_prices(conn: sqlite3.Connection, cfg: Config, now: float | None = None) -> int:
    """Insert each tier's default_usd for every length that has no price yet, once per tier: a price the dashboard
    cleared later stays cleared. Returns the rows added."""
    now = int(time.time() if now is None else now)
    n = 0
    for tier, t in cfg.tickets.tiers.items():
        key = f"ticket_prices_seeded:{tier}"
        if conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone():
            continue
        for length in LENGTHS:
            n += conn.execute("INSERT OR IGNORE INTO ticket_prices(tier, length, usd, updated_at, updated_by) VALUES(?,?,?,?,NULL)",
                              (tier, length, float(t.default_usd[length]), now)).rowcount
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, str(now)))
    return n


def seed_sizes(conn: sqlite3.Connection, cfg: Config, now: float | None = None) -> int:
    """Insert each tier's config size that has no DB row yet. Returns the rows added. The 5-hour size is left NULL,
    so the config's keeps applying until the dashboard sets one. share_pct is a legacy NOT NULL column, never read."""
    now = int(time.time() if now is None else now)
    n = 0
    for tier, t in cfg.tickets.tiers.items():
        n += conn.execute("INSERT OR IGNORE INTO ticket_tier_shares(tier, share_pct, updated_at, updated_by, size) VALUES(?,0,?,NULL,?)",
                          (tier, now, float(t.size))).rowcount
    return n


def _size_row(conn: sqlite3.Connection, tier: str):
    return conn.execute("SELECT size, size_5h FROM ticket_tier_shares WHERE tier=?", (tier,)).fetchone()


def tier_size(conn: sqlite3.Connection, cfg: Config, tier: str) -> float | None:
    """The tier's weekly size in Claude Pro weeks: the dashboard's, else the config's."""
    row = _size_row(conn, tier)
    if row is not None and row[0] is not None:
        return float(row[0])
    t = cfg.tickets.tiers.get(tier)
    return float(t.size) if t is not None else None


def tier_size_5h(conn: sqlite3.Connection, cfg: Config, tier: str) -> float | None:
    """The tier's 5-hour size in Claude Pro weeks: the dashboard's, else the config's, else the weekly size."""
    row = _size_row(conn, tier)
    if row is not None and row[1] is not None:
        return float(row[1])
    t = cfg.tickets.tiers.get(tier)
    return float(t.size_5h) if t is not None and t.size_5h is not None else tier_size(conn, cfg, tier)


def effective_share(conn: sqlite3.Connection, cfg: Config, tier: str, account: str) -> float | None:
    """The weekly share new tickets of this tier are sold with on `account`; None when the size is unset or the
    account has no plan."""
    size = tier_size(conn, cfg, tier)
    sh = plans.shares(conn, account, size, tier_size_5h(conn, cfg, tier)) if size is not None else None
    return sh[0] if sh else None


def effective_share_5h(conn: sqlite3.Connection, cfg: Config, tier: str, account: str) -> float | None:
    """The 5-hour share new tickets of this tier are sold with on `account`; None as effective_share."""
    size = tier_size(conn, cfg, tier)
    sh = plans.shares(conn, account, size, tier_size_5h(conn, cfg, tier)) if size is not None else None
    return sh[1] if sh else None


PLAN_UNKNOWN = "This subscription's plan is unknown; pick it on the Subscriptions page."


def _sell_shares(conn: sqlite3.Connection, cfg: Config, tier: str, account: str) -> tuple[float, float]:
    """(weekly, 5-hour) shares of a new ticket of this tier on `account`; CapacityError when its plan is unknown."""
    share, share_5h = effective_share(conn, cfg, tier, account), effective_share_5h(conn, cfg, tier, account)
    if share is None or share_5h is None:
        raise CapacityError(PLAN_UNKNOWN)
    return share, share_5h


def effective_tiers(conn: sqlite3.Connection, cfg: Config) -> dict[str, dict]:
    """{tier: {label, size, size_5h}} with dashboard sizes applied, in Claude Pro weeks, for new grants and display."""
    return {tid: {"label": t.label, "size": tier_size(conn, cfg, tid), "size_5h": tier_size_5h(conn, cfg, tid)}
            for tid, t in cfg.tickets.tiers.items()}


def _check_size(cfg: Config, tier: str, size, what: str) -> float:
    if tier not in cfg.tickets.tiers:
        raise TicketError(f"Unknown tier {tier!r}.")
    if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size):
        raise TicketError(f"The {what} must be a number.")
    size = float(size)
    if not 0 < size <= 1000:
        raise TicketError(f"The {what} must be above 0 and at most 1000 Claude Pro weeks.")
    return size


def set_size(conn: sqlite3.Connection, cfg: Config, tier: str, size, actor: int | None, now: float | None = None) -> float:
    """Change a tier's weekly size, in Claude Pro weeks, for future tickets. Existing tickets keep the share they were
    sold with. Refused when it fits no plan: over max_sold_pct even on the largest, it could never be sold."""
    size = _check_size(cfg, tier, size, "size")
    f7 = max(f["7d"] for f in plans.factors(conn).values())
    if size * 100 / f7 > cfg.tickets.max_sold_pct + 1e-9:
        raise TicketError(f"The size must be at most {cfg.tickets.max_sold_pct * f7 / 100:g} Claude Pro weeks "
                          "(max_sold_pct of the largest plan), or the tier could never be sold.")
    now = _now(now)
    conn.execute("INSERT INTO ticket_tier_shares(tier, share_pct, updated_at, updated_by, size) VALUES(?,0,?,?,?) "
                 "ON CONFLICT(tier) DO UPDATE SET size=excluded.size, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                 (tier, now, actor, size))
    db.audit(conn, actor, "size_set", tier, {"size": size})
    return size


def set_size_5h(conn: sqlite3.Connection, cfg: Config, tier: str, size, actor: int | None, now: float | None = None) -> float:
    """Change a tier's 5-hour size for future tickets. Refused when it fits no plan: over max_sold_5h_pct even on the
    largest, passes running at once could never hold it."""
    size = _check_size(cfg, tier, size, "5-hour size")
    f5 = max(f["5h"] for f in plans.factors(conn).values())
    if size * 100 / f5 > cfg.tickets.max_sold_5h_pct + 1e-9:
        raise TicketError(f"The 5-hour size must be at most {cfg.tickets.max_sold_5h_pct * f5 / 100:g} Claude Pro weeks "
                          "(max_sold_5h_pct of the largest plan), or the tier could never be sold.")
    now = _now(now)
    conn.execute("INSERT INTO ticket_tier_shares(tier, share_pct, updated_at, updated_by, size, size_5h) VALUES(?,0,?,?,?,?) "
                 "ON CONFLICT(tier) DO UPDATE SET size_5h=excluded.size_5h, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                 (tier, now, actor, tier_size(conn, cfg, tier), size))
    db.audit(conn, actor, "size_5h_set", tier, {"size_5h": size})
    return size


RECOMMENDED_KEY = "pricing_recommended"


def recommended(conn: sqlite3.Connection, cfg: Config) -> dict | None:
    """{tier, length} the pricing page puts forward, as the dashboard set it; None when unset or no longer offered."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (RECOMMENDED_KEY,)).fetchone()
    tier, _, length = (row[0] if row else "").partition(":")
    return {"tier": tier, "length": length} if tier in cfg.tickets.tiers and length in LENGTHS else None


def set_recommended(conn: sqlite3.Connection, cfg: Config, tier, length, actor: int | None) -> dict | None:
    """Put one tier and length forward on the pricing page; an empty tier clears it. Display only."""
    if tier in ("", None):
        conn.execute("DELETE FROM settings WHERE key=?", (RECOMMENDED_KEY,))
        db.audit(conn, actor, "recommended_set", "", {"tier": None, "length": None})
        return None
    _check_tier_length(cfg, tier, length)
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (RECOMMENDED_KEY, f"{tier}:{length}"))
    db.audit(conn, actor, "recommended_set", f"{tier}:{length}", {"tier": tier, "length": length})
    return {"tier": tier, "length": length}


SUB_END_KEY = "subscription_ends_at:{}"   # per account slug


def subscription_end_set(conn: sqlite3.Connection, account: str) -> int | None:
    """The end of the account's current Anthropic subscription as the admin set it; None when unset."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SUB_END_KEY.format(account),)).fetchone()
    try:
        return int(row[0]) if row else None
    except ValueError:
        return None


def subscription_end(conn: sqlite3.Connection, week: tuple[float, float] | None, now: float, account: str) -> float | None:
    """When every pass must have ended: the admin's subscription end, or, while it is unset or already past, the
    current weekly reset (week[1]), as the subscription is only sure to run until then. None without either."""
    end = subscription_end_set(conn, account)
    if end is not None and end > now:
        return float(end)
    return week[1] if week else None


def set_subscription_end(conn: sqlite3.Connection, ends_at: int | None, actor: int | None, account: str,
                         now: float | None = None) -> int | None:
    """Set the account's subscription end (UTC seconds), which must be in the future; None clears it."""
    if ends_at is None:
        conn.execute("DELETE FROM settings WHERE key=?", (SUB_END_KEY.format(account),))
    else:
        if ends_at <= _now(now):
            raise TicketError("The subscription's end must be in the future.")
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (SUB_END_KEY.format(account), str(int(ends_at))))
    db.audit(conn, actor, "subscription_end_set", account, {"ends_at": ends_at})
    return ends_at


# ---------- capacity (spec section 5) ----------

TICKET_COLS = f"t.*, {db.TICKET_EFFECTIVE_END} AS effective_end"


def _exclude(exclude) -> tuple[str, list]:
    """One ticket id, or several (the used-up tickets a new one replaces)."""
    ids = [] if exclude is None else [exclude] if isinstance(exclude, int) else list(exclude)
    return (f" AND t.id NOT IN ({','.join('?' * len(ids))})", ids) if ids else ("", [])


def sold_at(conn: sqlite3.Connection, account: str, t: float, exclude=None) -> float:
    """sold(t) for the 5-hour bucket (2026-11-04 spec, §2 check 3): 5-hour shares of the non-cancelled tickets and
    bonus shares covering `t` (half-open periods), each falling back to its weekly share. `exclude` leaves those
    tickets and their bonuses out."""
    ex, args = _exclude(exclude)
    a = conn.execute(f"SELECT COALESCE(SUM(COALESCE(t.share_5h_pct, t.share_pct)), 0) FROM tickets t WHERE t.account=? AND t.cancelled_at IS NULL "
                     f"AND t.starts_at<=? AND {db.TICKET_EFFECTIVE_END}>?{ex}", (account, t, t, *args)).fetchone()[0]
    b = conn.execute(f"SELECT COALESCE(SUM(COALESCE(b.share_5h_pct, b.share_pct)), 0) FROM ticket_bonuses b JOIN tickets t ON t.id=b.ticket_id "
                     f"WHERE t.account=? AND t.cancelled_at IS NULL AND b.cancelled_at IS NULL AND b.starts_at<=? AND b.ends_at>?{ex}",
                     (account, t, t, *args)).fetchone()[0]
    return a + b


def _starts_within(conn, account: str, starts_at: int, ends_at: int, exclude=None) -> set[int]:
    """Every ticket or bonus start strictly inside (starts_at, ends_at): the only places sold(t) can rise."""
    ex, args = _exclude(exclude)
    pts = {r[0] for r in conn.execute(f"SELECT t.starts_at FROM tickets t WHERE t.account=? AND t.cancelled_at IS NULL "
                                      f"AND t.starts_at>? AND t.starts_at<?{ex}", (account, starts_at, ends_at, *args))}
    pts |= {r[0] for r in conn.execute(f"SELECT b.starts_at FROM ticket_bonuses b JOIN tickets t ON t.id=b.ticket_id WHERE t.account=? "
                                       f"AND t.cancelled_at IS NULL AND b.cancelled_at IS NULL AND b.share_pct>0 AND b.starts_at>? AND b.starts_at<?{ex}",
                                       (account, starts_at, ends_at, *args))}
    return pts


def check_capacity(conn: sqlite3.Connection, cfg: Config, account: str, share_5h: float, starts_at: int, ends_at: int,
                   exclude=None) -> None:
    """The 5-hour check (2026-11-04 spec, §2 check 3): sold(t) + share_5h <= max_sold_5h_pct at the period's start and
    at every start inside it, else CapacityError."""
    ceiling = cfg.tickets.max_sold_5h_pct
    for point in sorted({starts_at, *_starts_within(conn, account, starts_at, ends_at, exclude)}):
        sold = sold_at(conn, account, point, exclude)
        if sold + share_5h > ceiling + 1e-9:
            raise CapacityError(f"5-hour shares would reach {sold + share_5h:g}% at {_date(point)}, over max_sold_5h_pct "
                                f"({ceiling:g}%).")


def verify_capacity(conn: sqlite3.Connection, cfg: Config, account: str, starts_at: int, ends_at: int) -> None:
    """The 5-hour check over rows already written, for changes of several pieces; call it inside the transaction or
    savepoint, so CapacityError undoes them."""
    ceiling = cfg.tickets.max_sold_5h_pct
    for point in sorted({starts_at, *_starts_within(conn, account, starts_at, ends_at)}):
        sold = sold_at(conn, account, point)
        if sold > ceiling + 1e-9:
            raise CapacityError(f"5-hour shares would reach {sold:g}% at {_date(point)}, over max_sold_5h_pct ({ceiling:g}%).")


# ---------- tickets ----------

def _row(conn, sql: str, args: tuple) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def get(conn: sqlite3.Connection, ticket_id: int) -> dict:
    t = _row(conn, f"SELECT {TICKET_COLS} FROM tickets t WHERE t.id=?", (ticket_id,))
    if t is None:
        raise TicketError(f"No pass #{ticket_id}.")
    return t


def user_tickets(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? ORDER BY t.starts_at, t.id", (user_id,))]


def last_end(conn: sqlite3.Connection, user_id: int) -> int | None:
    return conn.execute(f"SELECT MAX({db.TICKET_EFFECTIVE_END}) FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL", (user_id,)).fetchone()[0]


def next_start(conn: sqlite3.Connection, cfg: Config, user_id: int, now: float, start_now: bool = False) -> tuple[int, list[dict]]:
    """When a new ticket for the user starts, and the running tickets it replaces. `start_now`: now, alongside the
    running ones (their limits add up), or in their place when they are used up (limits.used_up), so their share no
    longer counts against capacity. Else after the user's last ticket, or now in place of running tickets that are
    used up with nothing queued after them."""
    from . import limits   # here, not at the top: limits imports tickets
    now, last = _now(now), last_end(conn, user_id)
    run = running(conn, user_id, now)
    if start_now:
        return now, run if run and limits.used_up(conn, cfg, user_id, now, run) else []
    if run and max(t["effective_end"] for t in run) == last and limits.used_up(conn, cfg, user_id, now, run):
        return now, run
    return max(now, last or 0), []


def end_used_up(conn: sqlite3.Connection, ticket: dict, now: int) -> None:
    """End a used-up running ticket at `now`, its bonuses and upgrades with it, so the next one can start. Inside the
    caller's transaction."""
    conn.execute("UPDATE tickets SET used_up_at=? WHERE id=?", (now, ticket["id"]))
    conn.execute("UPDATE ticket_bonuses SET ends_at=MAX(starts_at, MIN(ends_at, ?)) WHERE ticket_id=? AND cancelled_at IS NULL",
                 (now, ticket["id"]))


def running(conn: sqlite3.Connection, user_id: int, now: float) -> list[dict]:
    """Every ticket of the user's covering now, earliest first: tickets bought to start now run side by side, their
    limits added up."""
    return [dict(r) for r in conn.execute(f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL "
                                          f"AND t.starts_at<=? AND {db.TICKET_EFFECTIVE_END}>? ORDER BY t.starts_at, t.id",
                                          (user_id, now, now))]


def covering(conn: sqlite3.Connection, user_id: int, now: float) -> dict | None:
    """The user's earliest ticket covering now (starts_at <= now < effective end), or None: running's first."""
    return next(iter(running(conn, user_id, now)), None)


def next_queued(conn: sqlite3.Connection, user_id: int, now: float) -> dict | None:
    return _row(conn, f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL AND t.starts_at>? "
                      f"ORDER BY t.starts_at LIMIT 1", (user_id, now))


def past(conn: sqlite3.Connection, user_id: int, now: float, limit: int = 50) -> list[dict]:
    """The user's ended tickets, newest end first, each with `ended_at` and `how` it ended: "cancelled", "used_up"
    (before its end) or "ended". A ticket cancelled before it started never ran and is left out, as in last_ended."""
    out = []
    for t in user_tickets(conn, user_id):
        if t["cancelled_at"] is not None and t["cancelled_at"] <= t["starts_at"]:
            continue
        if t["cancelled_at"] is not None and t["cancelled_at"] < t["effective_end"]:   # cut short; one cancelled after its end ran its course
            ended, how = t["cancelled_at"], "cancelled"
        else:
            ended, how = t["effective_end"], "used_up" if t["used_up_at"] is not None else "ended"
        if ended <= now:
            out.append({**t, "ended_at": ended, "how": how})
    out.sort(key=lambda t: (t["ended_at"], t["id"]), reverse=True)
    return out[:limit]


def last_ended(conn: sqlite3.Connection, user_id: int, now: float) -> dict | None:
    """The user's most recently ended ticket, with `ended_at`: its effective end, or when it was cancelled if earlier.
    A ticket cancelled before it started is skipped."""
    best = None
    for t in user_tickets(conn, user_id):
        ended = min(t["effective_end"], t["cancelled_at"]) if t["cancelled_at"] is not None else t["effective_end"]
        if t["cancelled_at"] is not None and t["cancelled_at"] <= t["starts_at"]:
            continue   # cancelled before it started: it never ran, so it never ended either
        if ended <= now and (best is None or ended > best["ended_at"]):
            best = {**t, "ended_at": ended}
    return best


def is_gated(conn: sqlite3.Connection, user_id: int) -> bool:
    """Ticket-gated: at least one ticket row not marked ungated_at, cancelled or not (spec section 7)."""
    return conn.execute("SELECT 1 FROM tickets WHERE user_id=? AND ungated_at IS NULL LIMIT 1", (user_id,)).fetchone() is not None


def current_day(ticket: dict, now: float) -> tuple[int, int]:
    """The ticket day containing now: whole 24-hour steps from starts_at, bonus days included."""
    n = int((now - ticket["starts_at"]) // DAY)
    start = ticket["starts_at"] + n * DAY
    return start, start + DAY


def bonuses(conn: sqlite3.Connection, ticket_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_bonuses WHERE ticket_id=? ORDER BY starts_at, id", (ticket_id,))]


def active_bonuses(conn: sqlite3.Connection, ticket_id: int, now: float) -> list[dict]:
    """Non-cancelled bonuses whose share period covers now."""
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_bonuses WHERE ticket_id=? AND cancelled_at IS NULL AND share_pct>0 "
                                          "AND starts_at<=? AND ends_at>? ORDER BY id", (ticket_id, now, now))]


def bonus_share(conn: sqlite3.Connection, ticket_id: int, now: float) -> float:
    """The 5-hour share the active bonuses and upgrades add."""
    return sum(b["share_5h_pct"] if b["share_5h_pct"] is not None else b["share_pct"] for b in active_bonuses(conn, ticket_id, now))


# ---------- granting ----------

# Limit rows the grant form offers to remove. Share rows are replaced by the ticket while it gates the user and come
# back if the user is ever ungated; the sign-up credit goes with the first ticket anyway.
REMOVABLE = "kind NOT IN ('share_5h', 'share_7d', 'cost_total')"


def preview(conn: sqlite3.Connection, cfg: Config, user, tier: str, length: str, currency: str, now: float,
            start_now: bool = False, order_id: int | None = None, over_5h: bool = False, over_end: bool = False,
            over_week: bool = False, account: str | None = None) -> dict:
    """Everything the grant form shows before the admin confirms. Raises TicketError for a bad tier, length or currency
    and when the currency has no rate; capacity trouble is reported in `available`, not raised. `start_now`: the
    ticket starts now beside the user's running tickets instead of after them (next_start). `order_id`: the order
    this grant sells; its referral discount (orders.ref_pct, the percentage asked for) is applied to today's price and
    floored again on today's price and share (referrals design: The discount must survive every way an order is
    granted), so the preview and the grant agree. `usd` is then the discounted price, `list_usd` stays the regular one.
    `short_5h`: the only check that failed is Anthropic's current 5-hour window (FiveHourFull, checked last). `over_5h`:
    the admin grants anyway; the pass is `available` and `reason` stays as the warning. It then draws on the window's
    free room first come, so running passes may hit Anthropic's limit before their own until the reset.
    `short_end`: the pass would outlast the subscription's end (SubscriptionEnds); `end_warning` says so. `over_end`:
    the admin grants anyway, the other checks still applying; buyers' orders and price lists keep the rule. Such a pass
    cannot be upgraded later (upgrade_quote keeps the rule: an upgrade adds share past the end too).
    `short_week`: this week's room lacks the pass's points (WeekFull); `week_warning` says so. `over_week`: the admin
    oversells anyway, later weeks' max_sold_pct and the other checks still applying. Running passes may then meet
    Anthropic's weekly limit before their own until the weekly reset.
    `account`: the subscription account to sell on (the admin's choice), checked as is; None: placement's pick, the
    account where the pass fits with the most free Pro weeks. `accounts`: every linked account as placement judged it.
    When it fits nowhere, the waivers above are judged on the account that may sell with the most free Pro weeks, and
    `account` is that one only if they make the pass available there. `share_pct`, `week_split` and the checks are
    those of `account`."""
    from . import capacity, placement   # here, not at the top: both import tickets
    now = _now(now)
    _check_tier_length(cfg, tier, length)
    steps = currencies(cfg)
    if currency not in steps:
        raise TicketError(f"Unknown currency {currency!r}.")
    rate = current_rate(conn, currency)
    if rate is None:
        raise TicketError(f"No exchange rate for {currency}; set one on the Rates panel first.")
    p = price_now(conn, tier, length, now)
    ref = conn.execute("SELECT ref_pct FROM orders WHERE id=? AND user_id=? AND upgrade_of IS NULL AND ref_pct IS NOT NULL",
                       (order_id, user["id"])).fetchone() if order_id is not None else None
    if ref is not None:
        from . import referrals   # here, not at the top: referrals imports tickets
        before = p["usd"]
        p["usd"], p["ref_pct_applied"] = referrals.apply_pct(conn, cfg, tier, length, before, ref["ref_pct"])
        p["ref_off_usd"] = round(before - p["usd"], 2)
    size, size_5h = tier_size(conn, cfg, tier), tier_size_5h(conn, cfg, tier)
    starts_at, replaces = next_start(conn, cfg, user["id"], now, start_now)
    ends_at = starts_at + LENGTHS[length] * DAY
    excl = [t["id"] for t in replaces]
    # Used-up tickets this one replaces end as it starts: their reserved points and 5-hour share go with them.
    ctxs = placement.contexts(conn, cfg, now, excl)
    judged = placement.judge(conn, cfg, size, size_5h, starts_at, ends_at, now, excl, ctxs)
    rows = [r for r, _ in judged]
    if account is not None:
        if account not in ctxs:
            raise TicketError(f"No subscription account '{account}'.")
        target = account
    else:
        target = placement.best_of(conn, cfg, [r["account"] for r in rows if r["fits"]], now, ctxs)
        # Fits nowhere: the admin's waivers are judged where the pass comes closest, the best account that may sell.
        target = target or placement.best_candidate(conn, cfg, now, ctxs)
    row = next((r for r in rows if r["account"] == target), None)
    share, share_5h = (row["share_pct"], row["share_5h_pct"]) if row else (None, None)
    # The end and this week's room the admin may grant past: each is noted, then the other checks run without it.
    skip, end_warning, week_warning = {}, None, None
    while True:
        if row is None:   # no account may sell: the first one's reason
            available, reason, short_5h = False, rows[0]["reason"] if rows else "No subscription account is linked.", False
            break
        try:
            if why := placement.unsellable(conn, target, ctxs[target]):   # the admin's pick may be one that sells nothing
                raise CapacityError(why)
            capacity.check(conn, cfg, target, share, share_5h, starts_at, ends_at, now, exclude=excl, ctx=ctxs[target], **skip)
            available, reason, short_5h = True, None, False
        except SubscriptionEnds as e:
            end_warning, skip["sub_end"] = str(e), False
            continue
        except WeekFull as e:
            week_warning, skip["room"] = str(e), False
            continue
        except FiveHourFull as e:
            available, reason, short_5h = bool(over_5h), str(e), True
        except CapacityError as e:
            available, reason, short_5h = False, str(e), False
        break
    if week_warning is not None:   # `reason` keeps any other check's message; grant names the first one not waived
        available = available and bool(over_week)
    if end_warning is not None:
        available = available and bool(over_end)
    reason = reason or week_warning or end_warning
    if account is None and not available:
        target = None
    limit_rows = [dict(r) for r in conn.execute(f"SELECT kind, scope, value, unit FROM limits WHERE user_id=? AND {REMOVABLE} ORDER BY kind, scope",
                                                (user["id"],))]
    first = conn.execute("SELECT 1 FROM tickets WHERE user_id=? LIMIT 1", (user["id"],)).fetchone() is None
    return {"tier": tier, "label": cfg.tickets.tiers[tier].label, "length": length, "days": LENGTHS[length], "share_pct": share,
            "share_5h_pct": share_5h, "size": size, "size_5h": size_5h, "account": target, "accounts": rows, **p,
            "currency": currency, "rate": rate["rate"], "rate_set_at": rate["set_at"], "stale_rate": rate_is_stale(rate, now),
            "amount": round_local(p["usd"], rate["rate"], steps[currency], vat_percent(cfg)), "vat_percent": vat_percent(cfg),
            "starts_at": starts_at, "ends_at": ends_at, "queued": starts_at > now, "available": available, "reason": reason,
            "short_5h": short_5h, "over_5h": short_5h and bool(over_5h),
            "short_end": end_warning is not None, "end_warning": end_warning, "over_end": end_warning is not None and bool(over_end),
            "short_week": week_warning is not None, "week_warning": week_warning, "over_week": week_warning is not None and bool(over_week),
            "replaces": [t["id"] for t in replaces], "start_now": bool(start_now),
            "sold_out_now": sold_out(conn, cfg, tier, length, now) if starts_at > now else not available or short_5h or end_warning is not None or week_warning is not None,
            "week_split": week_split(conn, cfg, share, starts_at, ends_at, now, account=target) if target and share is not None else None,
            "terms_version": TERMS_VERSION,
            "limit_rows": limit_rows,
            # Only the first ticket takes the sign-up credit; a credit the admin sets after that stays.
            "credit": first and conn.execute("SELECT 1 FROM limits WHERE user_id=? AND kind='cost_total'", (user["id"],)).fetchone() is not None,
            "first_ticket": first}


def grant(conn: sqlite3.Connection, cfg: Config, actor: int | None, user, tier: str, length: str, currency: str, note: str = "",
          remove_limits=(), confirm_stale_rate: bool = False, expect_usd=None, expect_rate=None, order_id: int | None = None,
          paid: bool = False, after=None, now: float | None = None, terms_version: str | None = None, start_now: bool = False,
          over_5h: bool = False, over_end: bool = False, over_week: bool = False, account: str | None = None) -> dict:
    """Sell a ticket. One BEGIN IMMEDIATE transaction: the price, the start (now, or after the user's last ticket), the
    capacity check, the insert, the credit removal (first ticket only) and any ticked limit rows, each audited.
    `expect_usd` and `expect_rate`: what the admin's preview showed; QuoteChanged if the price or rate is now different.
    `order_id`: the order this sells (order requests design, section 7), marked done in the same transaction; an
    order no longer open or linked to someone else raises orders.OrderError (409) and nothing is granted.
    A grant with `order_id` uses the order row's stored terms version and needs no new tick; a direct grant takes
    `terms_version`, equality-checked against TERMS_VERSION (2026-10-13 spec, §3).
    `paid`: the order was paid online (payments design, section 4): it sells at the order's quote, whatever the price
    and rate are now, with no stale-rate confirmation. `after(ticket_id)`: called inside the transaction, last.
    `start_now`: start now beside the user's running tickets (preview); an order's own choice governs a grant from it.
    `over_5h`: grant even though Anthropic's current 5-hour window lacks the pass's 5-hour share (preview); the
    weekly checks still refuse. Audited as over_5h. `over_end`: grant even though the pass outlasts the subscription's
    end (preview); audited as over_end. `over_week`: grant even though this week's room lacks the pass's points
    (preview); audited as over_week. `account`: the subscription account to sell on, checked as is and refused with
    its reason; None: placement's pick (preview), as every self-service sale."""
    now = _now(now)
    note = _note(note)
    if not cfg.tickets.enabled:
        raise TicketError("Tickets are not enabled: add a [tickets] section to the config.")
    if user["revoked_at"] is not None:
        raise TicketError(f"{user['name']} is revoked.")
    if not user["enabled"]:
        raise TicketError(f"{user['name']} is disabled; enable them first.")
    if paid and order_id is None:
        raise TicketError("A paid grant needs the order it pays for.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        orow = conn.execute("SELECT terms_version, start_now FROM orders WHERE id=?", (order_id,)).fetchone() if order_id is not None else None
        if orow is not None:
            start_now = bool(orow["start_now"])
        # Unpaid: the order's referral discount is applied again to today's price, so expect_usd is the discounted one.
        # tickets.ref_off_usd keeps the referral part alone (preview's re-floored ref_off_usd unpaid, the order's own
        # paid), NULL without a referral: list_usd - usd would also count a sale price (ticket_discounts) in it.
        p = preview(conn, cfg, user, tier, length, currency, now, start_now, order_id=None if paid else order_id, over_5h=over_5h,
                    over_end=over_end, over_week=over_week, account=account)
        if order_id is not None:
            grant_terms = orow["terms_version"] if orow is not None else TERMS_VERSION
        else:
            grant_terms = _check_terms(terms_version)
        if paid:
            q = conn.execute("SELECT quoted_usd, quoted_rate, quoted_amount, ref_kind, ref_off_usd FROM orders WHERE id=?",
                             (order_id,)).fetchone()
            if q is None:
                raise TicketError(f"Order #{order_id} does not exist.")
            p = p | {"usd": q["quoted_usd"], "rate": q["quoted_rate"], "amount": q["quoted_amount"],
                     "ref_off_usd": (q["ref_off_usd"] or 0.0) if q["ref_kind"] else None}
        else:
            if expect_usd is not None and abs(p["usd"] - expect_usd) > 1e-9:
                raise QuoteChanged(f"The price is now ${p['usd']:g}, not ${expect_usd:g} as shown; check the new price and grant again.")
            if expect_rate is not None and abs(p["rate"] - expect_rate) > 1e-12:
                raise QuoteChanged(f"The {currency} rate is now {p['rate']:g}, not {expect_rate:g} as shown; check the new amount and grant again.")
            if p["stale_rate"] and not confirm_stale_rate:
                raise TicketError(f"The {currency} rate is {int((now - p['rate_set_at']) // 3600)} hours old. Confirm to grant at it "
                                  "anyway, or set today's rate first.")
        for rid in p["replaces"]:   # the user's tickets are used up: they end now and this one starts in their place
            end_used_up(conn, get(conn, rid), now)
            db.audit(conn, actor, "ticket_used_up", user["name"], {"ticket_id": rid})
        if not p["available"]:   # judged by preview just now, inside this transaction (2026-11-04 spec, §2)
            raise CapacityError(_refusal(p))
        cur = conn.execute(
            "INSERT INTO tickets(user_id, user_name, account, tier, share_pct, share_5h_pct, size, size_5h, length, days, starts_at, ends_at, "
            "list_usd, usd, discount_id, currency, rate, amount, granted_by, granted_at, note, ref_off_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user["id"], user["name"], p["account"], tier, p["share_pct"], p["share_5h_pct"], p["size"], p["size_5h"], length, p["days"], p["starts_at"], p["ends_at"], p["list_usd"],
             p["usd"], p["discount_id"], currency, p["rate"], p["amount"], actor, now, note, p.get("ref_off_usd")))
        tid = cur.lastrowid
        if p["first_ticket"] and conn.execute("DELETE FROM limits WHERE user_id=? AND kind='cost_total'", (user["id"],)).rowcount:
            db.audit(conn, actor, "credit_removed", user["name"], {"ticket_id": tid})
        for kind, scope in remove_limits:
            if conn.execute(f"DELETE FROM limits WHERE user_id=? AND kind=? AND scope=? AND {REMOVABLE}", (user["id"], kind, scope)).rowcount:
                db.audit(conn, actor, "limit_clear", f"{user['name']}:{kind}:{scope}", {"ticket_id": tid})
        db.audit(conn, actor, "ticket_grant", user["name"], {"ticket_id": tid, "account": p["account"], "tier": tier, "length": length, "usd": p["usd"],
                                                            "list_usd": p["list_usd"], "currency": currency, "amount": p["amount"],
                                                            "starts_at": p["starts_at"], "ends_at": p["ends_at"],
                                                            "terms_version": grant_terms, "start_now": bool(start_now),
                                                            **({"over_5h": True} if p["over_5h"] else {}),
                                                            **({"over_end": True} if p["over_end"] else {}),
                                                            **({"over_week": True} if p["over_week"] else {})})
        if order_id is not None:
            from . import orders   # here, not at the top: orders imports tickets
            orders.close_for_grant(conn, order_id, user["id"], tid, now, actor=actor)
            from . import referrals   # here, not at the top: referrals imports tickets
            # Uses its unit (or gives it back, when an unpaid grant's floor cut it to 0%); a friend's first pass earns one.
            referrals.on_granted(conn, order_id, now, floored_to_zero=not paid and p.get("ref_pct_applied") == 0)
        if after is not None:
            after(tid)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, tid)


# ---------- what the panels and /pricing ask ----------

def capacity(conn: sqlite3.Connection, cfg: Config, now: float, account: str) -> dict:
    """The capacity panel's figures (2026-11-04 spec, §6)."""
    from . import capacity as cap   # here, not at the top: capacity imports tickets
    return cap.summary(conn, cfg, account, _now(now))


def _refusal(p: dict) -> str:
    """Why grant refuses an unavailable preview: this week's room unless waived (checked before the later weeks and
    the 5-hour window), then any other check not waived, then the subscription's end."""
    if p["short_week"] and not p["over_week"]:
        return p["week_warning"]
    if p["reason"] not in (p["end_warning"], p["week_warning"]) and not p["over_5h"]:
        return p["reason"]
    return p["end_warning"]


def sold_out(conn: sqlite3.Connection, cfg: Config, tier: str, length: str, now: float, starts_at: float | None = None,
             exclude=None, ctx=None) -> bool:
    """A tier and length is sold out when a ticket starting now, or at `starts_at`, fits on no account that may sell
    (spec §2 on each, placement), or none may (price_table tells no fresh report apart as `unavailable`). `exclude`:
    the used-up tickets it would replace (next_start), as grant leaves them out. `ctx`: placement.contexts to reuse."""
    return _misfit(conn, cfg, tier, length, now, starts_at, exclude, ctx) is not None


def _misfit(conn, cfg, tier, length, now, starts_at=None, exclude=None, ctx=None) -> CapacityError | None:
    """sold_out's reason: None when a ticket starting now (or at `starts_at`) fits on some account, else the misfit
    that clears first: of the accounts sold out only until a moment (the 5-hour window's reset, the subscription's
    end), the earliest; else the first account's. No account linked: a CapacityError saying so."""
    from . import placement   # here, not at the top: placement imports tickets
    now = _now(now)
    start = now if starts_at is None else int(starts_at)
    size = tier_size(conn, cfg, tier)
    if size is None:
        return CapacityError(f"The {tier} tier has no size.")
    errs = [e for _, e in placement.judge(conn, cfg, size, tier_size_5h(conn, cfg, tier), start, start + LENGTHS[length] * DAY,
                                          now, exclude or (), ctx)]
    if not errs:
        return CapacityError("No subscription account is linked.")
    if any(e is None for e in errs):
        return None
    timed = [e for e in errs if isinstance(e, (FiveHourFull, SubscriptionEnds))]
    return min(timed, key=lambda e: e.resets_at) if timed else errs[0]


# ---------- cancel, bonus, queue moves ----------

def _later_tickets(conn, user_id: int, from_t: int) -> list[dict]:
    """The user's non-cancelled tickets starting at or after `from_t`, earliest first."""
    return [dict(r) for r in conn.execute(f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL "
                                          f"AND t.starts_at>=? ORDER BY t.starts_at, t.id", (user_id, from_t))]


def _shift(conn, cfg: Config, chain: list[dict], delta: int, now: float) -> None:
    """Move a user's queued tickets by `delta` seconds, bonus periods with them. Moves toward the past go earliest-first
    and moves toward the future latest-first, so a moving ticket never meets its own neighbour. One savepoint, one
    Guard over the whole move: a move that does not fit undoes them all and raises CapacityError."""
    from . import capacity   # here, not at the top: capacity imports tickets
    conn.execute("SAVEPOINT moves")
    try:
        # One Guard per account the chain's passes are on: each account's capacity judges its own passes' move.
        guards = {a: capacity.Guard(conn, cfg, a, now) for a in dict.fromkeys(tk["account"] for tk in chain)}
        for tk in (chain if delta < 0 else reversed(chain)):
            conn.execute("UPDATE tickets SET starts_at=starts_at+?, ends_at=ends_at+? WHERE id=?", (delta, delta, tk["id"]))
            conn.execute("UPDATE ticket_bonuses SET starts_at=starts_at+?, ends_at=ends_at+? WHERE ticket_id=? AND cancelled_at IS NULL",
                         (delta, delta, tk["id"]))
        for a, guard in guards.items():
            mine = [tk for tk in chain if tk["account"] == a]
            guard.verify(min(tk["starts_at"] for tk in mine) + delta, max(tk["effective_end"] for tk in mine) + delta)
        conn.execute("RELEASE moves")
    except BaseException:
        conn.execute("ROLLBACK TO moves")
        conn.execute("RELEASE moves")
        raise


def cancel(conn: sqlite3.Connection, cfg: Config, actor: int | None, ticket_id: int, now: float | None = None,
           after=None) -> dict:
    """Free the slice and end the ticket's bonuses. The user's queued tickets move forward to close the gap, each
    re-checked; if one would not fit they keep their dates and `dates_kept` says so. Never refused.
    `after(ticket)`: called inside the same transaction, before it commits (a refund records itself there)."""
    now = _now(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = get(conn, ticket_id)
        if t["cancelled_at"] is not None:
            raise TicketError("This pass is already cancelled.")
        conn.execute("UPDATE tickets SET cancelled_at=?, cancelled_by=? WHERE id=?", (now, actor, ticket_id))
        conn.execute("UPDATE ticket_bonuses SET cancelled_at=?, cancelled_by=? WHERE ticket_id=? AND cancelled_at IS NULL", (now, actor, ticket_id))
        moved, kept, reason = 0, False, None
        chain = _later_tickets(conn, t["user_id"], t["effective_end"]) if t["user_id"] is not None else []
        if chain:
            # The gap closes only up to the user's other passes before the queue: one running beside this one keeps
            # the queue behind it. A queued ticket never starts in the past.
            held = conn.execute(f"SELECT MAX({db.TICKET_EFFECTIVE_END}) FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL "
                                "AND t.starts_at<?", (t["user_id"], chain[0]["starts_at"])).fetchone()[0]
            shift = max(now, t["starts_at"], held or 0) - chain[0]["starts_at"]
            if shift < 0:
                try:
                    _shift(conn, cfg, chain, shift, now)
                    moved = len(chain)
                except CapacityError as e:
                    kept, reason = True, str(e)
        db.audit(conn, actor, "ticket_cancel", t["user_name"], {"ticket_id": ticket_id, "moved": moved, "dates_kept": kept})
        if after is not None:
            after(t)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ticket": get(conn, ticket_id), "moved": moved, "dates_kept": kept, "reason": reason}


def start_now(conn: sqlite3.Connection, cfg: Config, actor: int | None, ticket_id: int, now: float | None = None) -> dict:
    """Start a queued ticket now, beside the user's running ones, its bonuses moved with it, as one bought to start now
    (next_start): used-up running tickets end and it starts in their place. The user's tickets queued after it then
    move up to close the gap, as on cancel (kept where they are if they would not fit). CapacityError, changing
    nothing, when the ticket itself does not fit now."""
    from . import limits   # here, not at the top: limits imports tickets
    now = _now(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = get(conn, ticket_id)
        if t["cancelled_at"] is not None or t["starts_at"] <= now:
            raise TicketError("That pass is not queued.")
        run = running(conn, t["user_id"], now)
        if run and limits.used_up(conn, cfg, t["user_id"], now, run):   # their share no longer counts against capacity
            for r in run:
                end_used_up(conn, r, now)
            db.audit(conn, actor, "ticket_used_up", t["user_name"], {"ticket_ids": [r["id"] for r in run]})
        _shift(conn, cfg, [t], now - t["starts_at"], now)
        moved = 0
        chain = [x for x in _later_tickets(conn, t["user_id"], t["effective_end"]) if x["id"] != t["id"]]
        if chain:
            held = conn.execute(f"SELECT MAX({db.TICKET_EFFECTIVE_END}) FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL "
                                "AND t.starts_at<?", (t["user_id"], chain[0]["starts_at"])).fetchone()[0]
            shift = max(now, held or 0) - chain[0]["starts_at"]
            if shift < 0:
                try:
                    _shift(conn, cfg, chain, shift, now)
                    moved = len(chain)
                except CapacityError:
                    pass
        db.audit(conn, actor, "ticket_start_now", t["user_name"], {"ticket_id": ticket_id, "was": t["starts_at"], "moved": moved})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, ticket_id)


def hand_over(conn: sqlite3.Connection, cfg: Config, user_id: int, now: float | None = None) -> bool:
    """The user's running tickets, used up together (limits.used_up), end now and their queued tickets move up to start
    now, so a ticket bought while the last ones ran out is usable at once. False, changing nothing, without a queued
    ticket or when the moved tickets would not fit."""
    now = _now(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        run = running(conn, user_id, now)
        # Every ticket not started yet: one queued before a pass bought to start now may start before that pass ends.
        chain = _later_tickets(conn, user_id, now + 1) if run else []
        if not chain:
            conn.execute("ROLLBACK")
            return False
        for t in run:
            end_used_up(conn, t, now)
        if chain[0]["starts_at"] > now:
            _shift(conn, cfg, chain, now - chain[0]["starts_at"], now)
        db.audit(conn, None, "ticket_used_up", run[0]["user_name"], {"ticket_ids": [t["id"] for t in run], "moved": len(chain)})
        conn.execute("COMMIT")
    except CapacityError:
        conn.execute("ROLLBACK")
        return False
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True


def move(conn: sqlite3.Connection, cfg: Config, actor: int | None, ticket_id: int, account: str, now: float | None = None,
         dry_run: bool = False) -> dict:
    """Move a running or queued pass to another subscription account (2026-11-24 spec §4): it keeps its size in Pro
    weeks, so its shares are recomputed for the target's plan, and what it used of the current week and 5-hour window
    is carried over in the target's points (limits._carry), so it is neither refilled nor shrunk. Its borrows and
    bonuses go with it, converted the same way. The target must be able to sell and the pass must fit there
    (capacity.check, no waivers); one transaction, all or nothing. `dry_run`: the same answer, nothing written (the
    admin's preview)."""
    from . import capacity, limits, placement   # here, not at the top: they import tickets
    now = _now(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = get(conn, ticket_id)
        if t["cancelled_at"] is not None:
            raise TicketError("This pass is cancelled.")
        if t["effective_end"] <= now:
            raise TicketError("This pass has ended.")
        if account == t["account"]:
            raise TicketError("The pass is already on that subscription.")
        if conn.execute("SELECT 1 FROM credentials WHERE backend='oauth' AND account=?", (account,)).fetchone() is None:
            raise TicketError(f"No subscription account '{account}'.")
        why = plans.sells(conn, account)
        if why is not None:
            raise TicketError(PLAN_UNKNOWN if why == "plan_unknown" else placement.PLAN_CHANGED)
        f7o, f5o = plans.factor(conn, t["account"], "7d"), plans.factor(conn, t["account"], "5h")
        f7n, f5n = plans.factor(conn, account, "7d"), plans.factor(conn, account, "5h")
        if f7o is None or f5o is None:
            raise TicketError("The pass's subscription has no plan, so its use can't be converted; pick its plan first.")
        if t["size"] is not None:
            size, size_5h = t["size"], t["size_5h"]
        else:   # sold before sizes: its shares on the account it is on say its size
            size = t["share_pct"] * f7o / 100
            size_5h = (t["share_5h_pct"] if t["share_5h_pct"] is not None else t["share_pct"]) * f5o / 100
        share, share_5h = plans.shares(conn, account, size, size_5h)
        capacity.check(conn, cfg, account, share, share_5h, max(now, t["starts_at"]), t["effective_end"], now)
        carry_7d = carry_5h = 0.0
        if t["starts_at"] <= now:
            # Measured on the account it is on before anything moves; a use that can't be measured (no rate) carries 0.
            carry_7d = (limits._ticket_7d(conn, cfg, t, now).current or 0.0) * f7o / f7n
            carry_5h = (limits._ticket_5h(conn, cfg, t, now).current or 0.0) * f5o / f5n
        # Its borrows in the target's points: own_pts converted, and the rate (tokens per point) the other way, so the
        # debt _debt measures from them converts by the same ratio.
        conn.execute("UPDATE pass_borrows SET own_pts=own_pts*?, rate=rate*? WHERE ticket_id=?", (f7o / f7n, f7n / f7o, ticket_id))
        for b in conn.execute("SELECT id, share_pct, share_5h_pct FROM ticket_bonuses WHERE ticket_id=? AND cancelled_at IS NULL "
                              "AND ends_at>?", (ticket_id, now)).fetchall():
            b5 = b["share_5h_pct"] if b["share_5h_pct"] is not None else b["share_pct"]
            conn.execute("UPDATE ticket_bonuses SET share_pct=?, share_5h_pct=? WHERE id=?",
                         (b["share_pct"] * f7o / f7n,
                          None if b["share_5h_pct"] is None and f5o / f5n == f7o / f7n else b5 * f5o / f5n, b["id"]))
        conn.execute("UPDATE tickets SET account=?, share_pct=?, share_5h_pct=? WHERE id=?", (account, share, share_5h, ticket_id))
        conn.execute("INSERT INTO ticket_moves(ticket_id, moved_at, from_account, to_account, from_share_pct, from_share_5h_pct, "
                     "carry_7d_pts, carry_5h_pts, moved_by) VALUES(?,?,?,?,?,?,?,?,?)",
                     (ticket_id, now, t["account"], account, t["share_pct"], t["share_5h_pct"], carry_7d, carry_5h, actor))
        out = {"ticket": get(conn, ticket_id), "from": t["account"], "to": account, "share_pct": share,
               "share_5h_pct": share_5h, "carry_7d_pts": carry_7d, "carry_5h_pts": carry_5h}
        db.audit(conn, actor, "ticket_move", t["user_name"], {"ticket_id": ticket_id, "from": t["account"], "to": account,
                                                             "carry_7d_pts": carry_7d, "carry_5h_pts": carry_5h})
        conn.execute("ROLLBACK" if dry_run else "COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return out


def add_bonus(conn: sqlite3.Connection, cfg: Config, actor: int | None, ticket_id: int, share_pct=0, extra_days=0, starts_at=None,
              ends_at=None, note: str = "", now: float | None = None) -> dict:
    """Extra share for a period inside the ticket, extra days at the ticket's share, or both. Extra days push the user's
    queued tickets forward; the moves, the extension and the share are checked as one change (capacity.Guard), and a
    failure refuses the whole bonus."""
    from . import capacity   # here, not at the top: capacity imports tickets
    now = _now(now)
    note = _note(note)
    share_pct, extra_days = share_pct or 0, extra_days or 0
    if isinstance(share_pct, bool) or not isinstance(share_pct, (int, float)) or isinstance(extra_days, bool) \
            or not isinstance(extra_days, (int, float)):
        raise TicketError("Extra share and extra days must be numbers.")
    if not math.isfinite(share_pct) or share_pct < 0 or not math.isfinite(extra_days) or extra_days < 0 \
            or (share_pct == 0 and extra_days == 0):
        raise TicketError("A bonus needs extra share above 0, extra days above 0, or both.")
    if extra_days != int(extra_days):
        raise TicketError("Extra days must be a whole number.")
    if extra_days > 365:
        raise TicketError("Extra days must be 365 or fewer.")
    if share_pct > 100:
        raise TicketError("Extra share is at most 100 points.")
    share_pct, extra_days = float(share_pct), int(extra_days)
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = get(conn, ticket_id)
        if t["cancelled_at"] is not None:
            raise TicketError("The ticket is cancelled; bonuses go on live tickets.")
        if t["used_up_at"] is not None:
            raise TicketError("The ticket was used up and the next one took over; bonuses go on live tickets.")
        old_end, new_end, moved = t["effective_end"], t["effective_end"] + extra_days * DAY, 0
        guard = capacity.Guard(conn, cfg, t["account"], now)   # before the queue moves: the bonus is judged as one change
        # Capacity is checked from now on: on a ticket that has started or ended, the past is spent either way.
        if extra_days:
            chain = _later_tickets(conn, t["user_id"], old_end) if t["user_id"] is not None else []
            if chain and chain[0]["starts_at"] < new_end:
                # Only a ticket the extension reaches has to move, and one that has started cannot.
                if chain[0]["starts_at"] <= now:
                    raise TicketError("The user's next pass has already started; the extra days would run into it.")
                try:
                    _shift(conn, cfg, chain, new_end - chain[0]["starts_at"], now)
                    moved = len(chain)
                except CapacityError as e:
                    raise CapacityError(f"The extra days would move {t['user_name']}'s queued pass, which then does not fit: {e}") from e
        if share_pct:
            s = max(int(now if starts_at is None else starts_at), t["starts_at"])
            e = min(int(new_end if ends_at is None else ends_at), new_end)
            if s >= e:
                raise TicketError("The bonus share period must lie within the pass's period.")
        else:
            s, e = old_end, new_end
        if extra_days:   # an upgrade runs to the ticket's end, so it reaches into the extra days too (2026-10-26 spec)
            conn.execute("UPDATE ticket_bonuses SET ends_at=? WHERE ticket_id=? AND tier IS NOT NULL AND cancelled_at IS NULL AND ends_at=?",
                         (new_end, ticket_id, old_end))
        cur = conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, extra_days, starts_at, ends_at, note, granted_by, granted_at) "
                           "VALUES(?,?,?,?,?,?,?,?)", (ticket_id, share_pct, extra_days, s, e, note, actor, now))
        # Judged as written: the share and the extra days together, against the room there was before (spec §2).
        hi = max(e, new_end)
        if hi > now:
            guard.verify(max(min(s, old_end) if extra_days else s, now), hi)
        db.audit(conn, actor, "ticket_bonus", t["user_name"], {"ticket_id": ticket_id, "bonus_id": cur.lastrowid, "share_pct": share_pct,
                                                               "extra_days": extra_days, "starts_at": s, "ends_at": e, "moved": moved})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"bonus": dict(conn.execute("SELECT * FROM ticket_bonuses WHERE id=?", (cur.lastrowid,)).fetchone()),
            "ticket": get(conn, ticket_id), "moved": moved}


# ---------- upgrades (2026-10-26 spec) ----------
# No longer sold (2026-10-28): a pass bought to start now runs beside the running one instead. What is here fulfils
# the upgrade orders placed before that and reads the upgrades already sold.

UPGRADE_MIN_S = 3600   # less time left than this: no upgrade is offered or sold


def was_used(conn: sqlite3.Connection, ticket: dict) -> bool:
    """Whether the ticket's holder has sent a request since it started. Only an unused ticket can be upgraded."""
    return conn.execute("SELECT 1 FROM requests WHERE user_id=? AND started_at>=? AND rejected_by IS NULL AND path!=? "
                        "AND api_key_id IS NULL LIMIT 1", (ticket["user_id"], ticket["starts_at"], COUNT_TOKENS_PATH)).fetchone() is not None


def _upgrades(conn, ticket_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ticket_bonuses WHERE ticket_id=? AND tier IS NOT NULL AND cancelled_at IS NULL "
                                          "ORDER BY starts_at, id", (ticket_id,))]


def tier_now(conn: sqlite3.Connection, ticket: dict) -> str:
    """The tier a ticket runs at: its last upgrade's, else the one it was sold as."""
    ups = _upgrades(conn, ticket["id"])
    return ups[-1]["tier"] if ups else ticket["tier"]


def upgrade_quote(conn: sqlite3.Connection, cfg: Config, ticket: dict, tier: str, currency: str, now: float) -> dict:
    """What raising a running ticket to `tier` for the rest of it costs: the two tiers' prices for the ticket's length
    today, the difference prorated over the time left. The upgrade adds the share difference (weekly and 5-hour) from
    now to the ticket's end, as a bonus would, so what the ticket already spent stays spent.
    Raises TicketError for a tier that is not higher, a ticket not running, already used or with under an hour left, or no rate;
    capacity trouble is reported in `sold_out`, not raised."""
    from . import capacity   # here, not at the top: capacity imports tickets
    now = _now(now)
    if tier not in cfg.tickets.tiers:
        raise TicketError(f"Unknown tier {tier!r}.")
    if ticket.get("trial"):
        raise TicketError("A free trial can't be upgraded; buy a pass instead.")
    if ticket["cancelled_at"] is not None or not ticket["starts_at"] <= now < ticket["effective_end"]:
        raise TicketError("Only a running pass can be upgraded.")
    if len(running(conn, ticket["user_id"], now)) > 1:
        raise TicketError("Only a pass running on its own can be upgraded; buy another pass to start now instead.")
    if was_used(conn, ticket):
        raise TicketError("This pass has been used; only a pass not used yet can be upgraded. Buy a new pass instead.")
    left = ticket["effective_end"] - now
    if left < UPGRADE_MIN_S:
        raise TicketError("Less than an hour of this pass is left; buy a new one instead.")
    steps = currencies(cfg)
    if currency not in steps:
        raise TicketError(f"Unknown currency {currency!r}.")
    rate = current_rate(conn, currency)
    if rate is None:
        raise TicketError(f"No exchange rate for {currency}.")
    frm = tier_now(conn, ticket)
    # The shares the ticket runs at now: its own plus its upgrades' (bonuses are gifts and do not count).
    ups = _upgrades(conn, ticket["id"])
    has = ticket["share_pct"] + sum(u["share_pct"] for u in ups)
    has_5h = (ticket["share_5h_pct"] if ticket["share_5h_pct"] is not None else ticket["share_pct"]) + sum(
        u["share_5h_pct"] if u["share_5h_pct"] is not None else u["share_pct"] for u in ups)
    new, new_5h = _sell_shares(conn, cfg, tier, ticket["account"])   # on the pass's own account
    share, share_5h = new - has, new_5h - has_5h
    # Prorated over the pass's whole span, its bonus days included: they run at the new tier too, and the quote never
    # comes to more than the full price difference.
    whole = ticket["effective_end"] - ticket["starts_at"]
    usd = round((price_now(conn, tier, ticket["length"], now)["usd"] - price_now(conn, frm, ticket["length"], now)["usd"]) * left / whole, 2)
    if share <= 0 or usd <= 0:
        raise TicketError(f"{cfg.tickets.tiers[tier].label} is not above this pass's tier.")
    try:
        capacity.check(conn, cfg, ticket["account"], share, max(0.0, share_5h), now, ticket["effective_end"], now)
        sold, reason = False, None
    except CapacityError as e:
        sold, reason = True, str(e)
    return {"tier": tier, "label": cfg.tickets.tiers[tier].label, "from_tier": frm, "length": ticket["length"], "share_pct": share,
            "share_5h_pct": max(0.0, share_5h), "starts_at": now, "ends_at": ticket["effective_end"], "usd": usd, "currency": currency,
            "rate": rate["rate"], "amount": round_local(usd, rate["rate"], steps[currency], vat_percent(cfg)), "sold_out": sold,
            "reason": reason}


def upgrade(conn: sqlite3.Connection, cfg: Config, actor: int | None, user, ticket_id: int, tier: str, currency: str, note: str = "",
            order_id: int | None = None, paid: bool = False, after=None, now: float | None = None) -> dict:
    """Sell an upgrade: one BEGIN IMMEDIATE transaction re-quotes it, checks capacity, writes the upgrade row and closes
    the order. `paid`: charged at the order's quote, whatever the price and rate are now. `after(ticket_id)`: called
    inside the transaction, last."""
    now = _now(now)
    note = _note(note)
    if user["revoked_at"] is not None or not user["enabled"]:
        raise TicketError(f"{user['name']} is revoked or disabled.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = get(conn, ticket_id)
        if t["user_id"] != user["id"]:
            raise TicketError("That pass is not this user's.")
        q = upgrade_quote(conn, cfg, t, tier, currency, now)
        if paid:
            o = conn.execute("SELECT quoted_usd, quoted_rate, quoted_amount FROM orders WHERE id=?", (order_id,)).fetchone()
            if o is None:
                raise TicketError(f"Order #{order_id} does not exist.")
            q |= {"usd": o["quoted_usd"], "rate": o["quoted_rate"], "amount": o["quoted_amount"]}
        if q["sold_out"]:   # judged by upgrade_quote just now, inside this transaction
            raise CapacityError(q["reason"])
        cur = conn.execute("INSERT INTO ticket_bonuses(ticket_id, share_pct, share_5h_pct, extra_days, starts_at, ends_at, note, granted_by, "
                           "granted_at, tier) VALUES(?,?,?,0,?,?,?,?,?,?)",
                           (ticket_id, q["share_pct"], q["share_5h_pct"], now, t["effective_end"], note, actor, now, tier))
        db.audit(conn, actor, "ticket_upgrade", t["user_name"], {"ticket_id": ticket_id, "bonus_id": cur.lastrowid, "from": q["from_tier"],
                                                                 "to": tier, "usd": q["usd"], "currency": currency, "amount": q["amount"]})
        if order_id is not None:
            from . import orders   # here, not at the top: orders imports tickets
            orders.close_for_grant(conn, order_id, user["id"], ticket_id, now, actor=actor)
        if after is not None:
            after(ticket_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get(conn, ticket_id)


def ungate(conn: sqlite3.Connection, actor: int | None, user_id: int, now: float | None = None, end_live: bool = False) -> int:
    """Stop the user's tickets from gating them (spec section 7, Exit). Only for a user with no active or queued
    ticket, unless `end_live`: with tickets switched off there is no Tickets tab to cancel from, so the user's active
    and queued tickets (and their bonuses) are cancelled in the same step. The tickets remain as sales records.
    Returns how many rows were marked."""
    now = _now(now)
    name = db._user_name(conn, user_id)
    live = conn.execute(f"SELECT t.id FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL AND {db.TICKET_EFFECTIVE_END}>?",
                        (user_id, now)).fetchall()
    if live and not end_live:
        raise TicketError(f"{name} has an active or queued pass; cancel it before ungating.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids = [r[0] for r in live]
        for tid in ids:
            conn.execute("UPDATE tickets SET cancelled_at=?, cancelled_by=? WHERE id=?", (now, actor, tid))
            conn.execute("UPDATE ticket_bonuses SET cancelled_at=?, cancelled_by=? WHERE ticket_id=? AND cancelled_at IS NULL", (now, actor, tid))
        n = conn.execute("UPDATE tickets SET ungated_at=? WHERE user_id=? AND ungated_at IS NULL", (now, user_id)).rowcount
        if n == 0:
            raise TicketError(f"{name} has no passes to ungate.")
        db.audit(conn, actor, "ungate", name, {"tickets": n, **({"cancelled": ids} if ids else {})})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return n


def user_state(conn: sqlite3.Connection, user_id: int, now: float) -> dict:
    """For the Users page: whether the user is ticket-gated, which ticket covers now or is queued next, and whether they
    have any ticket at all (an ungated user's tickets are still sales records the Tickets tab filters by)."""
    current, queued = covering(conn, user_id, now), next_queued(conn, user_id, now)
    return {"gated": is_gated(conn, user_id), "current": current, "queued": queued, "live": current is not None or queued is not None,
            "has_tickets": conn.execute("SELECT 1 FROM tickets WHERE user_id=? LIMIT 1", (user_id,)).fetchone() is not None}


def counts(conn: sqlite3.Connection, user_id: int, now: float) -> dict:
    """For the Users page: how many of the user's passes are `active` (running or queued, not cancelled), `used_up`
    (ran out before their end) and `expired` (ran their course). Cancelled passes count in none."""
    out = {"active": 0, "used_up": 0, "expired": 0}
    for t in user_tickets(conn, user_id):
        if t["cancelled_at"] is not None and t["cancelled_at"] < t["effective_end"]:
            continue
        if t["effective_end"] > now:
            out["active"] += 1
        else:
            out["used_up" if t["used_up_at"] is not None else "expired"] += 1
    return out


def timeline(conn: sqlite3.Connection, cfg: Config, user_id: int, now: float) -> dict | None:
    """The dashboard's chart of the user's live tickets (running and queued) from now on, when there is more than one:
    `passes` in the order they start, and `segments`, the stretches with the same tickets, each with its `stack` of
    tickets (`i`, the pass's index) and `w`, its weekly share (upgrades included) over the tallest stack's, so no
    percent reaches the user. `plan_x`: a stack's total in Claude Pro weeks (its passes' sizes), when every pass's
    account has a known plan, as the price list compares with Claude Pro."""
    now = _now(now)
    live = [dict(r) for r in conn.execute(f"SELECT {TICKET_COLS} FROM tickets t WHERE t.user_id=? AND t.cancelled_at IS NULL "
                                          f"AND {db.TICKET_EFFECTIVE_END}>? ORDER BY t.starts_at, t.id", (user_id, now))]
    if len(live) < 2:
        return None
    tiers = [tier_now(conn, t) for t in live]
    share = [t["share_pct"] + sum(u["share_pct"] for u in _upgrades(conn, t["id"]) if u["ends_at"] > now) for t in live]
    # Each pass's share in Claude Pro weeks by its own account's plan; None while that plan is unknown.
    pro = [plans.pro_weeks(conn, t["account"], x) for t, x in zip(live, share)]
    plan = "Claude Pro" if all(x is not None for x in pro) else ""
    points = sorted({now, *(max(now, t["starts_at"]) for t in live), *(t["effective_end"] for t in live)})
    segments = [{"from": a, "to": b, "on": [i for i, t in enumerate(live) if t["starts_at"] <= a and t["effective_end"] >= b]}
                for a, b in zip(points, points[1:])]
    top = max(sum(share[i] for i in s["on"]) for s in segments) or 1
    for s in segments:
        on = s.pop("on")
        s["stack"] = [{"i": i, "w": share[i] / top} for i in on]
        s["plan_x"] = round(sum(pro[i] for i in on), 2) if plan and on else None
    label = lambda x: cfg.tickets.tiers[x].label if x in cfg.tickets.tiers else x  # noqa: E731
    return {"start": now, "end": points[-1], "plan": plan or None, "segments": segments,
            "passes": [{"tier": x, "label": label(x), "length": t["length"], "starts_at": t["starts_at"], "ends_at": t["effective_end"]}
                       for x, t in zip(tiers, live)]}


def price_table(conn: sqlite3.Connection, cfg: Config, now: float, currency: str, starts_at: float | None = None,
                exclude=None) -> dict:
    """What /pricing and a user's price list show: per tier and length the regular and charged price, the local
    amounts and a sold-out flag (with `sold_out_later` for a later `starts_at`), and how a ticket starting at `starts_at` (default now) splits across Anthropic's
    weeks, and the {tier, length} the page recommends (None when unset or off sale). A length without a price is None. Nothing about who holds what, and no subscription share: `compare_pct` is the tier's weekly share as a
    percent of Claude Pro's weekly usage (its size × 100), None while the size is unset. Falls back to USD without a rate.
    `exclude`: a buyer's used-up tickets, which a pass bought now replaces (next_start), left out of `sold_out`."""
    now = _now(now)
    from . import placement   # here, not at the top: placement imports tickets
    # Each account judged once per moment: its context is shared by every tier and length.
    ctx_now = placement.contexts(conn, cfg, now, exclude or ())
    ctx_later = placement.contexts(conn, cfg, now) if (later := starts_at is not None and starts_at > now) else None
    fresh = bool(placement.candidates(conn, cfg, now, ctx_now))
    rate = current_rate(conn, currency)
    if rate is None:
        currency, rate = "USD", current_rate(conn, "USD")
    step, vat = currencies(cfg)[currency], vat_percent(cfg)   # the price lists say the VAT is in, under the prices
    tiers = []
    eff = effective_tiers(conn, cfg)
    for tid, t in cfg.tickets.tiers.items():
        lengths = {}
        for length in LENGTHS:
            try:
                p = price_now(conn, tid, length, now)
            except TicketError:   # no price: not on sale, the pages leave it out
                lengths[length] = None
                continue
            lengths[length] = {"days": LENGTHS[length], "list_usd": p["list_usd"], "usd": p["usd"], "discount_ends_at": p["discount_ends_at"],
                               "amount": round_local(p["usd"], rate["rate"], step, vat), "list_amount": round_local(p["list_usd"], rate["rate"], step, vat),
                               # the USD price with VAT, for a page showing USD beside its own currency
                               "usd_total": round_local(p["usd"], 1, USD_ROUND_TO, vat), "list_usd_total": round_local(p["list_usd"], 1, USD_ROUND_TO, vat),
                               "sold_out": (miss := _misfit(conn, cfg, tid, length, now, None, exclude, ctx_now) if fresh else None) is not None,
                               # sold out only until Anthropic's 5-hour window resets, or the subscription's end
                               # (when it renews): when it is back on sale
                               "available_at": miss.resets_at if isinstance(miss, (FiveHourFull, SubscriptionEnds)) else None,
                               "unavailable": not fresh,
                               # a buyer's list: whether one starting at `starts_at` (after their passes) fits too
                               **({"sold_out_later": sold_out(conn, cfg, tid, length, now, starts_at, None, ctx_later)} if later else {})}
        compare_pct = round(eff[tid]["size"] * 100) if eff[tid]["size"] is not None else None
        tiers.append({"tier": tid, "label": t.label, "compare": "Claude Pro", "compare_pct": compare_pct, "lengths": lengths})
    return {"currency": currency, "rate_set_at": rate["set_at"], "how_to_buy": cfg.tickets.how_to_buy, "tiers": tiers, "vat_percent": vat,
            "recommended": rec if (rec := recommended(conn, cfg)) and next(x for x in tiers if x["tier"] == rec["tier"])["lengths"][rec["length"]] else None,
            "week_split": order_split(conn, cfg, now if starts_at is None else max(now, starts_at), now, account=best)
            if (best := placement.best_account(conn, cfg, now, ctx_now)) else None}
