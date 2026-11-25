"""The deploy script's rollback restore (deploy/lightsail/claude-gateway-deploy, between RESTORE-BEGIN and -END):
the database goes back to the copy taken before the deploy, keeping what the failed image wrote that matters."""
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "lightsail" / "claude-gateway-deploy"


def _restore_py() -> str:
    block = SCRIPT.read_text().split("# RESTORE-BEGIN", 1)[1].split("# RESTORE-END", 1)[0]
    return re.search(r"<<'PY'\n(.*?)\nPY\n", block, re.S).group(1)


def _run(tmp_path, pre, failed, live):
    return subprocess.run([sys.executable, "-c", _restore_py(), str(pre), str(failed), str(live)],
                          capture_output=True, text=True, cwd=tmp_path)


def _db(path, sql):
    c = sqlite3.connect(path)
    c.executescript(sql)
    c.commit()
    c.close()


OLD = """
CREATE TABLE credentials (backend TEXT, account TEXT, blob TEXT, PRIMARY KEY (backend, account));
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);
CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT);
CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, status TEXT);
CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER);
INSERT INTO credentials VALUES ('oauth', 'default', 'old-token');
INSERT INTO users(name) VALUES ('alice');
INSERT INTO orders(user_id, status) VALUES (1, 'new');
"""


def test_rollback_keeps_what_the_failed_image_wrote(tmp_path):
    pre, live, failed = tmp_path / "pre.db", tmp_path / "live.db", tmp_path / "failed.db"
    _db(pre, OLD)
    # The new image ran on the live database: migrated it (a new column), rotated the token, took a sign-up,
    # an order paid in the window and a payment for the order placed before the deploy.
    _db(live, OLD + """
ALTER TABLE users ADD COLUMN account TEXT;
UPDATE credentials SET blob='rotated-token';
INSERT INTO users(name, account) VALUES ('bob', 'default');
UPDATE orders SET status='paid' WHERE id=1;
INSERT INTO orders(user_id, status) VALUES (2, 'paid');
INSERT INTO payments(order_id, status) VALUES (1, 'verified'), (2, 'verified');
INSERT INTO requests(user_id) VALUES (2);
""")
    r = _run(tmp_path, pre, failed, live)
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(live)
    assert [r[1] for r in c.execute("PRAGMA table_info(users)")] == ["id", "name"]   # the old shape is back
    assert c.execute("SELECT blob FROM credentials").fetchone()[0] == "rotated-token"
    assert c.execute("SELECT name FROM users ORDER BY id").fetchall() == [("alice",), ("bob",)]
    assert c.execute("SELECT id, status FROM orders ORDER BY id").fetchall() == [(1, "paid"), (2, "paid")]
    assert c.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0   # usage history is not carried
    assert "payments" in r.stderr and failed.exists() and not Path(str(live) + ".restore").exists()


def test_carry_failure_still_restores_and_lists_payments(tmp_path):
    pre, live, failed = tmp_path / "pre.db", tmp_path / "live.db", tmp_path / "failed.db"
    # The old shape needs a column the new image's rows lack, so they can't be carried over.
    _db(pre, OLD + "CREATE TABLE tickets (id INTEGER PRIMARY KEY, must TEXT NOT NULL);")
    _db(live, OLD + """
CREATE TABLE tickets (id INTEGER PRIMARY KEY, other TEXT);
INSERT INTO tickets(other) VALUES ('x');
INSERT INTO orders(user_id, status) VALUES (1, 'paid');
INSERT INTO payments(order_id, status) VALUES (2, 'verified');
""")
    r = _run(tmp_path, pre, failed, live)
    assert r.returncode == 0, r.stderr
    assert "COULD NOT CARRY OVER" in r.stderr and "payments only there: [1]" in r.stderr
    c = sqlite3.connect(live)
    assert c.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0   # all-or-nothing: the copy as it was
    assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_missing_copy_leaves_the_live_database(tmp_path):
    live, failed = tmp_path / "live.db", tmp_path / "failed.db"
    _db(live, OLD + "INSERT INTO users(name) VALUES ('bob');")
    r = _run(tmp_path, tmp_path / "absent.db", failed, live)
    assert r.returncode != 0 and "left as it is" in r.stderr
    assert sqlite3.connect(live).execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
