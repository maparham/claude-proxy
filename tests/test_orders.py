import pytest

from claude_proxy import db as dbm, orders, tickets
from claude_proxy.config import EmailConfig
from tests.tickets_helpers import DAY, NOW, seeded, user


def visitor(conn, cfg, n=0, now=NOW, **kw):
    """A visitor order: a fresh IP and email per `n` unless given."""
    args = {"tier": "lite", "length": "week", "currency": "EUR", "name": f"Visitor {n}", "email": f"v{n}@example.com",
            "ip": f"10.0.0.{n}"} | kw
    return orders.create(conn, cfg, now=now, **args)


def signed_in(conn, cfg, uid, email="alice@example.com", now=NOW, **kw):
    args = {"tier": "lite", "length": "week", "currency": "EUR", "name": user(conn, uid)["name"], "email": email} | kw
    return orders.create(conn, cfg, user_id=uid, now=now, **args)


def add_user(conn, ids, name):
    ids[name], _ = dbm.create_user(conn, name)
    return ids[name]


def actions(conn):
    return [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'order_%' ORDER BY id")]


def refused(status, needle=None):
    return pytest.raises(orders.OrderError, match=needle) if needle else pytest.raises(orders.OrderError)


@pytest.fixture
def env(db):
    return seeded(db)


@pytest.fixture
def mailing(env):
    conn, cfg, ids = env
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    return env


# ---------- schema ----------

def test_orders_table_has_the_spec_columns(db):
    cols = {r[1] for r in db[1].execute("PRAGMA table_info(orders)")}
    assert cols == {"id", "created_at", "updated_at", "user_id", "name", "email", "tier", "length", "currency", "quoted_usd",
                    "quoted_rate", "quoted_amount", "message", "status", "admin_note", "ticket_id", "ip", "admin_mail",
                    "buyer_mail", "seen_at"}


# ---------- create ----------

def test_visitor_order_is_stored_with_the_quote(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg, message="Hi there")
    assert (o["tier"], o["length"], o["currency"], o["quoted_usd"], o["quoted_rate"], o["quoted_amount"]) == ("lite", "week", "EUR", 8, 0.92, 7.5)
    assert (o["name"], o["email"], o["message"], o["status"], o["user_id"], o["ip"]) == ("Visitor 0", "v0@example.com", "Hi there", "new", None, "10.0.0.0")
    assert (o["created_at"], o["updated_at"], o["admin_note"], o["ticket_id"], o["seen_at"]) == (NOW, NOW, "", None, None)
    assert (o["admin_mail"], o["buyer_mail"]) == ("off", "off")   # no [email] config
    assert actions(conn) == ["order_new"]


def test_signed_in_order_is_tied_to_the_user_and_charges_the_discount(env):
    conn, cfg, ids = env
    tickets.create_discount(conn, cfg, "lite", "week", 6, NOW - 10, NOW + DAY, ids["admin"], now=NOW - 10)
    o = signed_in(conn, cfg, ids["alice"], currency="USD")
    assert (o["user_id"], o["name"], o["email"], o["ip"], o["quoted_usd"], o["quoted_amount"]) == (ids["alice"], "alice", "alice@example.com", None, 6, 6)


def test_with_email_config_both_mails_are_pending(mailing):
    conn, cfg, ids = mailing
    o = visitor(conn, cfg)
    assert (o["admin_mail"], o["buyer_mail"]) == ("pending", "pending")


@pytest.mark.parametrize("kw,needle", [
    ({"tier": "gold"}, "Unknown tier"),
    ({"length": "year"}, "Unknown length"),
    ({"currency": "GBP"}, "Unknown currency"),
    ({"name": "x" * 101}, "100 characters"),
    ({"name": "  "}, "name"),
    ({"message": "x" * 1001}, "1000 characters"),
    ({"email": "not an email"}, "email"),
    ({"email": "a@b"}, "email"),
    ({"email": "a b@c.de"}, "email"),
    ({"email": ""}, "email"),
])
def test_bad_input_is_400(env, kw, needle):
    conn, cfg, ids = env
    with refused(400, needle) as e:
        visitor(conn, cfg, **kw)
    assert e.value.status == 400
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_name_of_100_and_message_of_1000_are_fine(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg, name="x" * 100, message="m" * 1000)
    assert len(o["name"]) == 100 and len(o["message"]) == 1000


def test_currency_without_a_rate_is_400(env):
    conn, cfg, ids = env
    conn.execute("DELETE FROM fx_rates")
    with refused(400, "No exchange rate") as e:
        visitor(conn, cfg)
    assert e.value.status == 400


def test_cr_and_lf_are_stripped_from_name_and_email(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg, name="Eve\r\nBcc: x@y.z", email="eve@example.com\r\n")
    assert o["name"] == "EveBcc: x@y.z" and o["email"] == "eve@example.com"


def test_html_in_name_and_message_is_stored_verbatim(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg, name="<img src=x onerror=alert(1)>", message="<script>alert(1)</script> ünïcødé")
    assert o["name"] == "<img src=x onerror=alert(1)>" and o["message"] == "<script>alert(1)</script> ünïcødé"


def test_sold_out_is_refused_with_409(env):
    conn, cfg, ids = env
    cfg.tickets.max_sold_pct = 5
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "week", "EUR", now=NOW)
    with refused(409, "sold out") as e:
        visitor(conn, cfg)
    assert e.value.status == 409
    bob = add_user(conn, ids, "bob")
    with refused(409, "sold out"):
        signed_in(conn, cfg, bob, email="bob@example.com")


