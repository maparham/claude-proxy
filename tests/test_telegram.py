"""Telegram bot (design 2026-11-05), with the Bot API faked by FakeTG and ZarinPal by FakeZP."""
import itertools
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from claude_proxy import telegram
from claude_proxy.config import TelegramConfig
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_payments_web import env  # noqa: F401  (the fixture: tickets, IRT rate, ZarinPal faked)
from tests.test_tickets_web import env as tickets_env  # noqa: F401  (test_payments_web's env needs it)

TOKEN, SECRET = "123456:SECRET-TOKEN", "hook-secret"
ADMIN_CHAT, ALICE, ADMIN_TG = -100123, 5001, 7001
_ids = itertools.count(1)


class FakeTG:
    """Stands in for api.telegram.org. `fail[method]`: a list of error bodies, one used up per call."""
    def __init__(self):
        self.calls, self.fail, self.webhook_url = [], {}, ""
        self.ids = itertools.count(9000)

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if self.fail.get(method):
            e = self.fail[method].pop(0)
            return httpx.Response(e["error_code"], json={"ok": False, **e})
        if method == "getWebhookInfo":
            result = {"url": self.webhook_url}
        elif method in ("sendMessage", "copyMessage"):
            result = {"message_id": next(self.ids)}
        else:
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent(self, method="sendMessage", chat_id=None) -> list[dict]:
        return [b for m, b in self.calls if m == method and (chat_id is None or b.get("chat_id") == chat_id)]

    def texts(self, chat_id) -> list[str]:
        return [b["text"] for b in self.sent("sendMessage", chat_id)]


def frm(uid, username="alice_tg", first_name="Alice"):
    return {"id": uid, "is_bot": False, "language_code": "fa",
            **({"username": username} if username else {}), **({"first_name": first_name} if first_name else {})}


def msg(chat_id, text=None, sender=None, **extra) -> dict:
    m = {"message_id": next(_ids), "chat": {"id": chat_id, "type": "private"}, "from": sender or frm(chat_id), **extra}
    if text is not None:
        m["text"] = text
    return {"update_id": next(_ids), "message": m}


def cb(chat_id, data, sender=None) -> dict:
    return {"update_id": next(_ids), "callback_query": {
        "id": f"q{next(_ids)}", "from": sender or frm(chat_id), "data": data,
        "message": {"message_id": next(_ids), "chat": {"id": chat_id, "type": "private"}}}}


def group(text, reply_to=None, sender=None, **extra) -> dict:
    m = {"message_id": next(_ids), "chat": {"id": ADMIN_CHAT, "type": "supergroup"}, "from": sender or frm(ADMIN_TG, "boss"),
         "text": text, **extra}
    if reply_to is not None:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": next(_ids), "message": m}


def link(conn, chat_id, user_id, lang="fa"):
    conn.execute("INSERT INTO telegram_chats(chat_id, user_id, lang, last_seen, created_at) VALUES(?,?,?,0,0) "
                 "ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id", (chat_id, user_id, lang))


@pytest.fixture
def tg(env, monkeypatch):
    gw, conn, cfg, ids, keys, zp = env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    cfg.telegram = TelegramConfig(bot_username="rahkar_bot", admin_chat_id=ADMIN_CHAT)
    fake = FakeTG()
    gw.http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return SimpleNamespace(gw=gw, conn=conn, cfg=cfg, ids=ids, keys=keys, zp=zp, fake=fake)


