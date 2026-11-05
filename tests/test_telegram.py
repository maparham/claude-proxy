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
    assert [b["callback_data"] for row in sent["reply_markup"]["inline_keyboard"] for b in row] == ["buy", "mine", "feedback", "lang"]
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


@pytest.mark.skip(reason="needs Task 7")
async def test_a_revoked_account_counts_as_unlinked(tg):
    from claude_proxy import db as dbm
    link(tg.conn, ALICE, tg.ids["alice"])
    dbm.revoke(tg.conn, tg.ids["alice"])
    async with app(tg) as c:
        await post(c, cb(ALICE, "buy"))
    assert tg.fake.texts(ALICE)[0].startswith(telegram.t("fa", "need_link", url="").split("\n")[0])