def test_one_open_order_per_user(env):
    conn, cfg, ids = env
    o = signed_in(conn, cfg, ids["alice"])
    with refused(409) as e:
        signed_in(conn, cfg, ids["alice"], tier="standard")
    assert e.value.status == 409 and str(e.value) == "You already have an open order."
    orders.withdraw(conn, ids["alice"], o["id"])
    assert signed_in(conn, cfg, ids["alice"])["status"] == "new"


def test_visitor_limit_per_ip(env):
    conn, cfg, ids = env
    for n in range(3):
        visitor(conn, cfg, n, ip="1.2.3.4")
    with refused(429) as e:
        visitor(conn, cfg, 9, ip="1.2.3.4")
    assert e.value.status == 429 and str(e.value) == "Too many orders today; try again tomorrow or sign in."
    assert visitor(conn, cfg, 9, ip="1.2.3.4", now=NOW + DAY + 1)["status"] == "new"   # 24 hours later


def test_visitor_limit_per_email_ignores_case(env):
    conn, cfg, ids = env
    for n in range(3):
        visitor(conn, cfg, n, email="Bob@Example.com")
    with refused(429) as e:
        visitor(conn, cfg, 9, email="bob@example.COM")
    assert str(e.value) == "Too many orders today; try again tomorrow or sign in."
    assert not any(ch.isdigit() for ch in str(e.value))


def test_signed_in_orders_do_not_count_toward_the_visitor_limits(env):
    conn, cfg, ids = env
    for name in ("u1", "u2", "u3"):
        signed_in(conn, cfg, add_user(conn, ids, name), email="same@example.com")
    assert visitor(conn, cfg, email="same@example.com")["status"] == "new"


def test_global_visitor_limit(env):
    conn, cfg, ids = env
    for n in range(50):
        visitor(conn, cfg, n, ip=f"10.1.{n}.1")
    with refused(429) as e:
        visitor(conn, cfg, 77, ip="10.9.9.9")
    assert str(e.value) == "Orders are busy today; please sign in to order."
    signed_in(conn, cfg, ids["alice"])   # signed-in users still can


# ---------- status changes ----------

ALLOWED = {("new", "contacted"), ("new", "declined"), ("contacted", "declined"), ("new", "withdrawn"), ("contacted", "withdrawn")}


def put_in(conn, oid, status):
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))


