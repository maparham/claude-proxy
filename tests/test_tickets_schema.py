import sqlite3

from claude_proxy import tickets
from claude_proxy.config import Config
from claude_proxy.db import init_db
from tests.conftest import make_gateway


def cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_ticket_tables_exist_with_the_spec_columns(db):
    conn = db[1]
    assert cols(conn, "ticket_prices") == {"tier", "length", "usd", "updated_at", "updated_by"}
    assert cols(conn, "ticket_discounts") == {"id", "tier", "length", "usd", "starts_at", "ends_at", "created_by", "created_at", "cancelled_at"}
    assert cols(conn, "fx_rates") == {"id", "currency", "rate", "set_at", "set_by"}
    assert cols(conn, "tickets") >= {"id", "user_id", "user_name", "account_id", "tier", "share_pct", "length", "days", "starts_at",
                                      "ends_at", "list_usd", "usd", "discount_id", "currency", "rate", "amount", "granted_by",
                                      "granted_at", "cancelled_at", "cancelled_by", "note", "ungated_at"}
    assert cols(conn, "ticket_bonuses") == {"id", "ticket_id", "share_pct", "extra_days", "starts_at", "ends_at", "note", "granted_by",
                                            "granted_at", "cancelled_at", "cancelled_by"}
    assert cols(conn, "usage_estimates") == {"computed_at", "family", "bucket", "busy_hours", "p75_share_per_hour"}


def test_init_is_idempotent_on_an_existing_database(tmp_path):
    path = tmp_path / "x.db"
    init_db(path).close()
    conn = init_db(path)   # second start: CREATE IF NOT EXISTS, no error
    conn.execute("INSERT INTO ticket_prices(tier, length, usd, updated_at) VALUES('lite','day',3,0)")
    conn.close()


def test_deleting_a_user_keeps_their_tickets_with_user_id_null(db):
    conn = db[1]
    from claude_proxy.db import create_user
    uid, _ = create_user(conn, "alice")
    conn.execute("INSERT INTO tickets(user_id, user_name, account_id, tier, share_pct, length, days, starts_at, ends_at, list_usd, usd, "
                 "currency, rate, amount, granted_at) VALUES(?,?,1,'lite',5,'day',1,0,86400,3,3,'USD',1,3,0)", (uid, "alice"))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    row = conn.execute("SELECT user_id, user_name FROM tickets").fetchone()
    assert (row["user_id"], row["user_name"]) == (None, "alice")


def test_seed_prices_fills_only_missing_rows(db):
    conn = db[1]
    cfg = Config()
    cfg.tickets.enabled = True
    assert tickets.seed_prices(conn, cfg, now=100) == 6
    conn.execute("UPDATE ticket_prices SET usd=4 WHERE tier='lite' AND length='day'")
    assert tickets.seed_prices(conn, cfg, now=200) == 0
    assert conn.execute("SELECT usd FROM ticket_prices WHERE tier='lite' AND length='day'").fetchone()[0] == 4


def test_gateway_seeds_prices_when_tickets_are_enabled(cfg, db):
    conn = db[1]
    make_gateway(cfg, conn)
    assert conn.execute("SELECT COUNT(*) FROM ticket_prices").fetchone()[0] == 0
    cfg.tickets.enabled = True
    make_gateway(cfg, conn)
    assert conn.execute("SELECT COUNT(*) FROM ticket_prices").fetchone()[0] == 6
