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
