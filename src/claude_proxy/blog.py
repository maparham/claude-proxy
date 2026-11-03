"""The public blog on the home host: Persian posts for search engines and for sharing, rendered on the server so a
crawler sees the whole text. Each post is blog/<slug>.html, an HTML fragment under a header comment:

    <!--
    title: ...
    description: ...
    date: 2026-11-03
    -->
    <p>...</p>

The slug is the file name, in Latin letters so links stay readable once copied.

A post can carry a table of the packages that suit its tasks; the server fills in each package's name, usage and
today's price, so the table follows the rate and discounts. Each row names its task and package:

    <table class="packages">
    <tr data-package="lite/day"><td>یک رزومه و یک انگیزه‌نامه</td></tr>
    </table>"""
from __future__ import annotations

import datetime
import html
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

POSTS = Path(__file__).parent / "blog"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
FA_TIERS = {"lite": "اقتصادی", "standard": "استاندارد"}
FA_LENGTHS = {"day": ("یک‌روزه", "یک روز"), "week": ("هفتگی", "یک هفته"), "month": ("ماهانه", "یک ماه")}
FA_PLANS = {"Claude Pro": "کلاود پرو", "Claude Max": "کلاود مکس"}
PACKAGE_ROW = re.compile(r'<tr data-package="([a-z0-9_]+)/(day|week|month)"><td>(.*?)</td></tr>', re.S)
FA_MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")


@dataclass(frozen=True)
class Post:
    slug: str
    title: str
    description: str
    date: datetime.date
    body: str


def _parse(path: Path) -> Post:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"<!--\n(.*?)\n-->\n", text, re.S)
    if not m:
        raise ValueError(f"{path.name}: no header comment")
    meta = dict(line.split(": ", 1) for line in m.group(1).splitlines())
    return Post(path.stem, meta["title"], meta["description"], datetime.date.fromisoformat(meta["date"]), text[m.end():])


@lru_cache(maxsize=1)
def posts() -> tuple[Post, ...]:
    """Every post, newest first. Posts ship with the code, so they are read once."""
    found = [_parse(p) for p in POSTS.glob("*.html") if SLUG.fullmatch(p.stem)]
    return tuple(sorted(found, key=lambda p: (p.date, p.slug), reverse=True))


def find(slug: str) -> Post | None:
    return next((p for p in posts() if p.slug == slug), None)


