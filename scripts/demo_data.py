"""Fill a database with two weeks of synthetic traffic, for looking at the dashboard.

    CLAUDE_PROXY_CREDENTIAL_KEY=$(claude-proxy keygen) python scripts/demo_data.py demo.db
"""
import math
import random
import sys
import time

from argon2 import PasswordHasher

from claude_proxy import db

path = sys.argv[1] if len(sys.argv) > 1 else "demo.db"
conn = db.init_db(path)
random.seed(7)
now = time.time()
admin, admin_key = db.create_user(conn, "admin", role="admin", password_hash=PasswordHasher().hash("demo-password"))
people = {name: db.create_user(conn, name)[0] for name in ("maya", "omid", "sara")}
people["admin"] = admin
profile = {"maya": (1.0, ["claude-opus-5-5", "claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5"]),
           "omid": (0.6, ["claude-sonnet-5", "claude-sonnet-5", "muse-spark-1.3", "claude-haiku-4-5"]),
           "sara": (0.35, ["claude-opus-5-5", "claude-sonnet-5", "muse-spark-1.3"]),
           "admin": (0.1, ["claude-sonnet-5"])}
rows = []
for name, uid in people.items():
    rate, models = profile[name]
    t = now - 14 * 86400
    session = None
    while t < now:
        hour = time.localtime(t).tm_hour
        busy = 1.0 if 9 <= hour <= 19 else 0.15
        if time.localtime(t).tm_wday >= 5:
            busy *= 0.3
        t += random.expovariate(rate * busy / 240)
        if session is None or random.random() < 0.04:
            session = f"{name}-{int(t)}"
        model = random.choice(models)
        provider = "meta" if model.startswith("muse") else "anthropic"
        cr = int(random.lognormvariate(10.5, 0.8))
        i = int(random.lognormvariate(6, 1.2))
        o = int(random.lognormvariate(6.3, 1.0))
        cw = int(random.lognormvariate(8, 1.2)) if random.random() < 0.3 else 0
        status, err, rej = 200, None, None
        roll = random.random()
        if roll < 0.01:
            status, err = 429, "upstream_throttle"
        elif roll < 0.013:
            status, err, rej = 429, "rate_limit_error", "tokens_daily"
        elif roll < 0.016:
            status, err = 529, "overloaded_error"
        rows.append((uid, t, t + random.uniform(2, 40), "POST", "/v1/messages", provider, model, status, 1, 1,
                     0 if rej else i, 0 if rej else o, 0 if rej else cw, 0 if rej else cw, 0, 0 if rej else cr, session, err, rej))
conn.executemany("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status, stream, complete, "
                 "input_tokens, output_tokens, cache_creation_tokens, cache_creation_5m, cache_creation_1h, cache_read_tokens, "
                 "session_id, error_type, rejected_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
# Account utilization that rises with traffic and resets every 5 h / 7 d.
for bucket, period, scale in (("5h", 5 * 3600, 0.05), ("7d", 7 * 86400, 0.004)):
    t = now - 14 * 86400
    while t < now:
        start = t - (t % period)
        used = sum(1 for r in rows if start <= r[2] <= t and r[5] == "anthropic")
        util = min(100.0, round(used * scale * 10))
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                     (t, "header", bucket, util, int(start + period)))
        t += 300
conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,?)", (people["omid"], "tokens_daily", "*", "900000", "weighted", int(now)))
conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,?)", (people["sara"], "share_5h", "*", "25", "pct", int(now)))
conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,?)", (people["sara"], "requests_daily", "claude-opus-*", "150", "count", int(now)))
conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,?)", (people["maya"], "cost_monthly", "*", "400", "usd", int(now)))
db.audit(conn, admin, "limit_set", "omid:tokens_daily:*", {"value": "900000"})
print(f"{len(rows)} requests written to {path}. Dashboard login: admin / demo-password")