async def post(c, update, secret=SECRET):
    return await c.post("/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def app(tg):
    return asgi_client(create_dashboard_app(tg.gw))


# ---------- Task 5: core ----------

async def test_webhook_needs_the_secret(tg):
    async with app(tg) as c:
        assert (await post(c, msg(ALICE, "/start"), secret="wrong")).status_code == 403
        assert (await c.post("/telegram/webhook", json=msg(ALICE, "/start"))).status_code == 403
    assert tg.fake.calls == []


async def test_webhook_rejects_a_non_ascii_secret(tg):
    # A non-ASCII header value (httpx itself won't send a str one, so pass raw bytes) must fail the comparison,
    # not raise and bubble up as a 500.
    async with app(tg) as c:
        r = await c.post("/telegram/webhook", json=msg(ALICE, "/start"),
                         headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-é-token".encode("utf-8")})
    assert r.status_code == 403
    assert tg.fake.calls == []


async def test_no_route_when_off(tg, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    async with app(tg) as c:
        assert (await post(c, msg(ALICE, "/start"))).status_code in (404, 405)   # no route (a page catch-all may answer 405)


async def test_start_shows_the_menu_in_persian(tg):
    async with app(tg) as c:
        assert (await post(c, msg(ALICE, "/start"))).status_code == 200
    (sent,) = tg.fake.sent(chat_id=ALICE)
    assert sent["text"] == telegram.t("fa", "menu")
    assert [b["callback_data"] for row in sent["reply_markup"]["inline_keyboard"] for b in row] == ["buy", "prices", "mine", "support", "feedback", "lang"]
    row = tg.conn.execute("SELECT * FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()
    assert (row["username"], row["first_name"], row["lang"], row["user_id"]) == ("alice_tg", "Alice", "fa", None)


async def test_a_repeated_update_is_handled_once(tg):
    u = msg(ALICE, "/menu")
    async with app(tg) as c:
        await post(c, u)
        await post(c, u)
    assert len(tg.fake.sent(chat_id=ALICE)) == 1


async def test_lang_switches_and_sticks(tg):
    async with app(tg) as c:
        await post(c, cb(ALICE, "lang"))
        await post(c, msg(ALICE, "/menu"))
    assert tg.fake.texts(ALICE)[0] == telegram.t("en", "lang_set")
    assert tg.fake.texts(ALICE)[-1] == telegram.t("en", "menu")
    assert tg.fake.sent("answerCallbackQuery")[0].get("text") is None


async def test_an_unknown_button_is_answered_stale(tg):
    async with app(tg) as c:
        await post(c, cb(ALICE, "zzz:1"))
    assert tg.fake.sent("answerCallbackQuery")[0]["text"] == telegram.t("fa", "stale")


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", json.dumps({"update_id": 77}).encode(),
                                  json.dumps({"update_id": 78, "message": {"text": "hi"}}).encode()])
async def test_a_malformed_update_gets_200_and_nothing_else(tg, body):   # Review Focus 4
    async with app(tg) as c:
        r = await c.post("/telegram/webhook", content=body, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET,
                                                                     "content-type": "application/json"})
    assert r.status_code == 200 and tg.fake.calls == []
    assert tg.conn.execute("SELECT COUNT(*) FROM telegram_chats").fetchone()[0] == 0


async def test_rate_limit_answers_once_then_drops(tg):
    async with app(tg) as c:
        for _ in range(telegram.MSG_PER_HOUR + 3):
            await post(c, msg(ALICE, "/menu"))
    texts = tg.fake.texts(ALICE)
    assert texts.count(telegram.t("fa", "menu")) == telegram.MSG_PER_HOUR
    assert texts.count(telegram.t("fa", "too_many")) == 1


async def test_429_is_retried_and_403_marks_blocked(tg):
    tg.fake.fail["sendMessage"] = [{"error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 0}}]
    async with app(tg) as c:
        await post(c, msg(ALICE, "/menu"))
        assert len(tg.fake.sent(chat_id=ALICE)) == 2   # the retry
        tg.fake.fail["sendMessage"] = [{"error_code": 403, "description": "Forbidden: bot was blocked by the user"}]
        await post(c, msg(ALICE, "/menu"))
    assert tg.conn.execute("SELECT blocked FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == 1


async def test_the_token_is_never_logged(tg, caplog):
    caplog.set_level(logging.INFO)
    tg.fake.fail["sendMessage"] = [{"error_code": 400, "description": "Bad Request"}]
    async with app(tg) as c:
        await post(c, msg(ALICE, "/menu"))
    assert "sendMessage" in caplog.text and TOKEN not in caplog.text
    # httpx's own "HTTP Request: ..." INFO line carries the URL, which holds the token; it must be masked, not dropped.
    assert "HTTP Request" in caplog.text and "<token>" in caplog.text


# ---------- Task 6: linking ----------

from tests.test_web import admin_client, bearer  # noqa: E402


async def link_url(c, tg):
    r = await c.post("/api/telegram/link", headers=bearer(tg.keys["alice"]))
    assert r.status_code == 200, r.text
    return r.json()["url"]


async def test_link_from_the_dashboard(tg):
    async with app(tg) as c:
        url = await link_url(c, tg)
        assert url.startswith("https://t.me/rahkar_bot?start=")
        code = url.split("=", 1)[1]
        assert tg.conn.execute("SELECT COUNT(*) FROM telegram_link_codes WHERE code_hash=?", (code,)).fetchone()[0] == 0   # hashed
        await post(c, msg(ALICE, f"/start {code}"))
        st = (await c.get("/api/telegram", headers=bearer(tg.keys["alice"]))).json()
    assert st == {"on": True, "bot": "rahkar_bot", "connected": "@alice_tg"}
    assert tg.fake.texts(ALICE)[0] == telegram.t("fa", "linked", who="al…@example.com")
    assert tg.conn.execute("SELECT user_id FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == tg.ids["alice"]


async def test_a_code_works_once_and_expires(tg):
    async with app(tg) as c:
        code = (await link_url(c, tg)).split("=", 1)[1]
        await post(c, msg(ALICE, f"/start {code}"))
        await post(c, msg(5002, f"/start {code}"))        # used
        old = telegram.new_link_code(tg.conn, tg.ids["alice"], now=0)
        await post(c, msg(5003, f"/start {old}"))          # expired
    assert tg.fake.texts(5002) == [telegram.t("fa", "link_bad")]
    assert tg.fake.texts(5003) == [telegram.t("fa", "link_bad")]
    assert tg.conn.execute("SELECT chat_id FROM telegram_chats WHERE user_id=?", (tg.ids["alice"],)).fetchone()[0] == ALICE


async def test_relink_moves_the_account_and_tells_the_old_chat(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        code = (await link_url(c, tg)).split("=", 1)[1]
        await post(c, msg(5009, f"/start {code}"))
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "relinked_away")]
    assert tg.conn.execute("SELECT chat_id FROM telegram_chats WHERE user_id=?", (tg.ids["alice"],)).fetchone()[0] == 5009
    assert tg.conn.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0] == "telegram_relink"


async def test_claiming_a_code_moves_a_chat_linked_to_someone_else_and_audits_them(tg):   # Final review finding 2
    link(tg.conn, ALICE, tg.ids["admin"])   # this chat already belongs to a different account (B)
    async with app(tg) as c:
        code = (await link_url(c, tg)).split("=", 1)[1]   # a code for alice's own account (A)
        await post(c, msg(ALICE, f"/start {code}"))
    assert tg.conn.execute("SELECT user_id FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == tg.ids["alice"]
    from claude_proxy import db as dbm
    rows = tg.conn.execute("SELECT action, target FROM audit_log WHERE action='telegram_unlink' ORDER BY id DESC").fetchall()
    assert rows and rows[0]["target"] == dbm._user_name(tg.conn, tg.ids["admin"])


async def test_unlink_from_bot_and_dashboard(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, msg(ALICE, "/unlink"))
        await post(c, msg(ALICE, "/unlink"))
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "unlinked"), telegram.t("fa", "not_linked")]
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        assert (await c.post("/api/telegram/unlink", headers=bearer(tg.keys["alice"]))).json() == {"ok": True}
    assert tg.conn.execute("SELECT user_id FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] is None


async def test_link_needs_csrf(tg):
    async with admin_client(tg.gw) as c:
        del c.headers["x-csrf-token"]
        assert (await c.post("/api/telegram/link")).status_code == 403


async def test_link_api_off_without_the_bot(tg, monkeypatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET")
    async with app(tg) as c:
        assert (await c.post("/api/telegram/link", headers=bearer(tg.keys["alice"]))).status_code == 404
        assert (await c.get("/api/telegram", headers=bearer(tg.keys["alice"]))).json() == {"on": False}


async def test_a_revoked_account_counts_as_unlinked(tg):
    from claude_proxy import db as dbm
    link(tg.conn, ALICE, tg.ids["alice"])
    dbm.revoke(tg.conn, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
    assert tg.fake.texts(ALICE)[0].startswith(telegram.t("fa", "need_link", url="").split("\n")[0])


# ---------- Task 7: buying ----------

from claude_proxy import orders, payments, tickets  # noqa: E402


def buttons(sent) -> list[str]:
    return [b.get("callback_data") or b.get("url") for row in sent["reply_markup"]["inline_keyboard"] for b in row]


async def buy_week(c, tg, chat=ALICE):
    """Buy, Lite, a week, then return the summary's buttons."""
    await post(c, cb(chat, "buy"))
    await post(c, cb(chat, "t:lite"))
    await post(c, cb(chat, "l:week"))
    return buttons(tg.fake.sent(chat_id=chat)[-1])


async def test_buy_needs_a_linked_account(tg):
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "need_link", url="https://dash.rahkar.pro/dashboard")]


async def test_buy_paused_when_zarinpal_cannot_take_it(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    payments.set_pay_mode(tg.conn, tg.ids["admin"], "manual")
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "paused")]


async def test_buy_to_a_zarinpal_link(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
        tiers = buttons(tg.fake.sent(chat_id=ALICE)[-1])
        assert "t:lite" in tiers
        await post(c, cb(ALICE, "t:lite"))
        assert "l:week" in buttons(tg.fake.sent(chat_id=ALICE)[-1])
        await post(c, cb(ALICE, "l:week"))
        summary = tg.fake.sent(chat_id=ALICE)[-1]
        (pay,) = buttons(summary)
        assert pay.startswith("p:") and "/privacy#terms" in summary["text"]
        await post(c, cb(ALICE, pay))
    ready = tg.fake.sent(chat_id=ALICE)[-1]
    assert ready["text"] == telegram.t("fa", "pay_ready")
    assert buttons(ready)[0].startswith("https://payment.zarinpal.com/pg/StartPay/")
    o = tg.conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone()
    assert (o["via"], o["tier"], o["length"], o["lang"], o["email"]) == ("telegram", "lite", "week", "fa", "alice@example.com")


async def test_a_double_tap_opens_one_payment(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        await post(c, cb(ALICE, pay))
    assert len(tg.zp.requests) == 1
    assert tg.fake.sent("answerCallbackQuery")[-1]["text"] == telegram.t("fa", "stale")


async def test_an_open_order_offers_continue_or_cancel(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        await post(c, cb(ALICE, "buy"))
        offer = tg.fake.sent(chat_id=ALICE)[-1]
        oid = tg.conn.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert buttons(offer) == [f"oc:{oid}", f"ox:{oid}"]
        await post(c, cb(ALICE, f"oc:{oid}"))
        assert tg.fake.sent(chat_id=ALICE)[-1]["text"] == telegram.t("fa", "pay_ready")
        await post(c, cb(ALICE, f"ox:{oid}"))
    assert orders.get(tg.conn, oid)["status"] == "withdrawn"
    assert "t:lite" in buttons(tg.fake.sent(chat_id=ALICE)[-1])   # back to choosing


async def test_no_email_asks_for_one(tg):
    tg.conn.execute("UPDATE users SET email=NULL WHERE id=?", (tg.ids["alice"],))
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "ask_email")
        await post(c, msg(ALICE, "not an email"))
        assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "bad_email")
        await post(c, msg(ALICE, "alice@mail.example"))
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "pay_ready")
    assert tg.conn.execute("SELECT email FROM orders ORDER BY id DESC LIMIT 1").fetchone()[0] == "alice@mail.example"
    assert tg.conn.execute("SELECT email FROM users WHERE id=?", (tg.ids["alice"],)).fetchone()[0] is None


@pytest.mark.parametrize("data", ["t:nope", "l:year"])   # a tier, a length no longer (or never) on sale
async def test_an_old_button_shows_the_tiers_again(tg, data):   # Review Focus 2
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
        await post(c, cb(ALICE, "t:lite"))
        await post(c, cb(ALICE, data))
    assert "t:lite" in buttons(tg.fake.sent(chat_id=ALICE)[-1])


async def test_a_zarinpal_failure_is_shown_in_persian(tg):
    from claude_proxy import zarinpal
    link(tg.conn, ALICE, tg.ids["alice"])
    tg.zp.request_answer = zarinpal.ZarinpalUnavailable("down")
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
    assert tg.fake.texts(ALICE)[-1] == telegram.FA_ERRORS[payments.UNAVAILABLE]


async def test_payment_starts_are_rate_limited(tg, monkeypatch):
    monkeypatch.setattr(telegram, "PAY_PER_HOUR", 1)
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        oid = tg.conn.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()[0]
        await post(c, cb(ALICE, f"oc:{oid}"))
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "too_many")
    assert len(tg.zp.requests) == 1


async def test_continuing_a_web_order_from_the_bot_gets_a_telegram_notice(tg):   # Final review finding 1
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        await c.post("/api/orders/pay", headers=bearer(tg.keys["alice"]),
                     json={"tier": "lite", "length": "week", "terms_version": "v1"})
        oid = tg.conn.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert orders.get(tg.conn, oid)["via"] == "web"
        await post(c, cb(ALICE, f"oc:{oid}"))
        assert orders.get(tg.conn, oid)["via"] == "telegram"
        auth = buttons(tg.fake.sent(chat_id=ALICE)[-1])[0].rsplit("/", 1)[1]
        r = await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    assert r.status_code == 302
    assert tg.fake.texts(ALICE)[-1].startswith("پرداخت انجام شد")


async def test_my_passes(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, cb(ALICE, "mine"))
        assert tg.fake.texts(ALICE)[-1].startswith(telegram.t("fa", "mine_none"))
        user = tg.conn.execute("SELECT * FROM users WHERE id=?", (tg.ids["alice"],)).fetchone()
        tickets.grant(tg.conn, tg.cfg, tg.ids["admin"], user, "lite", "week", "IRT", terms_version="v1")
        await post(c, cb(ALICE, "mine"))
    assert tg.fake.texts(ALICE)[-1].startswith("Lite: تا ")


# ---------- Task 7 review fix round 1 ----------

@pytest.mark.parametrize("leave", ["menu", "mine"])
async def test_leaving_the_email_prompt_clears_its_state(tg, leave):   # Review finding 2
    tg.conn.execute("UPDATE users SET email=NULL WHERE id=?", (tg.ids["alice"],))
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "ask_email")
        await post(c, cb(ALICE, leave))
        await post(c, msg(ALICE, "not an email"))
    assert telegram.t("fa", "bad_email") not in tg.fake.texts(ALICE)
    # With no state, plain text gets the menu (support only after its button).
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "menu")


