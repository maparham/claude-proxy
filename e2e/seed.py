"""Seeds a fresh gateway database for the end-to-end test and prints the new user's gateway key.

It adds the user `e2e` and the gateway's "subscription" credential. That credential's access token is whatever
E2E_UPSTREAM_TOKEN holds: a stand-in's (with the fake Anthropic), or a key for another gateway that this one then
sends its Claude requests to (with E2E_UPSTREAM_TOKEN a production key, the test's Claude requests are real). It
expires in 30 days, so this gateway never tries to refresh it. Runs where the gateway runs: its config and
CLAUDE_PROXY_CREDENTIAL_KEY.
"""
import os
import time

from claude_proxy import db
from claude_proxy.config import Config
from claude_proxy.credentials import encrypt_blob

cfg = Config.load()
conn = db.init_db(cfg.db.path)
_, key = db.create_user(conn, "e2e", role="user")
expires = int(time.time()) + 30 * 86400
token = os.environ.get("E2E_UPSTREAM_TOKEN") or "e2e-access-token"
conn.execute("INSERT INTO credentials(backend, account, encrypted_blob, expires_at, updated_at, state, label) "
             "VALUES('oauth','default',?,?,?,'active','default') "
             "ON CONFLICT(backend, account) DO UPDATE SET encrypted_blob=excluded.encrypted_blob, "
             "expires_at=excluded.expires_at, updated_at=excluded.updated_at, state=excluded.state",
             (encrypt_blob({"access_token": token, "refresh_token": "none", "expires_at": expires}), expires, int(time.time())))
conn.commit()
print(key)
