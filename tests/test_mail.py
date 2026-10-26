from claude_proxy import mail
from claude_proxy.config import EmailConfig

CFG = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")


def test_english_mail_is_plain_text_only():
    msg = mail.message(CFG, "a@example.com", "Your order", "Your order: <Lite>\n")
    assert msg.get_content_type() == "text/plain"


def test_persian_mail_gets_a_right_to_left_html_copy():
    body = "سفارش شما: تیکت لایت <b>\n- شرایط تیکت (v1):\n"
    msg = mail.message(CFG, "a@example.com", "سفارش شما", body)
    assert msg.get_content_type() == "multipart/alternative"
    plain, rich = msg.get_body(("plain",)), msg.get_body(("html",))
    assert plain.get_content() == body
    h = rich.get_content()
    assert 'dir="rtl"' in h and "&lt;b&gt;" in h and "<b>" not in h


def test_direction_follows_the_first_strong_character():
    assert mail.rtl("(v1) شرایط") is False   # Latin first, as dir="auto" would read it
    assert mail.rtl("۱۲ - شرایط") is True and mail.rtl("New order: علی") is False and mail.rtl("123") is False