@pytest.mark.parametrize("frm", ["new", "contacted", "done", "declined", "withdrawn"])
@pytest.mark.parametrize("to", ["new", "contacted", "done", "declined", "withdrawn"])
def test_the_transition_table(env, frm, to):
    conn, cfg, ids = env
    o = visitor(conn, cfg)
    put_in(conn, o["id"], frm)
    if (frm, to) in ALLOWED:
        r = orders.set_status(conn, ids["admin"], o["id"], to, note="no stock" if to == "declined" else None, now=NOW + 5)
        assert r["status"] == to and r["updated_at"] == NOW + 5
        assert actions(conn)[-1] == f"order_{to}"
    else:
        with refused(409) as e:
            orders.set_status(conn, ids["admin"], o["id"], to, note="n")
        assert e.value.status == 409 and orders.get(conn, o["id"])["status"] == frm


def test_decline_needs_a_note_and_stores_it(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg)
    with refused(400, "note") as e:
        orders.set_status(conn, ids["admin"], o["id"], "declined")
    assert e.value.status == 400
    with refused(400):
        orders.set_status(conn, ids["admin"], o["id"], "declined", note="x" * 201)
    assert orders.set_status(conn, ids["admin"], o["id"], "declined", note="Sold elsewhere")["admin_note"] == "Sold elsewhere"


def test_note_can_be_edited_in_any_status(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg)
    put_in(conn, o["id"], "done")
    assert orders.set_note(conn, ids["admin"], o["id"], " paid by transfer ")["admin_note"] == "paid by transfer"
    with refused(400, "200"):
        orders.set_note(conn, ids["admin"], o["id"], "x" * 201)
    assert actions(conn)[-1] == "order_note"


def test_missing_order_is_404(env):
    conn, cfg, ids = env
    with refused(404) as e:
        orders.get(conn, 999)
    assert e.value.status == 404
    with refused(404):
        orders.set_status(conn, ids["admin"], 999, "contacted")


# ---------- retention ----------

@pytest.mark.parametrize("to", ["declined", "withdrawn"])
def test_ip_is_cleared_when_an_order_closes(env, to):
    conn, cfg, ids = env
    o = visitor(conn, cfg)
    assert orders.set_status(conn, ids["admin"], o["id"], to, note="n")["ip"] is None


def test_ip_is_kept_on_contacted(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg)
    assert orders.set_status(conn, ids["admin"], o["id"], "contacted")["ip"] == "10.0.0.0"


def test_clear_old_ips_after_30_days(env):
    conn, cfg, ids = env
    old = visitor(conn, cfg, 1)
    new = visitor(conn, cfg, 2, now=NOW + 20 * DAY)
    assert orders.clear_old_ips(conn, now=NOW + 29 * DAY) == 0
    assert orders.clear_old_ips(conn, now=NOW + 31 * DAY) == 1
    assert orders.get(conn, old["id"])["ip"] is None and orders.get(conn, new["id"])["ip"] == "10.0.0.2"


def test_deleting_the_user_keeps_the_order(env):
    conn, cfg, ids = env
    o = signed_in(conn, cfg, ids["alice"])
    dbm.revoke(conn, ids["alice"])
    dbm.delete_user(conn, ids["alice"], ids["admin"], now=NOW)
    row = orders.get(conn, o["id"])
    assert (row["user_id"], row["name"], row["email"], row["status"]) == (None, "alice", "alice@example.com", "new")
    assert [x["id"] for x in orders.admin_list(conn, "open")] == [o["id"]]
    assert orders.admin_list(conn, "open")[0]["user_name"] is None


# ---------- linking ----------

def test_link_to_an_existing_user(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg)
    r = orders.link(conn, cfg, ids["admin"], o["id"], user_id=ids["alice"])
    assert r["user_id"] == ids["alice"] and actions(conn)[-1] == "order_link"
    with refused(409):   # already linked
        orders.link(conn, cfg, ids["admin"], o["id"], user_id=ids["admin"])
    assert user(conn, ids["alice"])["email"] is None