async def test_unlink_clears_the_email_prompts_state(tg):   # Final review finding 3
    tg.conn.execute("UPDATE users SET email=NULL WHERE id=?", (tg.ids["alice"],))
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "ask_email")
        await post(c, msg(ALICE, "/unlink"))
        await post(c, msg(ALICE, "not an email"))
    assert telegram.t("fa", "bad_email") not in tg.fake.texts(ALICE)
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "menu")


async def test_start_with_a_bad_code_clears_the_email_prompts_state(tg):   # Final review finding 3
    # An expired/bad code returns "link_bad" without ever reaching menu() (whose own set_state would otherwise
    # have cleared the state as a side effect), so this is the path that actually exercises the bug.
    tg.conn.execute("UPDATE users SET email=NULL WHERE id=?", (tg.ids["alice"],))
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        (pay,) = await buy_week(c, tg)
        await post(c, cb(ALICE, pay))
        assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "ask_email")
        await post(c, msg(ALICE, "/start bogus-code"))
        assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "link_bad")
        await post(c, msg(ALICE, "not an email"))
    assert telegram.t("fa", "bad_email") not in tg.fake.texts(ALICE)
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "menu")


@pytest.mark.parametrize("data,start_now", [("s:a", 0), ("s:n", 1)])
async def test_choosing_when_to_start_sets_the_orders_start_now(tg, data, start_now):   # Review finding 3
    link(tg.conn, ALICE, tg.ids["alice"])
    user = tg.conn.execute("SELECT * FROM users WHERE id=?", (tg.ids["alice"],)).fetchone()
    tickets.grant(tg.conn, tg.cfg, tg.ids["admin"], user, "lite", "week", "IRT", terms_version="v1")
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
        await post(c, cb(ALICE, "t:lite"))
        await post(c, cb(ALICE, "l:week"))
        assert buttons(tg.fake.sent(chat_id=ALICE)[-1]) == ["s:a", "s:n"]
        await post(c, cb(ALICE, data))
        (pay,) = buttons(tg.fake.sent(chat_id=ALICE)[-1])
        await post(c, cb(ALICE, pay))
    assert tg.conn.execute("SELECT start_now FROM orders ORDER BY id DESC LIMIT 1").fetchone()[0] == start_now


