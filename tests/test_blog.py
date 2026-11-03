import datetime

from claude_proxy import blog


def test_every_post_parses_with_a_title_description_and_body():
    assert blog.posts()
    for p in blog.posts():
        assert p.title and p.description and p.body.strip(), p.slug


def test_dates_are_written_in_the_persian_calendar():
    assert blog.jalali(datetime.date(2026, 11, 3)) == "۱۲ آبان ۱۴۰۵"
    assert blog.jalali(datetime.date(2026, 3, 21)) == "۱ فروردین ۱۴۰۵"
    assert blog.jalali(datetime.date(2025, 3, 20)) == "۳۰ اسفند ۱۴۰۳"   # a leap year's last day


def prices(**lite_day):
    day = {"amount": 545000.0, "list_amount": 545000.0, "discount_ends_at": None, "sold_out": False, **lite_day}
    return {"currency": "IRT", "vat_percent": 10.0, "tiers": [
        {"tier": "lite", "label": "Lite", "compare": "Claude Pro", "compare_pct": 100, "lengths": {"day": day, "week": None}},
        {"tier": "standard", "label": "Standard", "compare": "Claude Pro", "compare_pct": 200,
         "lengths": {"day": {**day, "amount": 1089000.0}, "week": None}}]}


TABLE = """<table class="packages">
<tr data-package="lite/day"><td>یک رزومه</td></tr>
<tr data-package="standard/day"><td>MVP</td></tr>
<tr data-package="lite/week"><td>اپلای</td></tr>
</table>"""


def test_a_package_table_shows_todays_price_and_usage_and_drops_packages_not_on_sale():
    out = blog.packages(TABLE, prices())
    assert "<b>اقتصادی یک‌روزه</b>" in out and "۵۴۵٬۰۰۰ تومان" in out and "مصرف یک روز کلاود پرو" in out
    assert "دو برابر مصرف یک روز کلاود پرو" in out and "۱٬۰۸۹٬۰۰۰ تومان" in out
    assert "اپلای" not in out                                    # no weekly Lite on sale
    assert 'href="/dashboard?tier=lite&amp;length=day&amp;order=1"' in out and "۱۰٪ مالیات" in out


def test_a_package_table_marks_discounts_and_sold_out_and_goes_without_prices():
    out = blog.packages(TABLE, prices(list_amount=600000.0, discount_ends_at=1.0, sold_out=True))
    assert "<s>۶۰۰٬۰۰۰ تومان</s>" in out and "تمام شد" in out
    assert blog.packages(TABLE, None) == ""


def test_a_post_gets_a_table_of_contents_linking_its_headings():
    body, toc = blog.contents("<h2>یک</h2><p>.</p><h3>دو</h3><h2>سه</h2>")
    assert body == '<h2 id="s1">یک</h2><p>.</p><h3 id="s2">دو</h3><h2 id="s3">سه</h2>'
    assert '<li class="toc-h2"><a href="#s1">یک</a></li><li class="toc-h3"><a href="#s2">دو</a></li>' in toc
    assert blog.contents("<h2>تنها</h2>")[1] == ""
