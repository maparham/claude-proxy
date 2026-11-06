"""The public blog on the home host: Persian posts for search engines and for sharing, rendered on the server so a
crawler sees the whole text. Each post is blog/<slug>.html, an HTML fragment under a header comment:

    <!--
    title: ...
    description: ...
    date: 2026-11-03
    icon: plane
    tag: مهاجرت
    color: s1
    -->
    <p>...</p>

The slug is the file name, in Latin letters so links stay readable once copied. icon and badge (the cover's large icon and
the small one on the card, from ICONS; badge defaults to icon), tag and color (a series color, s1 to s8) are optional
and dress the post's card on the home page.

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
COLORS = {f"s{i}" for i in range(1, 9)}
# Card icons as 24×24 stroke paths (Lucide shapes), drawn inline so the card needs no sprite on the page.
ICONS = {
    "card": '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20M6 15h4"/>',
    "doc": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    "plane": '<path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"/>',
    "rocket": '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
    "browser": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01"/><path d="m10 13 2.5 2.5L10 18M14 18h2"/>',
    "code": '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
    "grad": '<path d="M22 10 12 5 2 10l10 5 10-5z"/><path d="M6 12v5c3 2 9 2 12 0v-5"/>',
    "briefcase": '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>',
    "book": '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>',
    "form": '<rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4M12 16h4M8 11h.01M8 16h.01"/>',
    "pen": '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "scale": '<path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1z"/><path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1z"/><path d="M7 21h10M12 3v18M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/>',
}
FA_MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")


@dataclass(frozen=True)
class Post:
    slug: str
    title: str
    description: str
    date: datetime.date
    body: str
    icon: str = "doc"
    badge: str = "doc"   # the small icon on the card; the cover shows `icon` large
    tag: str = ""
    color: str = "s1"


def _parse(path: Path) -> Post:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"<!--\n(.*?)\n-->\n", text, re.S)
    if not m:
        raise ValueError(f"{path.name}: no header comment")
    meta = dict(line.split(": ", 1) for line in m.group(1).splitlines())
    look = {k: meta[k] for k in ("icon", "badge", "tag", "color") if k in meta}
    for k in ("icon", "badge"):
        if look.get(k, "doc") not in ICONS:
            raise ValueError(f"{path.name}: unknown {k} {look[k]!r}")
    if look.get("color", "s1") not in COLORS:
        raise ValueError(f"{path.name}: unknown color {look['color']!r}")
    look.setdefault("badge", look.get("icon", "doc"))
    return Post(path.stem, meta["title"], meta["description"], datetime.date.fromisoformat(meta["date"]), text[m.end():], **look)


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


def _page(base: str, path: str, title: str, description: str, main: str, og_type: str = "website", wide: bool = False) -> str:
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
<link rel="icon" href="/static/rahkar-favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/rahkar-icon-180.png">
<link rel="stylesheet" href="/static/app.css">
<link rel="stylesheet" href="/static/home.css">
{'<script src="/static/blog.js" defer></script>' if wide else ""}
</head>
<body class="home blog{' blog-wide' if wide else ''}">
<header class="home-top">
  <a class="home-brand" href="/"><img src="/static/rahkar-icon.svg" alt="" width="36" height="36"><span>راهکار هوش برتر</span></a>
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
    items = "".join(card(p, "h2") for p in posts())
    main = f'<section class="blog-head"><h1>وبلاگ کلاد</h1><p class="hero-lede">کارهایی که با بسته‌های کلاد سریع‌تر انجام می‌شوند.</p></section>\n<ul class="latest-list post-cards">{items}</ul>'
    return _page(base, "/blog", "وبلاگ کلاد", "کارهایی که با بسته‌های روزانه و هفتگی کلاد سریع‌تر انجام می‌شوند.", main, wide=True)


def icon(name: str, cls: str) -> str:
    return f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def card(p: Post, heading: str) -> str:
    """A post as a card, on the home page and the blog's list: a cover in the post's color with its icon, a badge
    icon, its tag, title (as `heading`), date and description."""
    tag = f'<span class="tag">{html.escape(p.tag)}</span>' if p.tag else ""
    return (f'<li><a href="/blog/{p.slug}" style="--c: var(--{p.color})">'
            f'<div class="cover">{icon(p.icon, "ic big")}</div><div class="post-ic">{icon(p.badge, "ic")}</div>'
            f'<div class="card-body">{tag}<{heading}>{html.escape(p.title)}</{heading}>'
            f'<p class="post-date"><time datetime="{p.date.isoformat()}">{jalali(p.date)}</time></p>'
            f"<p>{html.escape(p.description)}</p></div></a></li>")


def latest(n: int = 3) -> str:
    """The newest posts as cards for the home page, in Persian whichever language the page is shown in."""
    items = "".join(card(p, "h3") for p in posts()[:n])
    return f'<ul class="latest-list" lang="fa" dir="rtl">{items}</ul>' if items else ""


def contents(body: str) -> tuple[str, str]:
    """The post with an id on each h2 and h3, and its table of contents linking them; no table for under two."""
    items = []

    def anchor(m: re.Match) -> str:
        items.append((m.group(1), f"s{len(items) + 1}", m.group(2)))
        return f'<{m.group(1)} id="{items[-1][1]}">{m.group(2)}</{m.group(1)}>'

    body = re.sub(r"<(h[23])>(.*?)</\1>", anchor, body, flags=re.S)
    if len(items) < 2:
        return body, ""
    links = "".join(f'<li class="toc-{level}"><a href="#{i}">{text}</a></li>' for level, i, text in items)
    return body, ('<aside class="toc"><details open><summary>در این مطلب</summary>'
                  f'<nav aria-label="فهرست مطالب"><ol>{links}</ol></nav></details></aside>')


def post_page(base: str, p: Post, prices: dict | None) -> str:
    body, toc = contents(packages(p.body, prices))
    tag = f'<span class="tag">{html.escape(p.tag)}</span>' if p.tag else ""
    main = (f'<div class="post-layout">{toc}<article class="post" style="--c: var(--{p.color})">'
            f'<div class="post-cover"><div class="cover">{icon(p.icon, "ic big")}</div><div class="post-ic">{icon(p.badge, "ic")}</div></div>'
            f'{tag}<h1>{html.escape(p.title)}</h1>'
            f'<p class="post-date"><time datetime="{p.date.isoformat()}">{jalali(p.date)}</time></p>\n{body}'
            '<p class="post-cta"><a class="btn primary" href="/#pricing">دیدن بسته‌ها و قیمت‌ها</a></p></article></div>')
    return _page(base, f"/blog/{p.slug}", f"{p.title} · کلاد", p.description, main, og_type="article", wide=bool(toc))


def feed(base: str) -> str:
    def rfc822(d: datetime.date) -> str:
        return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = "".join(
        f"<item><title>{html.escape(p.title)}</title><link>{base}/blog/{p.slug}</link><guid>{base}/blog/{p.slug}</guid>"
        f"<pubDate>{rfc822(p.date)}</pubDate><description>{html.escape(p.description)}</description></item>" for p in posts())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>وبلاگ کلاد</title>'
            f"<link>{base}/blog</link><description>کارهایی که با بسته‌های کلاد سریع‌تر انجام می‌شوند.</description>"
            f"<language>fa</language>{items}</channel></rss>\n")