# ---------- Task 8: after payment ----------

import asyncio  # noqa: E402

from claude_proxy import cli, web  # noqa: E402


async def paid_via_bot(c, tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    (pay,) = await buy_week(c, tg)
    await post(c, cb(ALICE, pay))
    return buttons(tg.fake.sent(chat_id=ALICE)[-1])[0].rsplit("/", 1)[1]   # the authority


async def test_callback_tells_the_buyer_without_email(tg):
    assert tg.cfg.email is None
    async with app(tg) as c:
        auth = await paid_via_bot(c, tg)
        r = await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    assert r.status_code == 302
    assert tg.fake.texts(ALICE)[-1].startswith("پرداخت انجام شد (کد پیگیری زرین‌پال: 201). بسته شما از ")


async def test_a_web_order_gets_no_telegram_message(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        r = await c.post("/api/orders/pay", headers=bearer(tg.keys["alice"]), json={"tier": "lite", "length": "week", "terms_version": "v1"})
        auth = r.json()["url"].rsplit("/", 1)[1]
        await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    assert tg.fake.sent(chat_id=ALICE) == []


async def test_reconcile_tells_the_buyer(tg):
    async with app(tg) as c:
        await paid_via_bot(c, tg)
    tg.conn.execute("UPDATE payments SET created_at=created_at-7200")
    await cli._reconcile_once(tg.conn, tg.cfg, tg.gw.http)
    assert tg.fake.texts(ALICE)[-1].startswith("پرداخت انجام شد")


async def test_unfulfilled_payment_message(tg):
    async with app(tg) as c:
        auth = await paid_via_bot(c, tg)
    pid = tg.conn.execute("SELECT id FROM payments WHERE authority=?", (auth,)).fetchone()[0]
    tg.conn.execute("UPDATE payments SET status='paid_unfulfilled', ref_id='555' WHERE id=?", (pid,))
    await telegram.after_payment(tg.conn, tg.cfg, tg.gw.http, pid)
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "unfulfilled", ref="555")


async def test_paid_with_no_ticket_found_sends_nothing(tg, caplog):   # Final review finding: 'paid' with no ticket
    caplog.set_level(logging.WARNING)
    async with app(tg) as c:
        auth = await paid_via_bot(c, tg)
    pid = tg.conn.execute("SELECT id FROM payments WHERE authority=?", (auth,)).fetchone()[0]
    oid = tg.conn.execute("SELECT order_id FROM payments WHERE id=?", (pid,)).fetchone()[0]
    tg.conn.execute("UPDATE payments SET status='paid', ref_id='555' WHERE id=?", (pid,))
    # A real ticket, but not this buyer's: tickets.user_tickets(user_id=alice) won't find it, same as a lookup
    # that comes up empty.
    admin_user = tg.conn.execute("SELECT * FROM users WHERE id=?", (tg.ids["admin"],)).fetchone()
    other = tickets.grant(tg.conn, tg.cfg, tg.ids["admin"], admin_user, "lite", "week", "IRT", terms_version="v1")
    tg.conn.execute("UPDATE orders SET ticket_id=? WHERE id=?", (other["id"], oid))
    before = len(tg.fake.sent("sendMessage", ALICE))
    await telegram.after_payment(tg.conn, tg.cfg, tg.gw.http, pid)
    assert len(tg.fake.sent("sendMessage", ALICE)) == before
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_a_blocked_buyer_does_not_break_the_payment(tg):   # Review Focus 5
    async with app(tg) as c:
        auth = await paid_via_bot(c, tg)
        tg.fake.fail["sendMessage"] = [{"error_code": 403, "description": "Forbidden: bot was blocked by the user"}]
        r = await c.get(f"/pay/callback?Authority={auth}&Status=OK", follow_redirects=False)
        await asyncio.gather(*list(web._mail_tasks))
    assert r.status_code == 302
    assert tg.conn.execute("SELECT status FROM payments WHERE authority=?", (auth,)).fetchone()[0] == "paid"
    assert tg.conn.execute("SELECT blocked FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == 1


# ---------- Task 9: support and feedback ----------

def admin_linked(tg):
    link(tg.conn, ADMIN_TG, tg.ids["admin"])


def inbox(tg):
    return [dict(r) for r in tg.conn.execute("SELECT * FROM inbox_messages ORDER BY id")]


async def test_a_message_goes_to_the_admin_group(tg):
    link(tg.conn, ALICE, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, cb(ALICE, "support"))
        u = msg(ALICE, "my key does not work")
        await post(c, u)
        await post(c, msg(ALICE, "still broken"))
    (header, header2) = tg.fake.sent("sendMessage", ADMIN_CHAT)
    assert header["text"] == "#support · Alice · @alice_tg · alice@example.com" and "parse_mode" not in header
    copy = tg.fake.sent("copyMessage")[0]
    assert (copy["chat_id"], copy["from_chat_id"], copy["message_id"]) == (ADMIN_CHAT, ALICE, u["message"]["message_id"])
    assert [(m["kind"], m["direction"], m["text"]) for m in inbox(tg)] == [("support", "in", "my key does not work"), ("support", "in", "still broken")]
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "ask_support"), telegram.t("fa", "got_support")]   # confirmed once per 10 minutes
    assert tg.conn.execute("SELECT COUNT(*) FROM inbox_admin_msgs").fetchone()[0] == 4


