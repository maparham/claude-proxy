"""Telegram bot design, section 3: the tables, deleting an account, and retention."""
import time

from claude_proxy import db as dbm
from claude_proxy.db import create_user


def chat(conn, chat_id, user_id=None, last_seen=None):
    now = int(time.time())
    conn.execute("INSERT INTO telegram_chats(chat_id, user_id, last_seen, created_at) VALUES(?,?,?,?)",
                 (chat_id, user_id, last_seen or now, now))


def message(conn, chat_id, direction="in", actor=None):
    iid = conn.execute("INSERT INTO inbox_messages(chat_id, kind, direction, text, actor, created_at) VALUES(?,?,?,?,?,?)",
                       (chat_id, "support", direction, "hi", actor, int(time.time()))).lastrowid
    conn.execute("INSERT INTO inbox_admin_msgs(admin_msg_id, inbox_id) VALUES(?,?)", (iid + 1000, iid))
    return iid


def test_orders_via_defaults_to_web(db):
    conn = db[1]
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(orders)")}
    assert cols["via"][4] == "'web'"


def test_delete_user_removes_their_chat_and_messages(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    chat(conn, 5001, uid)
    message(conn, 5001)
    conn.execute("INSERT INTO telegram_link_codes(code_hash, user_id, expires_at) VALUES('h', ?, ?)", (uid, int(time.time()) + 600))
    dbm.revoke(conn, uid)
    dbm.delete_user(conn, uid)
    for table in ("telegram_chats", "inbox_messages", "inbox_admin_msgs", "telegram_link_codes"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_deleting_an_admin_keeps_the_threads_they_answered(db):
    conn = db[1]
    admin, _ = create_user(conn, "boss", role="admin")
    chat(conn, 5002)
    message(conn, 5002, "out", actor=admin)
    dbm.revoke(conn, admin)
    dbm.delete_user(conn, admin)
    assert conn.execute("SELECT actor FROM inbox_messages").fetchone()[0] is None


def test_cleanup_prunes_codes_updates_and_silent_unlinked_chats(db):
    conn = db[1]
    now = int(time.time())
    uid, _ = create_user(conn, "alice")
    conn.execute("INSERT INTO telegram_link_codes(code_hash, user_id, expires_at) VALUES('old', ?, ?), ('new', ?, ?)",
                 (uid, now - 1, uid, now + 600))
    conn.execute("INSERT INTO telegram_updates(update_id, at) VALUES(1, ?), (2, ?)", (now - 2 * 86400, now))
    chat(conn, 1, None, now - 181 * 86400)   # unlinked, silent: deleted
    message(conn, 1)
    chat(conn, 2, uid, now - 181 * 86400)    # linked: kept
    chat(conn, 3, None, now - 10 * 86400)    # recent: kept
    out = dbm.cleanup(conn, 180)
    assert (out["telegram_link_codes"], out["telegram_updates"], out["telegram_chats"]) == (1, 1, 1)
    assert [r[0] for r in conn.execute("SELECT chat_id FROM telegram_chats ORDER BY chat_id")] == [2, 3]
    assert conn.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()[0] == 0