def test_link_refused_for_closed_signed_in_or_unknown(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg)
    with refused(404):
        orders.link(conn, cfg, ids["admin"], o["id"], user_id=999)
    with refused(400):
        orders.link(conn, cfg, ids["admin"], o["id"])   # neither user_id nor create
    orders.set_status(conn, ids["admin"], o["id"], "declined", note="n")
    with refused(409):
        orders.link(conn, cfg, ids["admin"], o["id"], user_id=ids["alice"])
    s = signed_in(conn, cfg, ids["alice"])
    with refused(409):
        orders.link(conn, cfg, ids["admin"], s["id"], user_id=ids["admin"])


def test_link_creates_a_user_named_after_the_email(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg, email="New.Buyer@example.com")
    r = orders.link(conn, cfg, ids["admin"], o["id"], create=True)
    u = user(conn, r["user_id"])
    assert (u["name"], u["role"], u["email"]) == ("New.Buyer@example.com", "user", None)
    assert "order_link" in actions(conn)


def test_link_create_refused_when_the_name_or_email_is_taken(env):
    conn, cfg, ids = env
    taken, _ = dbm.create_user(conn, "carol@example.com")
    o = visitor(conn, cfg, 1, email="CAROL@example.com")
    with refused(409) as e:
        orders.link(conn, cfg, ids["admin"], o["id"], create=True)
    assert e.value.status == 409 and e.value.extra == {"existing_user_id": taken}
    conn.execute("UPDATE users SET email='dave@example.com' WHERE id=?", (ids["alice"],))
    o = visitor(conn, cfg, 2, email="Dave@example.com")
    with refused(409) as e:
        orders.link(conn, cfg, ids["admin"], o["id"], create=True)
    assert e.value.extra == {"existing_user_id": ids["alice"]}
    assert orders.get(conn, o["id"])["user_id"] is None


def test_suggest_user_matches_email_or_name_without_case(env):
    conn, cfg, ids = env
    conn.execute("UPDATE users SET email='Alice@Example.com' WHERE id=?", (ids["alice"],))
    o = visitor(conn, cfg, email="alice@example.COM")
    assert orders.suggest_user(conn, o)["id"] == ids["alice"]
    bob, _ = dbm.create_user(conn, "Bob@Example.com")
    assert orders.suggest_user(conn, visitor(conn, cfg, 1, email="bob@example.com"))["id"] == bob
    assert orders.suggest_user(conn, visitor(conn, cfg, 2, email="nobody@example.com")) is None
    assert orders.get(conn, o["id"])["user_id"] is None   # suggested, never linked


def test_a_typed_email_never_reaches_users_email(env):
    conn, cfg, ids = env
    o = signed_in(conn, cfg, ids["alice"], email="typed@example.com")
    v = visitor(conn, cfg, email="alice-visitor@example.com")
    orders.link(conn, cfg, ids["admin"], v["id"], user_id=ids["alice"])
    orders.set_status(conn, ids["admin"], o["id"], "contacted")
    orders.withdraw(conn, ids["alice"], o["id"])
    assert user(conn, ids["alice"])["email"] is None


# ---------- the buyer's view ----------

HIDDEN = {"ip", "admin_note", "admin_mail", "buyer_mail", "ticket_id"}


def test_mine_shows_the_open_order_without_admin_fields(env):
    conn, cfg, ids = env
    assert orders.mine(conn, ids["alice"]) is None
    o = signed_in(conn, cfg, ids["alice"])
    orders.set_note(conn, ids["admin"], o["id"], "secret")
    m = orders.mine(conn, ids["alice"])
    assert m["id"] == o["id"] and m["status"] == "new" and not HIDDEN & set(m)
    assert orders.mine(conn, ids["admin"]) is None


