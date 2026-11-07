from claude_proxy import mail, orders, tickets
from claude_proxy.config import EmailConfig
from tests.test_referrals import env, env_on, signed_in  # noqa: F401  (env_on needs env; both become fixtures here too)
from tests.tickets_helpers import NOW, user

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


# ---------- Task 6: the referral discount line in a buyer's emails ----------

def test_buyer_mail_shows_discount_line_en(env_on):
    conn, cfg, ids = env_on
    o = signed_in(conn, cfg, ids["bob"])
    assert (o["ref_kind"], o["ref_off_usd"]) == ("friend", 0.8)
    body = orders.buyer_mail_text(conn, cfg, o)[1]
    assert "Referral discount: 10% (−$0.80)" in body


def test_buyer_mail_shows_discount_line_fa(env_on):
    conn, cfg, ids = env_on
    o = signed_in(conn, cfg, ids["bob"], lang="fa")
    body = orders.buyer_mail_text(conn, cfg, o)[1]
    assert "تخفیف معرفی دوستان" in body and "−$۰٫۸۰" in body


def test_buyer_mail_no_line_without_a_referral_discount(env_on):
    conn, cfg, ids = env_on
    o = signed_in(conn, cfg, ids["alice"])   # not referred in, and no saved unit: nothing to discount
    assert o["ref_kind"] is None
    body = orders.buyer_mail_text(conn, cfg, o)[1]
    assert "Referral discount" not in body


def test_granted_mail_shows_the_discount_from_the_ticket(env_on):
    conn, cfg, ids = env_on
    o = signed_in(conn, cfg, ids["bob"])
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["bob"]), o["tier"], o["length"], o["currency"],
                      order_id=o["id"], now=NOW)
    body = orders.granted_mail_text(conn, cfg, orders.get(conn, o["id"]), t)[1]
    assert "Referral discount: 10% (−$0.80)" in body


def test_granted_mail_in_persian_shows_the_discount_too(env_on):
    conn, cfg, ids = env_on
    o = signed_in(conn, cfg, ids["bob"], lang="fa")
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["bob"]), o["tier"], o["length"], o["currency"],
                      order_id=o["id"], now=NOW)
    body = orders.granted_mail_text(conn, cfg, orders.get(conn, o["id"]), t)[1]
    assert "تخفیف معرفی دوستان" in body


def test_granted_mail_shows_nothing_once_the_cost_floor_zeroes_it(env_on):
    """Controller ruling (task 6): once a ticket exists the discount line comes from it (list_usd - usd), never
    orders.ref_off_usd - a unit (or here, a friend discount) floored to 0% at a manual grant (the tier's share rose
    since the order) sells at full price, so the order's own, now-stale ref_off_usd must not show."""
    conn, cfg, ids = env_on
    o = signed_in(conn, cfg, ids["bob"])
    assert o["ref_off_usd"] == 0.8
    tickets.set_share(conn, cfg, "lite", 40, ids["admin"])   # cost now $9.33, above the $7.20 the 10% discount asks for
    t = tickets.grant(conn, cfg, ids["admin"], user(conn, ids["bob"]), o["tier"], o["length"], o["currency"],
                      order_id=o["id"], expect_usd=8.0, now=NOW)
    assert t["usd"] == 8.0
    body = orders.granted_mail_text(conn, cfg, orders.get(conn, o["id"]), t)[1]
    assert "Referral discount" not in body