async def test_feedback_button_then_text(tg):
    async with app(tg) as c:
        await post(c, cb(ALICE, "feedback"))
        await post(c, msg(ALICE, "love it", sender=frm(ALICE, username=None, first_name=None)))   # Review Focus 1
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "ask_feedback"), telegram.t("fa", "got_feedback")]
    assert tg.fake.sent("sendMessage", ADMIN_CHAT)[0]["text"] == "#feedback · not linked"
    assert inbox(tg)[0]["kind"] == "feedback"
    assert tg.conn.execute("SELECT state FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] is None


async def test_photo_long_text_and_stickers(tg):
    async with app(tg) as c:
        await post(c, cb(ALICE, "support"))
        await post(c, msg(ALICE, None, photo=[{"file_id": "F"}], caption="screenshot"))
        await post(c, msg(ALICE, "x" * (telegram.TEXT_MAX + 1)))
        await post(c, msg(ALICE, None, sticker={"file_id": "S"}))
    assert [(m["media"], m["text"]) for m in inbox(tg)] == [("photo", "screenshot")]
    assert tg.fake.texts(ALICE)[2:] == [telegram.t("fa", "too_long"), telegram.t("fa", "unsupported")]


async def admin_reply(c, tg, text="try again now", sender=None, **extra):
    await post(c, cb(ALICE, "support"))
    await post(c, msg(ALICE, "help"))
    copy_id = tg.conn.execute("SELECT MAX(admin_msg_id) FROM inbox_admin_msgs").fetchone()[0]
    u = group(text, reply_to=copy_id, sender=sender, **extra)
    await post(c, u)
    return u


async def test_an_admin_reply_reaches_the_user(tg):
    admin_linked(tg)
    async with app(tg) as c:
        u = await admin_reply(c, tg)
    back = tg.fake.sent("copyMessage", ALICE)[0]
    assert (back["from_chat_id"], back["message_id"]) == (ADMIN_CHAT, u["message"]["message_id"])
    out = inbox(tg)[-1]
    assert (out["direction"], out["text"], out["actor"]) == ("out", "try again now", tg.ids["admin"])
    assert tg.fake.sent("setMessageReaction")[0]["reaction"] == [{"type": "emoji", "emoji": "👍"}]


async def test_a_reply_to_the_header_works_too(tg):
    admin_linked(tg)
    async with app(tg) as c:
        await post(c, cb(ALICE, "support"))
        await post(c, msg(ALICE, "help"))
        header_id = tg.conn.execute("SELECT MIN(admin_msg_id) FROM inbox_admin_msgs").fetchone()[0]
        await post(c, group("on it", reply_to=header_id))
    assert len(tg.fake.sent("copyMessage", ALICE)) == 1


@pytest.mark.parametrize("who", ["stranger", "demoted", "anonymous"])
async def test_replies_that_do_not_count(tg, who):
    admin_linked(tg)
    sender = None
    if who == "stranger":
        sender = frm(8888, "rando")
    elif who == "demoted":
        tg.conn.execute("UPDATE users SET role='user' WHERE id=?", (tg.ids["admin"],))
    async with app(tg) as c:
        if who == "anonymous":
            await admin_reply(c, tg, sender=frm(1087968824, "GroupAnonymousBot"), sender_chat={"id": ADMIN_CHAT})
        else:
            await admin_reply(c, tg, sender=sender)
    assert tg.fake.sent("copyMessage", ALICE) == []
    group_texts = tg.fake.texts(ADMIN_CHAT)
    assert (telegram.ANON in group_texts) == (who == "anonymous")


async def test_reply_to_a_user_who_blocked_the_bot(tg):
    admin_linked(tg)
    async with app(tg) as c:
        await post(c, cb(ALICE, "support"))
        await post(c, msg(ALICE, "help"))
        copy_id = tg.conn.execute("SELECT MAX(admin_msg_id) FROM inbox_admin_msgs").fetchone()[0]
        tg.fake.fail["copyMessage"] = [{"error_code": 403, "description": "Forbidden: bot was blocked by the user"}]
        await post(c, group("hello?", reply_to=copy_id))
        assert tg.fake.texts(ADMIN_CHAT)[-1] == telegram.NOT_DELIVERED_BLOCKED
        assert tg.conn.execute("SELECT blocked FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == 1
        await post(c, msg(ALICE, "back"))
    assert tg.conn.execute("SELECT blocked FROM telegram_chats WHERE chat_id=?", (ALICE,)).fetchone()[0] == 0


async def test_admin_reply_in_an_unsupported_type_is_reported_not_delivered(tg):   # Final review finding 4
    admin_linked(tg)
    async with app(tg) as c:
        await admin_reply(c, tg, text=None, sticker={"file_id": "S"})
    assert tg.fake.sent("copyMessage", ALICE) == []
    assert tg.fake.texts(ADMIN_CHAT)[-1] == telegram.NOT_DELIVERED_UNSUPPORTED


async def test_group_chatter_is_ignored(tg):   # Review Focus 3
    admin_linked(tg)
    async with app(tg) as c:
        await post(c, group("morning all"))
        await post(c, group("re: that", reply_to=424242))
    assert tg.fake.calls == []


async def test_other_groups_are_ignored(tg):
    async with app(tg) as c:
        u = group("hi")
        u["message"]["chat"] = {"id": -100999, "type": "supergroup"}
        await post(c, u)
    assert tg.fake.calls == [] and inbox(tg) == []


async def test_text_without_the_support_button_gets_the_menu(tg):
    async with app(tg) as c:
        await post(c, msg(ALICE, "dsaa"))
        await post(c, msg(ALICE, None, photo=[{"file_id": "F"}]))
    assert tg.fake.texts(ALICE) == [telegram.t("fa", "menu")] * 2
    assert tg.fake.sent(chat_id=ADMIN_CHAT) == [] and inbox(tg) == []


async def test_the_support_window_closes_after_30_minutes(tg):
    async with app(tg) as c:
        await post(c, cb(ALICE, "support"))
        await post(c, msg(ALICE, "first"))
        await post(c, msg(ALICE, "second"))   # still open: follow-ups go too
        tg.conn.execute("UPDATE telegram_chats SET pending=? WHERE chat_id=?",
                        (json.dumps({"until": __import__("time").time() - 1}), ALICE))
        await post(c, msg(ALICE, "much later"))
    assert [m["text"] for m in inbox(tg)] == ["first", "second"]
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "menu")


async def test_an_admin_reply_opens_the_window_again(tg):
    admin_linked(tg)
    async with app(tg) as c:
        await admin_reply(c, tg)
        tg.conn.execute("UPDATE telegram_chats SET state=NULL, pending=NULL WHERE chat_id=?", (ALICE,))
        copy_id = tg.conn.execute("SELECT MAX(admin_msg_id) FROM inbox_admin_msgs").fetchone()[0]
        await post(c, group("anything else?", reply_to=copy_id))
        await post(c, msg(ALICE, "yes, one more thing"))
    assert inbox(tg)[-1]["text"] == "yes, one more thing"


# ---------- Task 10: registration ----------

async def test_set_webhook_registers_our_url(tg):
    assert await telegram.set_webhook(tg.cfg, tg.gw.http) is True
    (hook,) = tg.fake.sent("setWebhook")
    assert hook == {"url": "https://dash.rahkar.pro/telegram/webhook", "secret_token": SECRET,
                    "allowed_updates": ["message", "callback_query"]}
    assert len(tg.fake.sent("setMyCommands")) == 2


async def test_startup_only_checks(tg, caplog):
    tg.fake.webhook_url = "https://elsewhere.example/telegram/webhook"
    await telegram.check_webhook(tg.cfg, tg.gw.http)
    assert [m for m, _ in tg.fake.calls] == ["getWebhookInfo"]
    assert "elsewhere.example" in caplog.text
    tg.fake.calls.clear()
    caplog.clear()
    tg.fake.webhook_url = telegram.webhook_url(tg.cfg)
    await telegram.check_webhook(tg.cfg, tg.gw.http)
    assert caplog.text == ""


def test_cli_refuses_when_off(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="The bot is off"):
        cli.main(["telegram", "set-webhook"])


# ---------- Prices (2026-11-05, after launch) ----------

def price_list(conn, cfg):
    return payments.prices(conn, cfg, __import__("time").time())


@pytest.mark.parametrize("update", [cb(ALICE, "prices"), msg(ALICE, "/prices")])
async def test_prices_for_anyone_with_buy_and_the_site(tg, update):
    async with app(tg) as c:   # Alice's chat is not linked: prices need no account
        await post(c, update)
    sent = tg.fake.sent(chat_id=ALICE)[-1]
    text, table = sent["text"], price_list(tg.conn, tg.cfg)
    assert text.startswith(telegram.t("fa", "prices_head"))
    for x in table["tiers"]:
        assert x["label"] in text
        for n, p in x["lengths"].items():
            if p is not None:
                assert f"{telegram.t('fa', f'per_{n}')}  {orders.fa_amount(p['amount'], 'IRT')}" in text
    assert buttons(sent) == ["buy", "https://rahkar.pro"]


async def test_prices_in_english_show_a_discount(tg):
    link(tg.conn, ALICE, tg.ids["alice"], lang="en")
    now = __import__("time").time()
    tickets.create_discount(tg.conn, tg.cfg, "lite", "week", 6, now - 60, now + 86400, tg.ids["admin"], now=now)
    async with app(tg) as c:
        await post(c, cb(ALICE, "prices"))
    text = tg.fake.texts(ALICE)[-1]
    p = next(x for x in price_list(tg.conn, tg.cfg)["tiers"] if x["tier"] == "lite")["lengths"]["week"]
    assert p["list_amount"] > p["amount"]
    pct = round((1 - p["usd"] / p["list_usd"]) * 100)
    assert f"Weekly  {payments._toman(p['amount'])}  🏷 {pct}% off" in text


async def test_prices_mark_sold_out_and_the_plan_share(tg, monkeypatch):
    table = {"vat_percent": 10, "tiers": [{"tier": "lite", "label": "Lite", "compare": "Claude Pro", "compare_pct": 60, "lengths": {
        "day": None,
        "week": {"amount": 900000, "list_amount": 900000, "usd": 6, "list_usd": 6, "sold_out": True, "unavailable": False}}}]}
    monkeypatch.setattr(payments, "prices", lambda conn, cfg, now: table)
    link(tg.conn, ALICE, tg.ids["alice"], lang="en")
    async with app(tg) as c:
        await post(c, cb(ALICE, "prices"))
    text = tg.fake.texts(ALICE)[-1]
    assert "Lite (about 60% of Claude Pro)" in text
    assert "Weekly  900,000 Toman  ⛔" in text and "Daily" not in text
    assert text.endswith("⛔ sold out for now · prices include 10% VAT")


async def test_prices_without_a_toman_rate_point_to_the_site(tg):
    del tg.cfg.tickets.currencies["IRT"]
    async with app(tg) as c:
        await post(c, cb(ALICE, "prices"))
    assert tg.fake.texts(ALICE)[-1] == telegram.t("fa", "prices_site", url="https://rahkar.pro")


@pytest.mark.parametrize("pct, head", [(100, "Lite (same as Claude Pro)"), (200, "Lite (2× Claude Pro)")])
async def test_prices_read_a_whole_number_of_plans_as_plans(tg, monkeypatch, pct, head):
    table = {"vat_percent": 0, "tiers": [{"tier": "lite", "label": "Lite", "compare": "Claude Pro", "compare_pct": pct, "lengths": {
        "week": {"amount": 900000, "list_amount": 900000, "usd": 6, "list_usd": 6, "sold_out": False, "unavailable": False}}}]}
    monkeypatch.setattr(payments, "prices", lambda conn, cfg, now: table)
    link(tg.conn, ALICE, tg.ids["alice"], lang="en")
    async with app(tg) as c:
        await post(c, cb(ALICE, "prices"))
    text = tg.fake.texts(ALICE)[-1]
    assert head in text and "VAT" not in text and "⛔" not in text