def test_mine_shows_a_closed_order_until_dismissed(env):
    conn, cfg, ids = env
    o = signed_in(conn, cfg, ids["alice"])
    orders.set_status(conn, ids["admin"], o["id"], "declined", note="secret reason")
    m = orders.mine(conn, ids["alice"])
    assert m["status"] == "declined" and "secret reason" not in str(m)
    with refused(404):
        orders.dismiss(conn, ids["admin"], o["id"])   # not theirs
    orders.dismiss(conn, ids["alice"], o["id"])
    assert orders.mine(conn, ids["alice"]) is None


def test_dismiss_refused_while_open(env):
    conn, cfg, ids = env
    o = signed_in(conn, cfg, ids["alice"])
    with refused(409):
        orders.dismiss(conn, ids["alice"], o["id"])


def test_mine_hides_withdrawn_and_older_orders(env):
    conn, cfg, ids = env
    o = signed_in(conn, cfg, ids["alice"])
    orders.set_status(conn, ids["admin"], o["id"], "declined", note="n")
    o2 = signed_in(conn, cfg, ids["alice"], now=NOW + 10)   # a new order hides the old one
    assert orders.mine(conn, ids["alice"])["id"] == o2["id"]
    orders.withdraw(conn, ids["alice"], o2["id"])
    assert orders.mine(conn, ids["alice"]) is None


def test_withdraw_only_own_open_orders(env):
    conn, cfg, ids = env
    o = signed_in(conn, cfg, ids["alice"])
    with refused(404):
        orders.withdraw(conn, ids["admin"], o["id"])
    orders.set_status(conn, ids["admin"], o["id"], "contacted")
    assert orders.withdraw(conn, ids["alice"], o["id"])["status"] == "withdrawn"
    with refused(409):
        orders.withdraw(conn, ids["alice"], o["id"])
    assert actions(conn)[-1] == "order_withdrawn"


# ---------- buyer mail cap ----------

def test_buyer_mail_cap_skips_the_fourth_mail_to_one_address(mailing):
    conn, cfg, ids = mailing
    states = []
    for name in ("u1", "u2", "u3", "u4"):
        o = signed_in(conn, cfg, add_user(conn, ids, name), email="Stranger@example.com")
        states.append((o["admin_mail"], o["buyer_mail"]))
    assert states == [("pending", "pending")] * 3 + [("pending", "skipped")]


def test_buyer_mail_cap_counts_only_sent_or_pending(mailing):
    conn, cfg, ids = mailing
    for name in ("u1", "u2", "u3"):
        o = signed_in(conn, cfg, add_user(conn, ids, name), email="x@example.com")
        orders.set_mail(conn, o["id"], "buyer_mail", "failed")
    assert signed_in(conn, cfg, add_user(conn, ids, "u4"), email="x@example.com")["buyer_mail"] == "pending"


def test_set_mail_validates(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg)
    orders.set_mail(conn, o["id"], "admin_mail", "sent")
    assert orders.get(conn, o["id"])["admin_mail"] == "sent"
    with pytest.raises(ValueError):
        orders.set_mail(conn, o["id"], "ip", "sent")
    with pytest.raises(ValueError):
        orders.set_mail(conn, o["id"], "admin_mail", "bogus")


# ---------- admin list ----------

def test_admin_list_filters_and_counts_new(env):
    conn, cfg, ids = env
    a = visitor(conn, cfg, 1)
    b = visitor(conn, cfg, 2, now=NOW + 1)
    c = signed_in(conn, cfg, ids["alice"], now=NOW + 2)
    orders.set_status(conn, ids["admin"], a["id"], "declined", note="n")
    orders.set_status(conn, ids["admin"], b["id"], "contacted")
    assert [o["id"] for o in orders.admin_list(conn, "open")] == [c["id"], b["id"]]
    assert [o["id"] for o in orders.admin_list(conn, "all")] == [c["id"], b["id"], a["id"]]
    assert [o["id"] for o in orders.admin_list(conn, "declined")] == [a["id"]]
    assert [o["id"] for o in orders.admin_list(conn, None)] == [c["id"], b["id"]]   # default: open
    assert orders.admin_list(conn, "open")[0]["user_name"] == "alice"
    with refused(400):
        orders.admin_list(conn, "bogus")
    assert orders.new_count(conn) == 1