def jalali(d: datetime.date) -> str:
    """The date as Iranians write it, e.g. ۱۲ آبان ۱۴۰۵."""
    gy, gm, gd = d.year, d.month, d.day
    g_days = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_days[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    jm, jd = (1 + days // 31, 1 + days % 31) if days < 186 else (7 + (days - 186) // 30, 1 + (days - 186) % 30)
    return f"{jd} {FA_MONTHS[jm - 1]} {jy}".translate(FA_DIGITS)


def _money(amount: float, currency: str) -> str:
    n = f"{round(amount):,}".translate(FA_DIGITS).replace(",", "٬")
    return f"{n} تومان" if currency == "IRT" else f"{n} {currency}"


def _usage(tier: dict, length: str) -> str:
    if not tier["compare"] or tier["compare_pct"] is None:
        return ""
    plan, pct, span = FA_PLANS.get(tier["compare"], tier["compare"]), tier["compare_pct"], FA_LENGTHS[length][1]
    times = {100: "", 200: "دو برابر ", 300: "سه برابر "}.get(round(pct))
    return f"{times}مصرف {span} {plan}" if times is not None else f"{pct:g}٪ مصرف {span} {plan}".translate(FA_DIGITS)


def packages(body: str, prices: dict | None) -> str:
    """The post with its package tables filled in from today's price table; rows for packages not on sale are dropped,
    and without prices (tickets off) the tables go."""
    tiers = {t["tier"]: t for t in prices["tiers"]} if prices else {}

    def row(m: re.Match) -> str:
        tier, length, task = tiers.get(m.group(1)), m.group(2), m.group(3)
        l = tier and tier["lengths"].get(length)
        if not l or l["amount"] is None:   # not on sale, or no exchange rate yet
            return ""
        name = f'{FA_TIERS.get(tier["tier"], tier["label"])} {FA_LENGTHS[length][0]}'
        price = _money(l["amount"], prices["currency"])
        if l["discount_ends_at"]:
            price = f'<s>{_money(l["list_amount"], prices["currency"])}</s> {price}'
        buy = ('<span class="muted">تمام شد</span>' if l["sold_out"] else
               f'<a class="btn small" href="/dashboard?tier={tier["tier"]}&amp;length={length}&amp;order=1">خرید</a>')
        return (f"<tr><td>{task}</td><td><b>{html.escape(name)}</b></td><td>{html.escape(_usage(tier, length))}</td>"
                f"<td>{price}</td><td>{buy}</td></tr>")

    def table(m: re.Match) -> str:
        rows = PACKAGE_ROW.sub(row, m.group(1))
        if not prices or "<tr>" not in rows:
            return ""
        vat = (f'<p class="packages-note">قیمت‌ها شامل {prices["vat_percent"]:g}٪ مالیات بر ارزش افزوده است.</p>'.translate(FA_DIGITS)
               if prices.get("vat_percent") else "")
        return ('<div class="packages-wrap"><table class="packages"><thead><tr><th>کار</th><th>بسته پیشنهادی</th>'
                f"<th>سهمیه</th><th>قیمت</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>{vat}")

    return re.sub(r'<table class="packages">(.*?)</table>', table, body, flags=re.S)


def _page(base: str, path: str, title: str, description: str, main: str, og_type: str = "website") -> str:
    t, d, url = html.escape(title), html.escape(description), html.escape(f"{base}{path}")
    return f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{d}">
<title>{t}</title>
<link rel="canonical" href="{url}">
<meta property="og:type" content="{og_type}">
<meta property="og:locale" content="fa_IR">
<meta property="og:site_name" content="کلاد">
<meta property="og:url" content="{url}">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{d}">
<meta property="og:image" content="{html.escape(base)}/static/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="کلاد: اشتراک کلاد بدون پرداخت ارزی">
<meta name="twitter:card" content="summary_large_image">
<link rel="alternate" type="application/rss+xml" title="وبلاگ کلاد" href="{html.escape(base)}/blog/feed.xml">
<link rel="stylesheet" href="/static/app.css">
<link rel="stylesheet" href="/static/home.css">
</head>
<body class="home blog">
<header class="home-top">
  <span class="home-top-right">
    <a class="btn small" href="/blog">وبلاگ</a>
    <a class="btn small" href="/#pricing">قیمت‌ها</a>
    <a class="btn small primary" href="/dashboard">ورود یا ثبت نام</a>
  </span>
</header>
<main>
{main}
</main>
<footer class="home-foot">
  <p><a href="/">کلاد</a> · <a href="/blog">وبلاگ</a> · <a href="/privacy">حریم خصوصی</a></p>
</footer>
</body>
</html>
"""


def index_page(base: str) -> str:
    items = "".join(
        f'<li><a href="/blog/{p.slug}"><h2>{html.escape(p.title)}</h2></a>'
        f'<p class="post-date"><time datetime="{p.date.isoformat()}">{jalali(p.date)}</time></p>'
        f"<p>{html.escape(p.description)}</p></li>" for p in posts())
    main = f'<section class="blog-head"><h1>وبلاگ کلاد</h1><p class="hero-lede">کارهایی که با بسته‌های کلاد سریع‌تر انجام می‌شوند.</p></section>\n<ul class="post-list">{items}</ul>'
    return _page(base, "/blog", "وبلاگ کلاد", "کارهایی که با بسته‌های روزانه و هفتگی کلاد سریع‌تر انجام می‌شوند.", main)


def post_page(base: str, p: Post, prices: dict | None) -> str:
    main = (f'<article class="post"><h1>{html.escape(p.title)}</h1>'
            f'<p class="post-date"><time datetime="{p.date.isoformat()}">{jalali(p.date)}</time></p>\n{packages(p.body, prices)}'
            '<p class="post-cta"><a class="btn primary" href="/#pricing">دیدن بسته‌ها و قیمت‌ها</a></p></article>')
    return _page(base, f"/blog/{p.slug}", f"{p.title} · کلاد", p.description, main, og_type="article")


def feed(base: str) -> str:
    def rfc822(d: datetime.date) -> str:
        return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = "".join(
        f"<item><title>{html.escape(p.title)}</title><link>{base}/blog/{p.slug}</link><guid>{base}/blog/{p.slug}</guid>"
        f"<pubDate>{rfc822(p.date)}</pubDate><description>{html.escape(p.description)}</description></item>" for p in posts())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>وبلاگ کلاد</title>'
            f"<link>{base}/blog</link><description>کارهایی که با بسته‌های کلاد سریع‌تر انجام می‌شوند.</description>"
            f"<language>fa</language>{items}</channel></rss>\n")