# ---------- mail texts and dispatch ----------

EVIL = "<script>alert('x')</script>"


def test_admin_mail_carries_the_buyers_text(mailing):
    conn, cfg, ids = mailing
    cfg.listener.dashboard_url = "https://gw.example.com/"
    o = visitor(conn, cfg, name=f"Mallory {EVIL}", message=f"Please {EVIL}")
    subject, body = orders.admin_mail_text(cfg, o)
    assert subject == "New order: Lite week"
    for needle in (f"Mallory {EVIL}", "v0@example.com", f"Please {EVIL}", "7.50 EUR", "https://gw.example.com/dashboard#orders"):
        assert needle in body
    assert "Account: none" in body
    o = signed_in(conn, cfg, ids["alice"])
    assert "Account: alice" in orders.admin_mail_text(cfg, o | {"user_name": "alice"})[1]


def test_buyer_mail_carries_nothing_the_buyer_typed(mailing):
    conn, cfg, ids = mailing
    o = visitor(conn, cfg, name=f"Mallory {EVIL} http://spam.example", message=f"Buy now {EVIL} http://spam.example")
    subject, body = orders.buyer_mail_text(cfg, o)
    assert subject == "Your order: Lite week"
    assert "Mallory" not in body and "Buy now" not in body and "spam" not in body and "<script>" not in body
    assert "7.50 EUR" in body and cfg.tickets.how_to_buy in body and "This is a request; the admin will contact you." in body


def fake_send(monkeypatch, fail=False):
    sent = []

    def send(email, to, subject, body):
        if fail:
            raise OSError("smtp down")
        sent.append((to, subject))
    monkeypatch.setattr(orders.mail, "send", send)
    return sent


async def test_dispatch_sends_both_and_records_sent(mailing, monkeypatch):
    conn, cfg, ids = mailing
    sent = fake_send(monkeypatch)
    o = visitor(conn, cfg)
    await orders.dispatch(conn, cfg, o["id"])
    assert sent == [("admin@example.com", "New order: Lite week"), ("v0@example.com", "Your order: Lite week")]
    o = orders.get(conn, o["id"])
    assert (o["admin_mail"], o["buyer_mail"]) == ("sent", "sent")


async def test_dispatch_failure_records_failed_and_keeps_the_order(mailing, monkeypatch, caplog):
    conn, cfg, ids = mailing
    fake_send(monkeypatch, fail=True)
    o = visitor(conn, cfg)
    await orders.dispatch(conn, cfg, o["id"])
    o = orders.get(conn, o["id"])
    assert (o["status"], o["admin_mail"], o["buyer_mail"]) == ("new", "failed", "failed")
    assert "smtp down" in caplog.text


async def test_dispatch_skips_off_and_skipped(mailing, monkeypatch):
    conn, cfg, ids = mailing
    sent = fake_send(monkeypatch)
    o = visitor(conn, cfg)
    orders.set_mail(conn, o["id"], "buyer_mail", "skipped")
    await orders.dispatch(conn, cfg, o["id"])
    assert [to for to, _ in sent] == ["admin@example.com"]
    assert orders.get(conn, o["id"])["buyer_mail"] == "skipped"
    cfg.email = None
    sent.clear()
    o = visitor(conn, cfg, 1)
    await orders.dispatch(conn, cfg, o["id"])
    assert sent == [] and orders.get(conn, o["id"])["admin_mail"] == "off"


# ---------- grants that close an order ----------

def grant(conn, cfg, ids, uid, order_id, **kw):
    return tickets.grant(conn, cfg, ids["admin"], user(conn, uid), "lite", "week", "EUR", order_id=order_id, now=NOW + 60, **kw)


def test_grant_with_order_id_marks_it_done(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg)
    orders.link(conn, cfg, ids["admin"], o["id"], user_id=ids["alice"])
    t = grant(conn, cfg, ids, ids["alice"], o["id"])
    o = orders.get(conn, o["id"])
    assert (o["status"], o["ticket_id"], o["ip"]) == ("done", t["id"], None)
    assert actions(conn)[-1] == "order_done"
    assert orders.mine(conn, ids["alice"])["status"] == "done"


@pytest.mark.parametrize("how", ["withdrawn", "declined", "done", "other_user", "unlinked", "missing"])
def test_grant_refused_when_the_order_changed(env, how):
    conn, cfg, ids = env
    o = signed_in(conn, cfg, ids["alice"])
    oid = o["id"]
    if how == "withdrawn":
        orders.withdraw(conn, ids["alice"], oid)
    elif how in ("declined", "done"):
        put_in(conn, oid, how)
    elif how == "unlinked":
        oid = visitor(conn, cfg)["id"]
    elif how == "missing":
        oid = 999
    target = ids["admin"] if how == "other_user" else ids["alice"]
    with refused(409) as e:
        grant(conn, cfg, ids, target, oid)
    assert e.value.status == 409 and str(e.value) == "This order was withdrawn or changed; reload."
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
    assert "ticket_grant" not in [r[0] for r in conn.execute("SELECT action FROM audit_log")]


def test_a_failed_grant_leaves_the_order_unchanged(env):
    conn, cfg, ids = env
    o = signed_in(conn, cfg, ids["alice"])
    cfg.tickets.max_sold_pct = 5
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["admin"]), "lite", "week", "EUR", now=NOW)
    with pytest.raises(tickets.CapacityError):
        grant(conn, cfg, ids, ids["alice"], o["id"])
    assert orders.get(conn, o["id"])["status"] == "new"
    with pytest.raises(tickets.QuoteChanged):
        cfg.tickets.max_sold_pct = 80
        grant(conn, cfg, ids, ids["alice"], o["id"], expect_usd=1)
    assert orders.get(conn, o["id"])["status"] == "new"


def test_grant_without_order_id_touches_no_order(env):
    conn, cfg, ids = env
    o = signed_in(conn, cfg, ids["alice"])
    grant(conn, cfg, ids, ids["alice"], None)
    assert orders.get(conn, o["id"])["status"] == "new"


@pytest.mark.parametrize("email", ["postmaster,victim@x.com", "z<victim@x.com>", "a;b@x.com", 'a"b@x.com', "a:b@x.com", "(c)v@x.com"])
def test_an_email_that_would_name_several_recipients_is_400(env, email):
    conn, cfg, ids = env
    with refused(400):
        visitor(conn, cfg, email=email)


def test_visitor_limited_checks_ip_and_global_without_writing(env):
    conn, cfg, ids = env
    for n in range(3):
        visitor(conn, cfg, n, ip="10.9.9.9")
    with refused(429, "Too many orders today"):
        orders.visitor_limited(conn, "10.9.9.9", now=NOW)
    orders.visitor_limited(conn, "10.9.9.8", now=NOW)   # another address: fine
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3


def test_link_refuses_a_revoked_user(env):
    conn, cfg, ids = env
    o = visitor(conn, cfg)
    uid = add_user(conn, ids, "rex")
    dbm.revoke(conn, uid, ids["admin"])
    with refused(409, "revoked"):
        orders.link(conn, cfg, ids["admin"], o["id"], user_id=uid)
    assert orders.get(conn, o["id"])["user_id"] is None


def test_the_buyer_view_leaves_out_name_email_and_message(env):
    conn, cfg, ids = env
    signed_in(conn, cfg, ids["alice"], message="private")
    assert not {"name", "email", "message"} & set(orders.mine(conn, ids["alice"]))


def test_mail_send_goes_only_to_the_given_address(monkeypatch):
    from claude_proxy import mail
    got = {}

    class Fake:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **kw): pass
        def send_message(self, msg, to_addrs=None): got["to"] = to_addrs
    monkeypatch.setattr(mail.smtplib, "SMTP", Fake)
    mail.send(EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com"), "v@x.com", "s", "b")
    assert got["to"] == ["v@x.com"]
