# OpenCode Routes and Client Side Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let people use OpenCode through the gateway for third-party models (Muse) on a second, routes-only key that can never reach the Claude subscription, and warn Claude Code users before a limit is hit.

**Architecture:** A per-user routes-only key (two new `users` columns) is checked by one function, `scope_allows`, at the top of the proxy handler, before limits and before the subscription credential is loaded. Route models gain admin-supplied `model_info` (context/output sizes) exposed through `/v1/models`. On the client side, `scripts/claude-gateway` learns `--opencode` (writes OpenCode's provider config) and installs a `UserPromptSubmit` hook that is a new `--warn` mode of `scripts/statusline.sh`.

**Tech Stack:** Python 3.12, FastAPI, httpx, SQLite, pytest (+ pytest-asyncio, `asyncio_mode = "auto"`), POSIX sh + awk (statusline), bash + embedded python3 (claude-gateway), vanilla JS dashboard.

**Spec:** `docs/superpowers/specs/2026-09-25-client-side-and-opencode-routes-design.md`. Read it first; this plan argues from it.

## Global Constraints

- Run tests with `.venv/bin/python -m pytest` from the repo root. The suite passes (177 tests) before Task 2; it must pass after every task.
- No new Python or JS dependencies.
- Routes-only keys start `sk-proxy-r-`; full keys keep `sk-proxy-`. Prefix shown = first `len(prefix) + 3` characters (12 for full keys, 14 for routes-only keys). Scope comes from which column matched, never from the key text.
- Proxy refusal: HTTP 403, `{"type": "error", "error": {"type": "permission_error", "message": ...}}`, message exactly `This key is for third-party models only (<patterns>). Claude models need your Claude Code key.` where `<patterns>` is the configured route globs joined by `, ` (default `muse-spark*`). Recorded with `rejected_by="key_scope"`, `error_type="permission_error"`.
- Dashboard refusal for a routes-only key: 403 `This key only works for third-party models; use your Claude Code key for the dashboard.`; on `/api/login/key`: 403 `This key only works for third-party models; sign in with your Claude Code key.`
- Errors panel kind for `rejected_by='key_scope'`: `gateway_key_scope`.
- Audit actions: `routes_key_issue`, `routes_key_replace`, `routes_key_remove`, target = user name.
- Default `muse-spark` model info: `context = 1048576`, `output = 32000`, `display_name = "Muse Spark 1.3"`.
- Warning hook: triggers at 80% (band 1) and 100% (band 2); repeats after 15 minutes (900 s) or on entering a higher band; reuses the statusline's 30-second cache and 3-second timeout; stdout carries only the one JSON line; exit 0 on every path in `--warn` mode.
- Commit messages follow the repo's style (`Area: what changed`, sentence case, no `feat:` prefixes) and end with the trailer `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. Work on branch `opencode-routes`.

## Review Focus

1. **OpenCode sends the key in `x-api-key`, Claude Code in `Authorization: Bearer`.** A routes-only key must be accepted and scoped identically in both headers. Pinned in Task 4 (`test_routes_key_reaches_muse_with_the_route_key`, parametrized over both headers).
2. **A disabled or revoked user must lose both keys at once**, not just the full key. Pinned in Task 4 (`test_disabling_or_revoking_stops_both_keys`).
3. **A person edits `opencode.json` between `on --opencode` and `off --opencode`** (changes a theme, adds a provider). `off` must keep those edits and remove only the gateway provider. Pinned in Task 10 (`test_opencode_off_restores_config_and_keeps_later_edits`).
4. **The status line has no figures at all** (a person with no limits: `alice`). The warning hook must stay silent, not warn on a parse accident. Pinned in Task 8 (`test_warn_silent_without_figures`).
5. **Re-running `on --opencode` after the admin adds a route**, with a person-edited agent file. The new model must appear, the edited agent must survive, and `off` must not delete the edited agent. Pinned in Task 10 (`test_opencode_rerun_refreshes_models_and_spares_an_edited_agent`).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/claude_proxy/db.py` | modify | schema columns + index, key generation, key lookup returning scope, issue/remove routes key |
| `src/claude_proxy/config.py` | modify | `Route.model_info`, its validation, `listed_models()`, default Muse sizes |
| `src/claude_proxy/app.py` | modify | `scope_allows`, `route_model_entries`, scope check in `handle`, local model list |
| `src/claude_proxy/web.py` | modify | `principal(routes_ok=)`, `/api/login/key` check, `routes_prefix`, admin actions, `ERROR_KIND` |
| `src/claude_proxy/cli.py` | modify | `claude-proxy user routes-key`, `user list` column |
| `src/claude_proxy/static/app.js` | modify | OpenCode key buttons, key dialog text, error label/tip |
| `scripts/statusline.sh` | modify | `--warn` mode, shared figure parsing |
| `scripts/claude-gateway` | modify | warning hook install/remove, `--opencode` on/off/status |
| `examples/opencode/muse.md` | create | OpenCode-format Muse subagent |
| `config.example.toml` | modify | `model_info` example |
| `README.md` | modify | onboarding an OpenCode user, warning hook |
| `tests/test_routes_keys.py` | create | DB-level routes key tests |
| `tests/test_route_models.py` | create | `model_info` config and `/v1/models` entries |
| `tests/test_routes_proxy.py` | create | proxy enforcement |
| `tests/test_routes_web.py` | create | dashboard behaviour for routes keys |
| `tests/test_routes_cli.py` | create | CLI command |
| `tests/test_client_scripts.py` | create | statusline `--warn` and claude-gateway, run as subprocesses against a stub gateway |

---

### Task 1: Verify the unverified (spike, no product code)

The design rests on four facts taken from documentation or a minified binary. Verify them and record the results in the spec. **If check A, B or C fails, stop and report to the user before any other task: the OpenCode half of the design would need to change.** Check D only changes wording.

**Files:**
- Modify: `docs/superpowers/specs/2026-09-25-client-side-and-opencode-routes-design.md` (sections 7 and 8 only)
- Scratch (not committed): `/tmp/cg-probe/`

- [ ] **Step 1: Start a recording server**

```bash
mkdir -p /tmp/cg-probe && cat > /tmp/cg-probe/recorder.py <<'EOF'
"""Logs every request (method, path, headers) as JSON lines to /tmp/cg-probe/requests.log and answers like the gateway."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = "/tmp/cg-probe/requests.log"
MODELS = {"data": [{"type": "model", "id": "muse-spark", "display_name": "Muse Spark 1.3",
                    "created_at": "2026-01-01T00:00:00Z", "max_input_tokens": 1048576, "max_tokens": 32000}],
          "has_more": False, "first_id": "muse-spark", "last_id": "muse-spark"}
MESSAGE = {"id": "msg_1", "type": "message", "role": "assistant", "model": "muse-spark",
           "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
           "usage": {"input_tokens": 5, "output_tokens": 1}}

def sse():
    events = [("message_start", {"type": "message_start", "message": {**MESSAGE, "content": [], "stop_reason": None}}),
              ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
              ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
              ("content_block_stop", {"type": "content_block_stop", "index": 0}),
              ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
              ("message_stop", {"type": "message_stop"})]
    return b"".join(f"event: {e}\ndata: {json.dumps(d)}\n\n".encode() for e, d in events)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _log(self, body=b""):
        with open(LOG, "a") as f:
            f.write(json.dumps({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                "body": body[:2000].decode(errors="replace")}) + "\n")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._log()
        if self.path.startswith("/v1/models"):
            return self._send(200, json.dumps(MODELS).encode(), "application/json")
        self._send(404, b"{}", "application/json")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        self._log(body)
        try:
            stream = json.loads(body).get("stream")
        except ValueError:
            stream = False
        if stream:
            return self._send(200, sse(), "text/event-stream")
        self._send(200, json.dumps(MESSAGE).encode(), "application/json")

ThreadingHTTPServer(("127.0.0.1", 18099), H).serve_forever()
EOF
rm -f /tmp/cg-probe/requests.log
```

Then run it in the background (Bash tool with `run_in_background: true`): `python3 /tmp/cg-probe/recorder.py`

- [ ] **Step 2: Build an isolated OpenCode config with both `opencode.jsonc` and `opencode.json`**

This uses `XDG_CONFIG_HOME` so the person's real `~/.config/opencode` is never touched.

```bash
P=/tmp/cg-probe
mkdir -p $P/config/opencode $P/data $P/cache $P/state $P/project
printf 'sk-proxy-r-probe' > $P/routes.key        # no trailing newline, as claude-gateway writes it
cat > $P/config/opencode/opencode.jsonc <<'EOF'
{
  // a comment, so this file is only valid as JSONC
  "$schema": "https://opencode.ai/config.json"
}
EOF
cat > $P/config/opencode/opencode.json <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "gateway": {
      "npm": "@ai-sdk/anthropic",
      "name": "Claude gateway",
      "options": { "baseURL": "http://127.0.0.1:18099/v1", "apiKey": "{file:$P/routes.key}" },
      "models": { "muse-spark": { "name": "Muse Spark 1.3", "limit": { "context": 1048576, "output": 32000 } } }
    }
  }
}
EOF
```

- [ ] **Step 3: Check A — OpenCode merges `opencode.json` with an existing `opencode.jsonc`**

```bash
P=/tmp/cg-probe
cd $P/project && XDG_CONFIG_HOME=$P/config XDG_DATA_HOME=$P/data XDG_CACHE_HOME=$P/cache XDG_STATE_HOME=$P/state \
  perl -e 'alarm 120; exec @ARGV' opencode models gateway
```

Expected: output lists `gateway/muse-spark`. If it does not, check A fails.

- [ ] **Step 4: Checks B and C — a custom provider accepts `@ai-sdk/anthropic`; `{file:}` with an absolute path works; the key goes in `x-api-key`**

```bash
P=/tmp/cg-probe
cd $P/project && XDG_CONFIG_HOME=$P/config XDG_DATA_HOME=$P/data XDG_CACHE_HOME=$P/cache XDG_STATE_HOME=$P/state \
  perl -e 'alarm 180; exec @ARGV' opencode run -m gateway/muse-spark "Reply with the single word ok" > $P/run.log 2>&1; echo "exit $?"
python3 - <<'EOF'
import json
reqs = [json.loads(l) for l in open("/tmp/cg-probe/requests.log")]
msgs = [r for r in reqs if r["method"] == "POST" and r["path"].startswith("/v1/messages")]
print("message requests:", len(msgs))
for r in msgs[:1]:
    h = {k.lower(): v for k, v in r["headers"].items()}
    print("path:", r["path"])
    print("x-api-key:", repr(h.get("x-api-key")))
    print("authorization:", repr(h.get("authorization")))
    print("anthropic-beta:", h.get("anthropic-beta"))
EOF
```

Expected: at least one message request; `x-api-key: 'sk-proxy-r-probe'` exactly (no newline, no `{file:` text); path `/v1/messages`. Check B fails if no request arrived because the provider package was rejected (see `$P/run.log`). Check C fails if the key is missing, contains `{file:`, or ends with `\n`. If the key arrives in `authorization: Bearer ...` instead, that still works with the gateway (it accepts both); record it and continue.

- [ ] **Step 5: Check D — does Claude Code in key-only mode ever fetch `/api/oauth/usage`?**

Claude Code writes its debug log (including `fetchUtilization: GET ...` lines) when started with `--debug`. Drive an interactive session through a pty so `/usage` can be typed. `CLAUDE_CONFIG_DIR` isolates it from the person's own settings (whose `env` block may point at the real gateway).

```bash
P=/tmp/cg-probe; mkdir -p $P/cc
cat > $P/drive.py <<'EOF'
import os, pty, subprocess, sys, time
env = {**os.environ, "CLAUDE_CONFIG_DIR": "/tmp/cg-probe/cc", "ANTHROPIC_BASE_URL": "http://127.0.0.1:18099",
       "ANTHROPIC_AUTH_TOKEN": "sk-proxy-probe"}
env.pop("ANTHROPIC_API_KEY", None)
master, slave = pty.openpty()
p = subprocess.Popen(["claude", "--debug"], stdin=slave, stdout=slave, stderr=slave, env=env, cwd="/tmp/cg-probe/project")
out = open("/tmp/cg-probe/claude-tty.log", "wb")
def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            import select
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                out.write(os.read(master, 65536))
        except OSError:
            return
pump(8)
for keys in (b"\r", b"/usage\r"):      # first Enter dismisses any trust/onboarding prompt
    os.write(master, keys); pump(6)
os.write(master, b"\x1b"); pump(1)
os.write(master, b"/exit\r"); pump(3)
p.kill()
EOF
python3 $P/drive.py
grep -rh "fetchUtilization\|api/oauth/usage" $P/cc/debug 2>/dev/null | head -5; echo "---"; grep -c "oauth/usage" $P/requests.log || true
```

Expected (spec section 7's claim holds): no `fetchUtilization` line and no `/api/oauth/usage` request. If the onboarding flow in the isolated config dir blocks the session (the tty log shows a theme or login picker), record "not confirmed: the isolated session did not reach the prompt" instead; do not spend more than one more attempt.

- [ ] **Step 6: Record the results in the spec**

Replace the last sentence of section 7 ("This is from reading the binary, not from running it; plan task 1 confirms it with the recording upstream and records the result here.") with the observed result and the Claude Code version (`claude --version`), for example: `Confirmed on 2026-09-XX with Claude Code 2.1.282: with --debug and /usage typed in key-only mode, no fetchUtilization call was logged.` In section 8, replace the "Unverified until plan task 1" bullet with what Steps 3–4 showed (OpenCode version from `opencode --version`, header used, `{file:}` result, jsonc merge result).

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-09-25-client-side-and-opencode-routes-design.md
git commit -m "Spec: record OpenCode and /usage verification results

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

Stop the recorder (TaskStop on its background task).

---

### Task 2: Routes-only keys in the database

**Files:**
- Modify: `src/claude_proxy/db.py` (SCHEMA users table, `_migrate`, `generate_virtual_key`, `find_user_by_key`, `find_session`; add `set_routes_key`, `remove_routes_key`)
- Test: `tests/test_routes_keys.py`

**Interfaces:**
- Produces:
  - `db.ROUTES_KEY_PREFIX = "sk-proxy-r-"`
  - `db.generate_virtual_key(prefix: str = "sk-proxy-") -> tuple[str, str, str]` (raw, hash, display prefix)
  - `db.find_user_by_key(conn, raw_key) -> dict | None` — a dict copy of the user row plus `"key_scope": "full" | "routes"`
  - `db.find_session(conn, raw_token) -> dict | None` — dict copy plus `"key_scope": "full"`
  - `db.set_routes_key(conn, user_id: int, actor: int | None = None) -> str` — raises `ValueError` for a revoked or unknown user
  - `db.remove_routes_key(conn, user_id: int, actor: int | None = None) -> bool`
  - `users.routes_key_hash TEXT`, `users.routes_key_prefix TEXT`, unique index `idx_users_routes_key`
- `auth.authenticate` needs no code change: it returns whatever `find_user_by_key` returns, so every caller now gets a dict with `key_scope`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routes_keys.py`:

```python
"""Routes-only keys (spec 2.1-2.2): a second key per person that only reaches third-party routes."""
import sqlite3

import pytest

from claude_proxy.db import (create_user, find_session, create_session, find_user_by_key, init_db, remove_routes_key,
                             revoke, rotate_key, set_routes_key)


def audit_actions(conn, target):
    return [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE target=? ORDER BY id", (target,))]


def test_each_key_maps_to_its_user_with_its_scope(db):
    conn = db[1]
    uid, full = create_user(conn, "alice")
    routes = set_routes_key(conn, uid)
    assert routes.startswith("sk-proxy-r-")
    assert find_user_by_key(conn, full)["key_scope"] == "full"
    u = find_user_by_key(conn, routes)
    assert (u["id"], u["name"], u["key_scope"]) == (uid, "alice", "routes")
    assert u["routes_key_prefix"] == routes[:14]
    assert find_user_by_key(conn, "sk-proxy-r-not-a-key") is None


def test_replacing_and_removing_the_routes_key(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    first = set_routes_key(conn, uid)
    second = set_routes_key(conn, uid)
    assert find_user_by_key(conn, first) is None
    assert find_user_by_key(conn, second)["id"] == uid
    assert remove_routes_key(conn, uid) is True
    assert find_user_by_key(conn, second) is None
    assert remove_routes_key(conn, uid) is False
    assert audit_actions(conn, "alice") == ["routes_key_issue", "routes_key_replace", "routes_key_remove"]


def test_rotating_one_key_leaves_the_other_working(db):
    conn = db[1]
    uid, full = create_user(conn, "alice")
    routes = set_routes_key(conn, uid)
    new_full = rotate_key(conn, uid)
    assert find_user_by_key(conn, full) is None
    assert find_user_by_key(conn, routes)["key_scope"] == "routes"
    set_routes_key(conn, uid)
    assert find_user_by_key(conn, new_full)["key_scope"] == "full"


def test_revoked_user_gets_no_routes_key(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    revoke(conn, uid)
    with pytest.raises(ValueError, match="revoked"):
        set_routes_key(conn, uid)


def test_two_users_cannot_share_a_routes_key_hash(db):
    conn = db[1]
    a, _ = create_user(conn, "alice")
    b, _ = create_user(conn, "bob")
    conn.execute("UPDATE users SET routes_key_hash='same' WHERE id=?", (a,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE users SET routes_key_hash='same' WHERE id=?", (b,))


def test_dashboard_session_user_has_full_scope(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    raw, _ = create_session(conn, uid)
    u = find_session(conn, raw)
    assert (u["id"], u["key_scope"]) == (uid, "full") and u["csrf_token"]


OLD_USERS = ("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
             "role TEXT NOT NULL CHECK(role IN ('admin','user')), key_hash TEXT NOT NULL UNIQUE, key_prefix TEXT NOT NULL, "
             "enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, revoked_at INTEGER, password_hash TEXT)")


def test_migration_adds_the_columns_and_index_to_an_old_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(OLD_USERS)
    old.execute("INSERT INTO users(name, role, key_hash, key_prefix, created_at) VALUES('alice','user','h','sk-proxy-abc',0)")
    old.commit()
    old.close()
    conn = init_db(path)
    assert {"routes_key_hash", "routes_key_prefix"} <= {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    assert "idx_users_routes_key" in {r[1] for r in conn.execute("PRAGMA index_list(users)")}
    assert find_user_by_key(conn, set_routes_key(conn, 1))["name"] == "alice"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_routes_keys.py -q`
Expected: collection error `ImportError: cannot import name 'remove_routes_key'`.

- [ ] **Step 3: Implement**

In `src/claude_proxy/db.py`, change the end of the `users` table in `SCHEMA` from

```python
        revoked_at INTEGER,
        password_hash TEXT
    )""",
```

to

```python
        revoked_at INTEGER,
        password_hash TEXT,
        routes_key_hash TEXT,       -- the routes-only key (OpenCode): third-party models only
        routes_key_prefix TEXT
    )""",
```

At the end of `_migrate`, add:

```python
    if "routes_key_hash" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN routes_key_hash TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN routes_key_prefix TEXT")
    # SQLite can't ADD COLUMN ... UNIQUE; a unique index gives the same guarantee, and NULLs never collide in it.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_routes_key ON users(routes_key_hash)")
```

Replace `generate_virtual_key` with:

```python
FULL_KEY_PREFIX = "sk-proxy-"
ROUTES_KEY_PREFIX = "sk-proxy-r-"   # only for people to tell the keys apart; scope comes from the column that matched


def generate_virtual_key(prefix: str = FULL_KEY_PREFIX) -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix). The raw key is shown once and never stored."""
    raw = prefix + secrets.token_urlsafe(32)
    return raw, hash_key(raw), raw[:len(prefix) + 3]
```

Replace `find_user_by_key` with:

```python
def find_user_by_key(conn: sqlite3.Connection, raw_key: str) -> dict | None:
    """The user a gateway key belongs to, with `key_scope`: "full" for their Claude Code key, "routes" for their
    routes-only key, which may only reach third-party routes (app.scope_allows)."""
    h = hash_key(raw_key)
    row = conn.execute("SELECT * FROM users WHERE key_hash=? OR routes_key_hash=?", (h, h)).fetchone()
    if row is None:
        return None
    return {**dict(row), "key_scope": "full" if row["key_hash"] == h else "routes"}
```

Change the return of `find_session` so it also returns a dict:

```python
    row = conn.execute(
        "SELECT u.*, s.csrf_token AS csrf_token FROM users u JOIN sessions s ON s.user_id=u.id "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1 AND u.revoked_at IS NULL",
        (hash_key(raw_token), int(time.time())),
    ).fetchone()
    return {**dict(row), "key_scope": "full"} if row else None
```

and its annotation to `-> dict | None`. After `rotate_key`, add:

```python
def set_routes_key(conn: sqlite3.Connection, user_id: int, actor: int | None = None) -> str:
    """Issue or replace a user's routes-only key. Returns the raw key, shown once."""
    u = conn.execute("SELECT revoked_at, routes_key_hash FROM users WHERE id=?", (user_id,)).fetchone()
    if u is None or u["revoked_at"] is not None:
        raise ValueError("No such user, or the user is revoked.")
    raw, h, prefix = generate_virtual_key(ROUTES_KEY_PREFIX)
    conn.execute("UPDATE users SET routes_key_hash=?, routes_key_prefix=? WHERE id=?", (h, prefix, user_id))
    audit(conn, actor, "routes_key_replace" if u["routes_key_hash"] else "routes_key_issue", _user_name(conn, user_id))
    return raw


def remove_routes_key(conn: sqlite3.Connection, user_id: int, actor: int | None = None) -> bool:
    """Delete a user's routes-only key. False when they had none."""
    n = conn.execute("UPDATE users SET routes_key_hash=NULL, routes_key_prefix=NULL "
                     "WHERE id=? AND routes_key_hash IS NOT NULL", (user_id,)).rowcount
    if n:
        audit(conn, actor, "routes_key_remove", _user_name(conn, user_id))
    return bool(n)
```

- [ ] **Step 4: Run the new tests and the whole suite**

Run: `.venv/bin/python -m pytest tests/test_routes_keys.py -q && .venv/bin/python -m pytest -q`
Expected: all pass (7 new; the existing 177 still pass, because every caller reads the user by column name and a dict supports the same `u["..."]` and `.keys()`).

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/db.py tests/test_routes_keys.py
git commit -m "Keys: a second, routes-only key per user

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Route model metadata and the model list entries

**Files:**
- Modify: `src/claude_proxy/config.py` (`Route`, `_default_routes`)
- Modify: `src/claude_proxy/app.py` (add `route_model_entries`; use it in `_models_with_routes`)
- Modify: `config.example.toml`
- Test: `tests/test_route_models.py`

**Interfaces:**
- Produces:
  - `Route.model_info: dict[str, dict]` — per listed model, keys among `context`, `output` (positive ints, both or neither), `display_name` (str)
  - `Route.listed_models() -> list[str]` — `model_map` keys then `model_info` keys, de-duplicated, in order
  - `app.route_model_entries(cfg: Config) -> list[dict]` — Anthropic-shaped model objects: `type`, `id`, `display_name`, `created_at`, and `max_input_tokens`/`max_tokens` when sizes are known
  - Invalid `model_info` raises `TypeError` from `Route(...)`, which `Config.load` already turns into `ConfigError("<file>: bad [[routes]] entry: ...")`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_route_models.py`:

```python
"""Route model metadata (spec 3): what /v1/models lists for third-party routes, and the sizes OpenCode needs."""
import pytest
from fastapi.responses import JSONResponse

from claude_proxy.app import create_app, route_model_entries
from claude_proxy.config import Config, ConfigError, Route
from tests.conftest import asgi_client
from tests.test_proxy import setup  # noqa: F401  (fixture)

MUSE = {"type": "model", "id": "muse-spark", "display_name": "Muse Spark 1.3", "created_at": "2026-01-01T00:00:00Z",
        "max_input_tokens": 1048576, "max_tokens": 32000}


def route(**kw):
    return Route(name="x", base_url="http://x", api_key_env="X", models=["m*"], **kw)


def test_listed_models_are_the_map_and_info_keys_in_order():
    r = route(model_map={"a": "a-1"}, model_info={"b": {"context": 10, "output": 5}, "a": {"display_name": "A"}})
    assert r.listed_models() == ["a", "b"]


def test_default_muse_entry_carries_its_sizes():
    assert route_model_entries(Config()) == [MUSE]


def test_entry_without_sizes_has_no_size_fields():
    cfg = Config()
    cfg.routes[0].model_info = {}
    assert route_model_entries(cfg) == [{"type": "model", "id": "muse-spark", "display_name": "muse-spark (via meta)",
                                         "created_at": "2026-01-01T00:00:00Z"}]


def test_a_glob_only_route_lists_nothing():
    cfg = Config()
    cfg.routes.append(Route(name="other", base_url="http://o", api_key_env="O", models=["foo-*"]))
    assert [m["id"] for m in route_model_entries(cfg)] == ["muse-spark"]


@pytest.mark.parametrize("info,error", [
    ({"m1": {"context": 1000}}, "both context and output"),
    ({"m1": {"context": 1000, "output": 0}}, "positive whole number"),
    ({"m1": {"context": "1M", "output": 10}}, "positive whole number"),
    ({"m1": {"context": True, "output": 10}}, "positive whole number"),
    ({"m1": {"ctx": 1}}, "unknown keys"),
])
def test_bad_model_info_is_refused(info, error):
    with pytest.raises(TypeError, match=error):
        route(model_info=info)


def test_bad_model_info_in_a_config_file_is_a_config_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[[routes]]\nname = "meta"\nbase_url = "http://x"\napi_key_env = "X"\nmodels = ["m*"]\n'
                 'model_info = { m1 = { context = 1000 } }\n')
    with pytest.raises(ConfigError, match="both context and output"):
        Config.load(str(p))


async def test_full_key_model_list_appends_route_models_with_sizes(setup, anthropic):  # noqa: F811
    gw, conn, uid, h = setup
    anthropic.default = lambda req: JSONResponse({"data": [{"type": "model", "id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"}],
                                                  "has_more": False, "first_id": "claude-sonnet-5", "last_id": "claude-sonnet-5"})
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models", headers=h)
    assert r.json()["data"][1] == MUSE
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_route_models.py -q`
Expected: `ImportError: cannot import name 'route_model_entries'`.

- [ ] **Step 3: Implement `Route.model_info`**

In `src/claude_proxy/config.py`, in `class Route`, after `auth_header`, add the field and methods:

```python
    # Per listed model name: {"context": tokens, "output": tokens, "display_name": str}. Supplied by the admin;
    # /v1/models reports the sizes so OpenCode knows the real context window (spec 3.1).
    model_info: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        for name, info in self.model_info.items():
            unknown = set(info) - {"context", "output", "display_name"}
            if unknown:
                raise TypeError(f"model_info[{name!r}]: unknown keys {sorted(unknown)}")
            if ("context" in info) != ("output" in info):
                raise TypeError(f"model_info[{name!r}]: give both context and output, or neither")
            for k in ("context", "output"):
                v = info.get(k)
                if k in info and (isinstance(v, bool) or not isinstance(v, int) or v <= 0):
                    raise TypeError(f"model_info[{name!r}].{k} must be a positive whole number of tokens")

    def listed_models(self) -> list[str]:
        """Globs can't be enumerated, so the models /v1/models lists are the model_map and model_info keys.
        A model that matches `models` but is in neither is still routed, just not listed."""
        return list(dict.fromkeys([*self.model_map, *self.model_info]))
```

In `_default_routes`, add to the `Route(...)` call:

```python
        # 1M window as listed for muse-spark-1.3 by Promptfoo's Meta provider docs and OpenRouter; Meta publishes
        # no output limit for 1.3, so 32000 is a conservative cap.
        model_info={"muse-spark": {"context": 1048576, "output": 32000, "display_name": "Muse Spark 1.3"}},
```

- [ ] **Step 4: Implement `route_model_entries` and use it**

In `src/claude_proxy/app.py`, change `from .config import Route` to `from .config import Config, Route`. Above `create_app`, add:

```python
def route_model_entries(cfg: Config) -> list[dict]:
    """The third-party models /v1/models lists, in Anthropic's shape (spec 3.2)."""
    out = []
    for route in cfg.routes:
        for name in route.listed_models():
            info = route.model_info.get(name, {})
            m = {"type": "model", "id": name, "display_name": info.get("display_name") or f"{name} (via {route.name})",
                 "created_at": "2026-01-01T00:00:00Z"}
            if "context" in info:
                m["max_input_tokens"], m["max_tokens"] = info["context"], info["output"]
            out.append(m)
    return out
```

In `_models_with_routes`, replace

```python
        for route in gw.cfg.routes:
            for name in route.model_map:
                if name not in ids:
                    data["data"].append({"type": "model", "id": name, "display_name": f"{name} (via {route.name})",
                                         "created_at": "2026-01-01T00:00:00Z"})
```

with

```python
        data["data"].extend(m for m in route_model_entries(gw.cfg) if m["id"] not in ids)
```

- [ ] **Step 5: Document it in `config.example.toml`**

After the `drop_body_fields = []` line of the `[[routes]]` example, add:

```toml
# Sizes /v1/models reports for each listed model (OpenCode uses them); give context and output together.
# Routes match by glob, so /v1/models lists only the model_map and model_info keys.
model_info = { "muse-spark" = { context = 1048576, output = 32000, display_name = "Muse Spark 1.3" } }
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_route_models.py -q && .venv/bin/python -m pytest -q`
Expected: all pass. `test_models_list_includes_route_models` in `tests/test_proxy.py` still passes (it checks ids only).

- [ ] **Step 7: Commit**

```bash
git add src/claude_proxy/config.py src/claude_proxy/app.py config.example.toml tests/test_route_models.py
git commit -m "Routes: model_info sizes, reported in /v1/models

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Enforce the key scope on the proxy

**Files:**
- Modify: `src/claude_proxy/app.py` (`scope_allows`, `_route_patterns`, the check in `handle`)
- Test: `tests/test_routes_proxy.py`

**Interfaces:**
- Consumes: `db.set_routes_key` (Task 2); `user["key_scope"]` from `authenticate` (Task 2); `route_model_entries(cfg)` (Task 3).
- Produces: `app.scope_allows(key_scope: str, method: str, path: str, route: Route | None) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routes_proxy.py`:

```python
"""Routes-only keys on the proxy (spec 2.3): third-party routes and the model list, nothing else, ever."""
import time

import pytest

from claude_proxy.app import create_app, scope_allows
from claude_proxy.config import Route
from claude_proxy.db import create_user, revoke, set_enabled, set_routes_key
from tests.conftest import asgi_client, make_gateway, seed_oauth, sse_response

MSG = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
MUSE = {**MSG, "model": "muse-spark"}
ROUTE = Route(name="meta", base_url="http://meta.fake", api_key_env="META_API_KEY", models=["muse-spark*"])
REFUSAL = "This key is for third-party models only (muse-spark*). Claude models need your Claude Code key."


@pytest.fixture
def env(cfg, db, anthropic, meta, monkeypatch):
    conn = db[1]
    uid, full = create_user(conn, "alice")
    routes = set_routes_key(conn, uid)
    seed_oauth(conn, access="oauth-secret-token")
    cfg.routes[0].base_url = "http://meta.fake"
    monkeypatch.setenv("META_API_KEY", "meta-key-123")
    gw = make_gateway(cfg, conn, anthropic, meta)
    return gw, conn, uid, {"authorization": f"Bearer {full}"}, {"x-api-key": routes}


def last_request(conn):
    return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()


def forbid_credential(gw, monkeypatch):
    async def boom():
        raise AssertionError("a routes-only key made the gateway load the subscription credential")
    monkeypatch.setattr(gw.backend, "upstream_headers", boom)


@pytest.mark.parametrize("scope,method,path,routed,allowed", [
    ("full", "POST", "/v1/messages", False, True),
    ("full", "GET", "/v1/anything", False, True),
    ("routes", "POST", "/v1/messages", True, True),
    ("routes", "POST", "/v1/messages/count_tokens", True, True),
    ("routes", "GET", "/v1/models", False, True),
    ("routes", "POST", "/v1/messages", False, False),
    ("routes", "POST", "/v1/messages/count_tokens", False, False),
    ("routes", "POST", "/v1/models", False, False),
    ("routes", "HEAD", "/v1/models", False, False),
    ("routes", "GET", "/v1/messages", False, False),
    ("routes", "GET", "/v1/foo", False, False),
    ("routes", "POST", "/v1/foo", True, False),
])
def test_scope_allows(scope, method, path, routed, allowed):
    assert scope_allows(scope, method, path, ROUTE if routed else None) is allowed


async def test_claude_on_a_routes_key_is_refused_before_anything_upstream(env, anthropic, monkeypatch):
    gw, conn, uid, full, routes = env
    forbid_credential(gw, monkeypatch)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json=MSG, headers=routes)
    assert r.status_code == 403
    assert r.json() == {"type": "error", "error": {"type": "permission_error", "message": REFUSAL}}
    assert anthropic.calls == []
    row = last_request(conn)
    assert (row["user_id"], row["status"], row["rejected_by"]) == (uid, 403, "key_scope")


async def test_scope_is_checked_before_limits(env, anthropic):
    gw, conn, uid, full, routes = env
    now = time.time()
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (uid, "requests_daily", "claude-*", "1", "count"))
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status) VALUES(?,?,?,?,?,?,?,?)",
                 (uid, now - 10, now - 9, "POST", "/v1/messages", "anthropic", "claude-sonnet-5", 200))
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json=MSG, headers=full)).status_code == 429
        r = await c.post("/v1/messages", json=MSG, headers=routes)
    assert r.status_code == 403 and last_request(conn)["rejected_by"] == "key_scope"
    assert anthropic.calls == []


async def test_count_tokens_for_claude_on_a_routes_key_is_refused(env, anthropic, monkeypatch):
    gw, conn, uid, full, routes = env
    forbid_credential(gw, monkeypatch)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages/count_tokens", json=MSG, headers=routes)
    assert r.status_code == 403 and anthropic.calls == []


async def test_model_list_on_a_routes_key_is_local_and_route_only(env, anthropic, monkeypatch):
    gw, conn, uid, full, routes = env
    forbid_credential(gw, monkeypatch)
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models?limit=1000", headers=routes)
    assert r.status_code == 200
    assert r.json() == {"data": [{"type": "model", "id": "muse-spark", "display_name": "Muse Spark 1.3",
                                  "created_at": "2026-01-01T00:00:00Z", "max_input_tokens": 1048576, "max_tokens": 32000}],
                        "has_more": False, "first_id": "muse-spark", "last_id": "muse-spark"}
    assert anthropic.calls == []
    assert (last_request(conn)["provider"], last_request(conn)["status"]) == ("gateway", 200)


@pytest.mark.parametrize("method,path", [("GET", "/v1/foo"), ("POST", "/v1/models"), ("GET", "/v1/messages")])
async def test_other_paths_on_a_routes_key_are_refused(env, anthropic, method, path):
    gw, conn, uid, full, routes = env
    async with asgi_client(create_app(gw)) as c:
        r = await c.request(method, path, headers=routes)
    assert r.status_code == 403 and anthropic.calls == []


async def test_unreadable_body_on_a_routes_key_is_a_scope_refusal(env, anthropic):
    gw, conn, uid, full, routes = env
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", content=b"not json", headers={**routes, "content-type": "application/json"})
    assert r.status_code == 403 and last_request(conn)["rejected_by"] == "key_scope"


@pytest.mark.parametrize("header", ["x-api-key", "authorization"])
async def test_routes_key_reaches_muse_with_the_route_key(env, anthropic, meta, header):
    gw, conn, uid, full, routes = env
    key = routes["x-api-key"]
    h = {"x-api-key": key} if header == "x-api-key" else {"authorization": f"Bearer {key}"}
    meta.default = lambda req: sse_response(model="muse-spark-1.3", input_tokens=50, output_tokens=20)
    async with asgi_client(create_app(gw)) as c:
        r = await c.post("/v1/messages", json={**MUSE, "stream": True}, headers=h)
    assert r.status_code == 200
    call = meta.calls[0]
    assert call["headers"]["authorization"] == "Bearer meta-key-123" and "x-api-key" not in call["headers"]
    row = last_request(conn)
    assert (row["user_id"], row["provider"], row["input_tokens"], row["output_tokens"]) == (uid, "meta", 50, 20)
    assert anthropic.calls == []


@pytest.mark.parametrize("action", ["disable", "revoke"])
async def test_disabling_or_revoking_stops_both_keys(env, meta, action):
    gw, conn, uid, full, routes = env
    (revoke if action == "revoke" else lambda c, u: set_enabled(c, u, False))(conn, uid)
    async with asgi_client(create_app(gw)) as c:
        for h in (full, routes):
            assert (await c.post("/v1/messages", json=MUSE, headers=h)).status_code in (401, 403)
    assert meta.calls == []


async def test_share_limit_blocks_claude_but_muse_still_works_on_the_routes_key(env, anthropic, meta):
    gw, conn, uid, full, routes = env
    now = time.time()
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)",
                 (uid, "share_5h", "*", "20", "pct"))

    def snap(t, util):
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                     (t, "header", "5h", util, now + 3600))
    snap(now - 300, 10)
    conn.execute("INSERT INTO requests(user_id, started_at, ended_at, method, path, provider, model, status, input_tokens) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (uid, now - 200, now - 200, "POST", "/v1/messages", "anthropic", "claude-sonnet-5", 200, 1000))
    snap(now - 100, 35)       # alice was the only one active: +25 points, over her 20
    async with asgi_client(create_app(gw)) as c:
        claude = await c.post("/v1/messages", json=MSG, headers=full)
        muse = await c.post("/v1/messages", json=MUSE, headers=routes)
    assert claude.status_code == 429
    assert muse.status_code == 200
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_routes_proxy.py -q`
Expected: `ImportError: cannot import name 'scope_allows'`.

- [ ] **Step 3: Implement**

In `src/claude_proxy/app.py`, after `route_model_entries`, add:

```python
def scope_allows(key_scope: str, method: str, path: str, route: Route | None) -> bool:
    """Whether a key of this scope may make this request (spec 2.3). A routes-only key (OpenCode) gets exactly two
    things: messages and token counts for a routed model, and the model list, which the gateway answers itself.
    Everything else is refused, so it can never reach the subscription credential."""
    if key_scope != "routes":
        return True
    if method == "GET" and path == "/v1/models":
        return True
    return method == "POST" and path in ROUTED_PATHS and route is not None


def _route_patterns(cfg: Config) -> str:
    return ", ".join(p for r in cfg.routes for p in r.models) or "none configured"
```

In `handle`, directly after the line `wants_title = bool(base["session_id"]) and ...` and before `if request.method == "POST" and path in ROUTED_PATHS and model is None:`, insert:

```python
    if not scope_allows(user["key_scope"], request.method, path, route):
        # Before the model check and limits: this key never reaches Claude, whatever else is wrong with the request.
        record(status=403, stream=0, complete=1, error_type="permission_error", rejected_by="key_scope")
        return api_error(403, "permission_error", f"This key is for third-party models only ({_route_patterns(cfg)}). "
                         "Claude models need your Claude Code key.")
    if user["key_scope"] == "routes" and path == "/v1/models":
        base["provider"] = "gateway"
        record(status=200, stream=0, complete=1)
        entries = route_model_entries(cfg)
        return JSONResponse({"data": entries, "has_more": False, "first_id": entries[0]["id"] if entries else None,
                             "last_id": entries[-1]["id"] if entries else None})
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_routes_proxy.py -q && .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/app.py tests/test_routes_proxy.py
git commit -m "Proxy: routes-only keys reach third-party routes and nothing else

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Dashboard backend

**Files:**
- Modify: `src/claude_proxy/web.py` (`ERROR_KIND`, `principal`, `login_key`, `me_status`, `user_action`, `_public_user`)
- Test: `tests/test_routes_web.py`

**Interfaces:**
- Consumes: `db.set_routes_key`, `db.remove_routes_key`, `user["key_scope"]` (Task 2).
- Produces: `POST /api/admin/users/{uid}/routes_key` → `{"ok": true, "key": "sk-proxy-r-..."}`; `POST /api/admin/users/{uid}/routes_key_remove` → `{"ok": true, "removed": bool}`; every user object from `/api/users`, `/api/session` and `/api/me/status` gains `"routes_prefix": str | null`. Task 7 uses these.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routes_web.py`:

```python
"""Routes-only keys on the dashboard (spec 2.4)."""
import time

from claude_proxy.db import set_routes_key
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client
from tests.test_web import admin_client, bearer, env  # noqa: F401  (fixture)


async def test_routes_key_reads_its_own_status_and_nothing_else(env):  # noqa: F811
    gw, conn, ids, keys = env
    rk = set_routes_key(conn, ids["alice"])
    async with asgi_client(create_dashboard_app(gw)) as c:
        r = await c.get("/api/me/status", headers=bearer(rk))
        assert r.status_code == 200 and r.json()["user"]["name"] == "alice"
        r = await c.get("/api/me/status?format=text", headers={"x-api-key": rk})
        assert r.status_code == 200 and r.text.startswith("alice")
        r = await c.get("/api/overview", headers=bearer(rk))
        assert r.status_code == 403
        assert r.json()["error"] == "This key only works for third-party models; use your Claude Code key for the dashboard."


async def test_an_admins_routes_key_cannot_do_admin_actions(env):  # noqa: F811
    gw, conn, ids, keys = env
    rk = set_routes_key(conn, ids["admin"])
    async with asgi_client(create_dashboard_app(gw)) as c:
        assert (await c.post("/api/admin/users", json={"name": "eve"}, headers=bearer(rk))).status_code == 403
        assert (await c.get("/api/users", headers=bearer(rk))).status_code == 403
    assert conn.execute("SELECT 1 FROM users WHERE name='eve'").fetchone() is None


async def test_routes_key_cannot_sign_in_and_is_not_a_failed_attempt(env):  # noqa: F811
    gw, conn, ids, keys = env
    rk = set_routes_key(conn, ids["alice"])
    async with asgi_client(create_dashboard_app(gw)) as c:
        for _ in range(7):           # more than the limiter's 5 failures
            r = await c.post("/api/login/key", json={"key": rk})
            assert r.status_code == 403
            assert r.json()["error"] == "This key only works for third-party models; sign in with your Claude Code key."
        assert (await c.post("/api/login/key", json={"key": keys["alice"]})).status_code == 200


async def test_admin_issues_and_removes_an_opencode_key(env):  # noqa: F811
    gw, conn, ids, keys = env

    async def bob(c):
        return next(u for u in (await c.get("/api/users")).json()["users"] if u["name"] == "bob")
    async with admin_client(gw) as c:
        assert (await bob(c))["routes_prefix"] is None
        key = (await c.post(f"/api/admin/users/{ids['bob']}/routes_key", json={})).json()["key"]
        assert key.startswith("sk-proxy-r-")
        assert (await bob(c))["routes_prefix"] == key[:14]
        r = await c.post(f"/api/admin/users/{ids['bob']}/routes_key_remove", json={})
        assert r.json() == {"ok": True, "removed": True}
        assert (await bob(c))["routes_prefix"] is None
    assert [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE target='bob' ORDER BY id")] == \
        ["routes_key_issue", "routes_key_remove"]


async def test_key_scope_refusals_are_not_counted_as_limit_hits(env):  # noqa: F811
    gw, conn, ids, keys = env
    conn.execute("INSERT INTO requests(user_id, started_at, method, path, model, status, rejected_by) VALUES(?,?,?,?,?,?,?)",
                 (ids["bob"], time.time() - 60, "POST", "/v1/messages", "claude-sonnet-5", 403, "key_scope"))
    async with admin_client(gw) as c:
        d = (await c.get(f"/api/errors?range=7d&user_id={ids['bob']}")).json()
    assert [(r["k"], r["rejected_by"]) for r in d["recent"]] == [("gateway_key_scope", "key_scope")]
    assert {p["k"] for p in d["points"]} == {"gateway_key_scope"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_routes_web.py -q`
Expected: failures (routes key accepted by `/api/overview`; `routes_prefix` missing; kind `gateway_limit`).

- [ ] **Step 3: Implement**

In `src/claude_proxy/web.py`:

1. `ERROR_KIND` becomes:

```python
ERROR_KIND = ("CASE WHEN rejected_by = 'auth' THEN 'gateway_auth' WHEN rejected_by = 'request' THEN 'gateway_bad_request' "
              "WHEN rejected_by = 'key_scope' THEN 'gateway_key_scope' "
              "WHEN rejected_by IS NOT NULL THEN 'gateway_limit' ELSE COALESCE(error_type, 'http_' || status) END")
```

2. Replace the key branch of `principal` so it reads:

```python
    def principal(request: Request, write: bool = False, routes_ok: bool = False):
        if request.headers.get("authorization") or request.headers.get("x-api-key"):
            try:
                user = authenticate(conn, request.headers)
            except AuthError as e:
                fail(e.status, e.body["error"]["message"])
            # A routes-only key (OpenCode) may read its own status and nothing else, and never acts as an admin.
            if user["key_scope"] == "routes" and not routes_ok:
                fail(403, "This key only works for third-party models; use your Claude Code key for the dashboard.")
            return user
```

(the cookie branch below it is unchanged).

3. In `login_key`, after the `try/except` block and before `return start_session(user)`, add:

```python
        if user["key_scope"] == "routes":   # a valid key, so not counted as a failed attempt
            fail(403, "This key only works for third-party models; sign in with your Claude Code key.")
```

4. In `me_status`, change `user = principal(request)` to `user = principal(request, routes_ok=True)`.

5. In `user_action`, after the `if action == "rotate": ...` line, add:

```python
        if action == "routes_key":
            try:
                return {"ok": True, "key": db.set_routes_key(conn, u["id"], actor["id"])}
            except ValueError as e:
                fail(400, str(e))
        if action == "routes_key_remove":
            return {"ok": True, "removed": db.remove_routes_key(conn, u["id"], actor["id"])}
```

6. `_public_user` becomes:

```python
def _public_user(u) -> dict:
    return {"id": u["id"], "name": u["name"], "role": u["role"], "prefix": u["key_prefix"],
            "routes_prefix": u["routes_key_prefix"]}
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_routes_web.py -q && .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/web.py tests/test_routes_web.py
git commit -m "Dashboard: routes-only keys read their own status only; admin issues and removes them

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `claude-proxy user routes-key`

**Files:**
- Modify: `src/claude_proxy/cli.py` (new `cmd_user_routes_key`, `cmd_user_list` column, parser)
- Test: `tests/test_routes_cli.py`

**Interfaces:**
- Consumes: `db.set_routes_key`, `db.remove_routes_key` (Task 2).
- Produces: `claude-proxy user routes-key <user> [--remove]`. The raw key is the last line of stdout.

- [ ] **Step 1: Write the failing test**

Create `tests/test_routes_cli.py`:

```python
from claude_proxy import cli
from claude_proxy.db import create_user, find_user_by_key, revoke

import pytest


def test_routes_key_issue_list_remove(db, monkeypatch, capsys):
    path, conn = db
    monkeypatch.setenv("CLAUDE_PROXY_DB", path)
    uid, _ = create_user(conn, "alice")
    cli.main(["user", "routes-key", "alice"])
    key = capsys.readouterr().out.strip().splitlines()[-1]
    assert find_user_by_key(conn, key)["key_scope"] == "routes"
    cli.main(["user", "list"])
    assert f"opencode {key[:14]}…" in capsys.readouterr().out
    cli.main(["user", "routes-key", "alice", "--remove"])
    assert "removed" in capsys.readouterr().out
    assert find_user_by_key(conn, key) is None
    cli.main(["user", "routes-key", "alice", "--remove"])
    assert "has no OpenCode key" in capsys.readouterr().out


def test_routes_key_for_a_revoked_user_exits_with_a_message(db, monkeypatch):
    path, conn = db
    monkeypatch.setenv("CLAUDE_PROXY_DB", path)
    uid, _ = create_user(conn, "alice")
    revoke(conn, uid)
    with pytest.raises(SystemExit, match="revoked"):
        cli.main(["user", "routes-key", "alice"])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_routes_cli.py -q`
Expected: `SystemExit: 2` from argparse (`invalid choice: 'routes-key'`).

- [ ] **Step 3: Implement**

In `src/claude_proxy/cli.py`, after `cmd_user_rotate`, add:

```python
def cmd_user_routes_key(args, cfg):
    conn = _conn(cfg)
    u = _user(conn, args.user)
    if args.remove:
        removed = db.remove_routes_key(conn, u["id"])
        print(f"{u['name']}: OpenCode key removed" if removed else f"{u['name']} has no OpenCode key.")
        return
    try:
        key = db.set_routes_key(conn, u["id"])
    except ValueError as e:
        sys.exit(str(e))
    print(f"OpenCode key for {u['name']} (third-party models only, never Claude), shown once:\n{key}")
```

In `cmd_user_list`, change the `print` to:

```python
        opencode = f"  opencode {r['routes_key_prefix']}…" if r["routes_key_prefix"] else ""
        print(f"{r['id']:>3}  {r['name']:<16} {r['role']:<5} {r['key_prefix']}…  {state}{opencode}")
```

In `main`, after the `rotate` parser line, add:

```python
    s = u.add_parser("routes-key", help="issue, replace or --remove a user's OpenCode key (third-party models only)")
    s.add_argument("user"); s.add_argument("--remove", action="store_true"); s.set_defaults(func=cmd_user_routes_key)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_routes_cli.py -q && .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/cli.py tests/test_routes_cli.py
git commit -m "CLI: user routes-key issues, replaces and removes OpenCode keys

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Dashboard frontend

**Files:**
- Modify: `src/claude_proxy/static/app.js`

**Interfaces:**
- Consumes: `u.routes_prefix`, `POST /api/admin/users/{id}/routes_key` → `{key}`, `.../routes_key_remove` (Task 5); error kind `gateway_key_scope`.

There are no JS tests in this repo; verification is `node --check` plus a look in a browser.

- [ ] **Step 1: Tips and error labels**

In the `TIPS` object, after the `act_rotate:` line, add:

```js
  act_routes_key: `Issue a key for OpenCode that works only for third-party models (such as Muse), never Claude. Issuing again replaces it.`,
  act_routes_key_remove: `Delete this user's OpenCode key. Their Claude Code key keeps working.`,
```

In `ERROR_TIPS`, after the `gateway_auth:` line, add:

```js
  gateway_key_scope: "An OpenCode key asked for a Claude model or a path it can't use. It never reached a provider.",
```

In `ERROR_LABELS`, after `gateway_auth: "Bad gateway key",` add `gateway_key_scope: "OpenCode key: not allowed",`.

- [ ] **Step 2: Show the OpenCode key prefix and the actions**

In `userRow`, change

```js
      <div class="muted" style="font-size:12px">${esc(u.prefix)}…</div></td>
```

to

```js
      <div class="muted" style="font-size:12px">${esc(u.prefix)}…${u.routes_prefix ? ` · OpenCode ${esc(u.routes_prefix)}…` : ""}</div></td>
```

Replace `userActions` with:

```js
function userActions(u) {
  const opencode = `<button class="btn small" data-act="routes_key" data-id="${u.id}" data-tip="act_routes_key">${u.routes_prefix ? "New OpenCode key" : "OpenCode key"}</button>` +
    (u.routes_prefix ? `<button class="btn small" data-act="routes_key_remove" data-id="${u.id}" data-tip="act_routes_key_remove">Remove OpenCode key</button>` : "");
  return `<div class="row-actions">${u.revoked ? `<button class="btn small danger" data-act="delete" data-id="${u.id}" data-tip="act_delete">Delete</button>` : u.id === S.user.id ? `<button class="btn small" data-act="limits" data-id="${u.id}" data-tip="act_limits">Limits</button>${opencode}` : `
      <button class="btn small" data-act="limits" data-id="${u.id}" data-tip="act_limits">Limits</button>
      <button class="btn small" data-act="rotate" data-id="${u.id}" data-tip="act_rotate">Rotate key</button>
      ${opencode}
      <button class="btn small" data-act="${u.enabled ? "disable" : "enable"}" data-id="${u.id}" data-tip="act_${u.enabled ? "disable" : "enable"}">${u.enabled ? "Disable" : "Enable"}</button>
      <button class="btn small danger" data-act="revoke" data-id="${u.id}" data-tip="act_revoke">Revoke</button>`}</div>`;
}
```

- [ ] **Step 3: Key dialog wording and the action handler**

Change `keyDialog` to take the instruction line:

```js
function keyDialog(title, key, how = "The user sets it as <code>ANTHROPIC_AUTH_TOKEN</code>.") {
  openDialog(`<h3>${esc(title)}</h3><p>Copy this key now; it is not shown again. ${how}</p>
    <code class="key" id="new-key">${esc(key)}</code><p><button class="btn" id="copy-key">Copy</button> <button class="btn primary" data-close>Done</button></p>`);
  $("#copy-key").onclick = async () => { try { await navigator.clipboard.writeText(key); $("#copy-key").textContent = "Copied"; } catch { /* clipboard blocked */ } };
}
```

In `userAction`, after `if (act === "rotate") return keyDialog(...);`, add:

```js
    if (act === "routes_key") return keyDialog(`OpenCode key for ${u.name}`, r.key,
      "It works only for third-party models. The user runs <code>claude-gateway on --opencode --url … --routes-key …</code> with it.");
```

- [ ] **Step 4: Verify**

Run: `node --check src/claude_proxy/static/app.js && .venv/bin/python -m pytest -q`
Expected: no syntax error; suite passes (the asset-versioning test hashes the file, so it keeps passing).

Then look at it: `python scripts/demo_data.py /tmp/demo.db`, start the gateway on that database (`CLAUDE_PROXY_DB=/tmp/demo.db CLAUDE_PROXY_CREDENTIAL_KEY=$(.venv/bin/claude-proxy keygen) .venv/bin/claude-proxy serve` as a background task), open http://127.0.0.1:8081, sign in as `admin` / `demo-password`, open Users & limits, click **OpenCode key** on a user, confirm the dialog shows an `sk-proxy-r-` key and the row then shows `· OpenCode sk-proxy-r-…` and a **Remove OpenCode key** button. Stop the server.

- [ ] **Step 5: Commit**

```bash
git add src/claude_proxy/static/app.js
git commit -m "Dashboard: OpenCode key buttons and the gateway_key_scope error kind

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `statusline.sh --warn`

**Files:**
- Modify: `scripts/statusline.sh` (full rewrite below; statusline mode output unchanged)
- Test: `tests/test_client_scripts.py` (create; Tasks 9 and 10 add to it)

**Interfaces:**
- Produces: `statusline.sh --warn` reading hook JSON on stdin; prints at most one line `{"systemMessage": "Gateway: <line>"}`; always exits 0. State file `${TMPDIR:-/tmp}/claude-gateway-status.<uid>.warned` holds the last band (1 or 2). Task 9 installs it as `<statusline path> --warn`.

- [ ] **Step 1: Write the failing tests (and the stub gateway the script tests share)**

Create `tests/test_client_scripts.py`:

```python
"""The client-side scripts, run as subprocesses against a stub gateway (spec 4, 5, tests 14-16)."""
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATUSLINE = ROOT / "scripts" / "statusline.sh"
GATEWAY = ROOT / "scripts" / "claude-gateway"


class Stub:
    """Answers /api/me/status (text), /v1/models and /health; records every request with lower-cased headers."""

    def __init__(self):
        self.status_line: str | None = "alice · daily 10/100 req"     # None: /api/me/status fails (gateway down)
        self.models: dict = {"data": [], "has_more": False}
        self.models_status = 200
        self.requests: list[tuple[str, dict]] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                stub.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                if self.path.startswith("/api/me/status"):
                    ok = stub.status_line is not None
                    code, body, ctype = (200 if ok else 503), ((stub.status_line or "") + "\n").encode(), "text/plain; charset=utf-8"
                elif self.path.startswith("/v1/models"):
                    code, body, ctype = stub.models_status, json.dumps(stub.models).encode(), "application/json"
                elif self.path == "/health":
                    code, body, ctype = 200, b'{"ok": true}', "application/json"
                else:
                    code, body, ctype = 404, b"{}", "application/json"
                self.send_response(code)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.server.shutdown()


def run(args, env, stdin=""):
    return subprocess.run(args, input=stdin, capture_output=True, text=True, env=env, timeout=60)


# ---------- statusline.sh --warn (spec 5, test 16) ----------

@pytest.fixture
def warn_env(tmp_path, stub):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp),
            "CLAUDE_GATEWAY_DASHBOARD": stub.url, "ANTHROPIC_AUTH_TOKEN": "sk-proxy-k"}


def warn(env):
    return run(["sh", str(STATUSLINE), "--warn"], env, stdin='{"prompt": "hi"}')


def fresh(env):
    """Drop the 30-second status cache but keep the record of the last warning."""
    for p in Path(env["TMPDIR"]).glob("claude-gateway-status.*"):
        if not p.name.endswith(".warned"):
            p.unlink()


def test_warn_at_80_percent(stub, warn_env):
    stub.status_line = "alice · daily 80/100 req"
    r = warn(warn_env)
    assert r.returncode == 0
    assert json.loads(r.stdout) == {"systemMessage": "Gateway: alice · daily 80/100 req"}


def test_warn_silent_at_79_percent(stub, warn_env):
    stub.status_line = "alice · daily 79/100 req · 5h 12%"
    r = warn(warn_env)
    assert (r.returncode, r.stdout) == (0, "")


def test_warn_silent_without_figures(stub, warn_env):
    stub.status_line = "alice"
    r = warn(warn_env)
    assert (r.returncode, r.stdout) == (0, "")


def test_warn_once_per_band_then_again_at_100(stub, warn_env):
    stub.status_line = "alice · daily $80/$100"
    assert warn(warn_env).stdout
    fresh(warn_env)
    stub.status_line = "alice · daily $95/$100"
    assert warn(warn_env).stdout == ""                     # same band within 15 minutes
    fresh(warn_env)
    stub.status_line = "alice · daily $100/$100"
    assert json.loads(warn(warn_env).stdout)["systemMessage"].endswith("$100/$100")


def test_warn_again_after_15_minutes(stub, warn_env):
    stub.status_line = "alice · 5h 85%"
    assert warn(warn_env).stdout
    fresh(warn_env)
    state = next(Path(warn_env["TMPDIR"]).glob("*.warned"))
    old = state.stat().st_mtime - 901
    os.utime(state, (old, old))
    assert warn(warn_env).stdout


def test_warn_silent_when_the_gateway_is_down(stub, warn_env):
    stub.status_line = None
    r = warn(warn_env)
    assert (r.returncode, r.stdout) == (0, "")


def test_warn_silent_without_dashboard_but_statusline_mode_complains(warn_env):
    env = {k: v for k, v in warn_env.items() if k != "CLAUDE_GATEWAY_DASHBOARD"}
    r = warn(env)
    assert (r.returncode, r.stdout) == (0, "")
    r = run(["sh", str(STATUSLINE)], env)
    assert r.returncode == 1 and "CLAUDE_GATEWAY_DASHBOARD" in r.stderr and r.stdout == ""


def test_warn_message_is_valid_json_whatever_the_name(stub, warn_env):
    stub.status_line = 'a"b\\c · daily 90/100 req'
    assert json.loads(warn(warn_env).stdout) == {"systemMessage": 'Gateway: a"b\\c · daily 90/100 req'}


def test_statusline_mode_still_colours_figures(stub, warn_env):
    stub.status_line = "alice · daily 90/100 req"
    r = run(["sh", str(STATUSLINE)], warn_env)
    assert r.returncode == 0
    assert "\033[33m90/100\033[36m" in r.stdout and r.stdout.startswith("\033[36m◆ alice")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_client_scripts.py -q`
Expected: the `--warn` tests fail (today the script ignores `--warn` and prints the coloured line); `test_statusline_mode_still_colours_figures` passes.

- [ ] **Step 3: Rewrite `scripts/statusline.sh`**

Replace the whole file with:

```sh
#!/bin/sh
# Claude Code statusline for gateway users: your limits and the shared account's quota.
#
# ~/.claude/settings.json:
#   "statusLine": {"type": "command", "command": "/path/to/statusline.sh", "refreshInterval": 30}
# With --warn it is a UserPromptSubmit hook instead. When a figure on the line is at 80% or more it prints
# {"systemMessage": "Gateway: <line>"}, which Claude Code shows; again after 15 minutes, or at once when a
# figure reaches 100%. Nothing else ever goes to stdout in that mode: a hook's plain output joins the prompt.
#   "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/path/to/statusline.sh --warn"}]}]}
# Environment (e.g. in the same settings.json "env" block):
#   ANTHROPIC_AUTH_TOKEN        your gateway key (already set for the gateway); in own-login mode, where
#                               it is unset, the key is read from ANTHROPIC_CUSTOM_HEADERS (x-gateway-key)
#   CLAUDE_GATEWAY_DASHBOARD    dashboard base URL, e.g. http://gateway.lan:8081
warn=
[ "${1:-}" = --warn ] && warn=1
cat >/dev/null   # Claude Code sends session or prompt JSON on stdin; this script does not need it.
if [ -z "${CLAUDE_GATEWAY_DASHBOARD:-}" ]; then
  [ -n "$warn" ] && exit 0
  echo "statusline.sh: set CLAUDE_GATEWAY_DASHBOARD" >&2
  exit 1
fi
cache="${TMPDIR:-/tmp}/claude-gateway-status.$(id -u)"
# A figure is a percentage or a used/limit pair (`$61/$100`, `4.2M/5.0M`); pct() gives it as a percentage.
FIGURES='
function num(t) { gsub(/[$,%]/, "", t); return t ~ /M$/ ? t * 1e6 : t ~ /K$/ ? t * 1e3 : t + 0 }
function pct(tok,  ab) { if (split(tok, ab, "/") == 2) return num(ab[2]) ? 100 * num(ab[1]) / num(ab[2]) : 0; return num(tok) }
BEGIN { RE = "[$]?[0-9][0-9,.]*[KM]?/[$]?[0-9][0-9,.]*[KM]?%?|[0-9]+([.][0-9]+)?%" }
'
# Cyan with a leading diamond, so it stands apart from Claude Code's own items; a figure turns yellow at 80%
# and red at 100%. The cache keeps the plain line.
show() {
  printf '%s\n' "$1" | awk "$FIGURES"'
  {
    out = ""; rest = $0
    while (match(rest, RE)) {
      tok = substr(rest, RSTART, RLENGTH); v = pct(tok)
      c = v >= 100 ? "\033[31m" : v >= 80 ? "\033[33m" : ""
      out = out substr(rest, 1, RSTART - 1) (c ? c tok "\033[36m" : tok)
      rest = substr(rest, RSTART + RLENGTH)
    }
    printf "\033[36m\342\227\206 %s%s\033[0m\n", out, rest
  }'
}
peak() {   # the highest figure on the line, as a whole percentage (0 when there is none)
  printf '%s\n' "$1" | awk "$FIGURES"'
  {
    m = 0; rest = $0
    while (match(rest, RE)) { v = pct(substr(rest, RSTART, RLENGTH)); if (v > m) m = v; rest = substr(rest, RSTART + RLENGTH) }
    printf "%d\n", m
  }'
}
age() { echo $(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1") )); }

if [ -f "$cache" ] && [ "$(age "$cache")" -lt 30 ]; then
  line=$(cat "$cache")
else
  key=${ANTHROPIC_AUTH_TOKEN:-$(printf '%s\n' "${ANTHROPIC_CUSTOM_HEADERS:-}" | sed -n 's/^[Xx]-[Gg]ateway-[Kk]ey: *//p' | head -n 1)}
  if line=$(curl -fsS --max-time 3 -H "Authorization: Bearer ${key}" \
            "${CLAUDE_GATEWAY_DASHBOARD%/}/api/me/status?format=text" 2>/dev/null); then
    printf '%s\n' "$line" > "$cache"
  else
    line=
  fi
fi

if [ -z "$warn" ]; then
  show "${line:-gateway status unavailable}"
  exit 0
fi

# --warn: band 1 from 80%, band 2 from 100%. The state file holds the band last warned about; its age is the time since.
[ -n "$line" ] || exit 0
p=$(peak "$line")
band=0
[ "${p:-0}" -ge 80 ] 2>/dev/null && band=1
[ "${p:-0}" -ge 100 ] 2>/dev/null && band=2
state="$cache.warned"
if [ "$band" = 0 ]; then
  rm -f "$state"
  exit 0
fi
last=$(cat "$state" 2>/dev/null)
case "$last" in 1|2) ;; *) last=0 ;; esac
if [ "$band" -le "$last" ] && [ "$(age "$state")" -lt 900 ]; then
  exit 0
fi
printf '%s\n' "$band" > "$state"
msg=$(printf 'Gateway: %s' "$line" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
printf '{"systemMessage": "%s"}\n' "$msg"
exit 0
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_client_scripts.py -q && .venv/bin/python -m pytest -q`
Expected: all pass. If `test_statusline_mode_still_colours_figures` fails, compare with `git show HEAD:scripts/statusline.sh` — statusline mode output must be byte-for-byte what it was.

- [ ] **Step 5: Commit**

```bash
git add scripts/statusline.sh tests/test_client_scripts.py
git commit -m "Statusline: --warn mode for a UserPromptSubmit hook, warning at 80% and 100%

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: `claude-gateway on` installs the warning hook

**Files:**
- Modify: `scripts/claude-gateway` (the python in `edit_settings`, the header comment, `usage`)
- Test: `tests/test_client_scripts.py` (append)

**Interfaces:**
- Consumes: `statusline.sh --warn` (Task 8).
- Produces: `client.json` field `added_warn_hook: true` while installed. The hook entry is `{"hooks": [{"type": "command", "command": "<quoted statusline path> --warn", "timeout": 10}]}` appended to `settings.hooks.UserPromptSubmit`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_client_scripts.py`:

```python
# ---------- claude-gateway ----------

@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def cg(home, *args):
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    return run(["bash", str(GATEWAY), *args], {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(tmp)})


def test_on_installs_the_warning_hook_beside_others_and_off_removes_only_it(stub, home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    other = {"hooks": [{"type": "command", "command": "echo other"}]}
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [other]}}, indent=2) + "\n")
    r = cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 0, r.stderr
    s = json.loads(settings.read_text())
    groups = s["hooks"]["UserPromptSubmit"]
    assert groups[0] == other
    ours = groups[1]["hooks"][0]
    assert ours["type"] == "command" and ours["command"].endswith("statusline.sh --warn") and ours["timeout"] == 10
    assert cg(home, "on").returncode == 0                                 # idempotent
    assert len(json.loads(settings.read_text())["hooks"]["UserPromptSubmit"]) == 2
    assert cg(home, "off").returncode == 0
    assert json.loads(settings.read_text()).get("hooks") == {"UserPromptSubmit": [other]}


def test_off_removes_the_hooks_block_it_created(stub, home):
    assert cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert cg(home, "off").returncode == 0
    assert "hooks" not in json.loads((home / ".claude" / "settings.json").read_text())
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_client_scripts.py -q -k "hook"`
Expected: `KeyError: 'hooks'` / assertion failures.

- [ ] **Step 3: Implement**

In `scripts/claude-gateway`, inside the python heredoc of `edit_settings`, directly after these existing lines:

```python
if c.pop("added_statusline", False) and ours_line(s.get("statusLine")):
    del s["statusLine"]
```

add:

```python
# The limit warning (statusline.sh --warn): our own group in the UserPromptSubmit hooks, beside any others.
warn_cmd = line_cmd + " --warn"
ours_hook = lambda g: isinstance(g, dict) and any(isinstance(h, dict) and h.get("command") == warn_cmd for h in g.get("hooks", []))
if c.pop("added_warn_hook", False) and isinstance(s.get("hooks"), dict):
    groups = [g for g in s["hooks"].get("UserPromptSubmit", []) if not ours_hook(g)]
    if groups:
        s["hooks"]["UserPromptSubmit"] = groups
    else:
        s["hooks"].pop("UserPromptSubmit", None)
    if not s["hooks"]:
        del s["hooks"]
```

Then, in the `if mode == "on":` block, inside `if os.path.exists(statusline):`, after the existing statusline `if/elif`, add (same indentation as that `if`):

```python
        hooks = s.setdefault("hooks", {})
        if isinstance(hooks, dict) and isinstance(hooks.setdefault("UserPromptSubmit", []), list):
            if not any(ours_hook(g) for g in hooks["UserPromptSubmit"]):
                hooks["UserPromptSubmit"].append({"hooks": [{"type": "command", "command": warn_cmd, "timeout": 10}]})
                c["added_warn_hook"] = True
```

Update the header comment: replace the line

```
# `on` also installs a statusline showing the user's gateway limits, unless one is already set; then it prints
```

with

```
# `on` also installs a UserPromptSubmit hook (statusline.sh --warn) that shows a warning when a limit is at 80%,
# and a statusline showing the user's gateway limits, unless one is already set; then it prints
```

and make `usage` print the whole leading comment block, however long it gets:

```bash
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit "${1:-0}"; }
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_client_scripts.py -q && .venv/bin/python -m pytest -q && bash scripts/claude-gateway --help | head -3`
Expected: all pass; `--help` prints the first comment lines.

- [ ] **Step 5: Commit**

```bash
git add scripts/claude-gateway tests/test_client_scripts.py
git commit -m "claude-gateway: install the limit warning hook with the statusline

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: `claude-gateway --opencode` and the OpenCode Muse agent

**Files:**
- Create: `examples/opencode/muse.md`
- Modify: `scripts/claude-gateway` (variables, `field`, `src_dir`, `install_statusline`, new `preflight_routes`, `opencode_edit`, `opencode_on`, `on`/`off`/`status` branches, header)
- Test: `tests/test_client_scripts.py` (append)

**Interfaces:**
- Consumes: gateway `GET /v1/models` on a routes-only key (Task 4), with `max_input_tokens`/`max_tokens` (Task 3).
- Produces: `claude-gateway on --opencode [--url URL] [--routes-key KEY]`, `claude-gateway off --opencode`, `status` line `opencode: gateway provider installed` / `opencode: not set up`. Files: `~/.config/claude-gateway/routes.key` (600, no newline); `client.json` field `opencode: {added_provider, created_file, agent_sha256}`; `~/.config/opencode/opencode.json` provider `gateway`; `~/.config/opencode/agents/muse.md`.

- [ ] **Step 1: Create the OpenCode agent**

Create `examples/opencode/muse.md`:

```markdown
---
description: Fast executor for isolated, well-specified coding tasks. Give it a precise task with file paths and acceptance criteria; it does not plan or explore broadly.
mode: subagent
model: gateway/muse-spark
tools:
  write: false
  webfetch: false
---

You carry out one well-specified coding task. Read only the files you need, make the change, run the
check you were given, and report what you changed and the check's result. If the task is ambiguous or
needs a design decision, stop and say what is missing instead of guessing.
```

(OpenCode takes the agent name from the file name. `write: false` and `webfetch: false` mirror the Claude Code agent in `examples/muse-worker.md`, which has Read, Edit, Bash, Glob and Grep only.)

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_client_scripts.py`:

```python
# ---------- claude-gateway --opencode (spec 4, tests 14-15) ----------

MODELS = {"data": [{"type": "model", "id": "muse-spark", "display_name": "Muse Spark 1.3", "created_at": "2026-01-01T00:00:00Z",
                    "max_input_tokens": 1048576, "max_tokens": 32000},
                   {"type": "model", "id": "other-model", "display_name": "other-model (via x)", "created_at": "2026-01-01T00:00:00Z"}],
          "has_more": False, "first_id": "muse-spark", "last_id": "other-model"}


def oc_paths(home):
    return (home / ".config" / "opencode" / "opencode.json", home / ".config" / "claude-gateway" / "routes.key",
            home / ".config" / "opencode" / "agents" / "muse.md")


def test_opencode_on_writes_provider_key_file_and_agent(stub, home):
    stub.models = MODELS
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-abc")
    assert r.returncode == 0, r.stderr
    conf, key_file, agent = oc_paths(home)
    assert json.loads(conf.read_text()) == {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"gateway": {
            "npm": "@ai-sdk/anthropic", "name": "Claude gateway",
            "options": {"baseURL": stub.url + "/v1", "apiKey": "{file:%s}" % key_file},
            "models": {"muse-spark": {"name": "Muse Spark 1.3", "limit": {"context": 1048576, "output": 32000}},
                       "other-model": {"name": "other-model (via x)"}}}}}
    assert key_file.read_text() == "sk-proxy-r-abc"
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert "model: gateway/muse-spark" in agent.read_text()
    path, headers = stub.requests[-1]
    assert path == "/v1/models" and headers["x-api-key"] == "sk-proxy-r-abc"
    assert "opencode: gateway provider installed" in cg(home, "status").stdout


def test_opencode_off_restores_config_and_keeps_later_edits(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    conf.parent.mkdir(parents=True)
    original = {"$schema": "https://opencode.ai/config.json", "theme": "dark", "provider": {"mine": {"npm": "x"}}}
    text = json.dumps(original, indent=2) + "\n"
    conf.write_text(text)
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "k").returncode == 0
    assert cg(home, "off", "--opencode").returncode == 0
    assert conf.read_text() == text
    assert not key_file.exists() and not agent.exists()
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "k").returncode == 0
    edited = json.loads(conf.read_text())
    edited["theme"] = "light"
    edited["provider"]["theirs"] = {"npm": "y"}
    conf.write_text(json.dumps(edited, indent=2) + "\n")
    assert cg(home, "off", "--opencode").returncode == 0
    assert json.loads(conf.read_text()) == {**original, "theme": "light", "provider": {"mine": {"npm": "x"}, "theirs": {"npm": "y"}}}


def test_opencode_off_deletes_a_config_it_created(stub, home):
    stub.models = MODELS
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "k").returncode == 0
    assert cg(home, "off", "--opencode").returncode == 0
    conf, key_file, agent = oc_paths(home)
    assert not conf.exists() and not key_file.exists() and not agent.exists()
    assert "opencode: not set up" in cg(home, "status").stdout


def test_opencode_rerun_refreshes_models_and_spares_an_edited_agent(stub, home):
    stub.models = {"data": [MODELS["data"][0]], "has_more": False}
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "k").returncode == 0
    conf, key_file, agent = oc_paths(home)
    agent.write_text(agent.read_text() + "\nMy own note.\n")
    stub.models = MODELS
    r = cg(home, "on", "--opencode")                  # reuses the saved URL and key
    assert r.returncode == 0, r.stderr
    assert set(json.loads(conf.read_text())["provider"]["gateway"]["models"]) == {"muse-spark", "other-model"}
    assert cg(home, "off", "--opencode").returncode == 0
    assert agent.read_text().endswith("My own note.\n")


def test_opencode_on_changes_nothing_when_the_gateway_refuses_the_key(stub, home):
    stub.models_status = 403
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "bad")
    assert r.returncode == 1 and "did not accept the OpenCode key (HTTP 403)" in r.stderr
    conf, key_file, agent = oc_paths(home)
    assert not conf.exists() and not key_file.exists() and not agent.exists()


def test_opencode_on_leaves_a_non_json_config_alone(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    conf.parent.mkdir(parents=True)
    conf.write_text('{\n  // a comment\n  "theme": "dark"\n}\n')
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "k")
    assert r.returncode == 1 and "not plain JSON" in r.stderr
    assert conf.read_text() == '{\n  // a comment\n  "theme": "dark"\n}\n'
    assert not key_file.exists()


def test_opencode_on_refuses_a_gateway_provider_it_did_not_add(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    conf.parent.mkdir(parents=True)
    conf.write_text(json.dumps({"provider": {"gateway": {"npm": "mine"}}}, indent=2) + "\n")
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "k")
    assert r.returncode == 1 and "did not add" in r.stderr
    assert json.loads(conf.read_text()) == {"provider": {"gateway": {"npm": "mine"}}}


def test_opencode_on_never_touches_opencode_jsonc(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    conf.parent.mkdir(parents=True)
    jsonc = conf.parent / "opencode.jsonc"
    jsonc.write_text('{\n  // mine\n  "theme": "dark"\n}\n')
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "k").returncode == 0
    assert jsonc.read_text() == '{\n  // mine\n  "theme": "dark"\n}\n'
    assert "gateway" in json.loads(conf.read_text())["provider"]


def test_routes_key_without_opencode_is_refused(stub, home):
    r = cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full", "--routes-key", "k")
    assert r.returncode == 1 and "--routes-key goes with --opencode" in r.stderr
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_client_scripts.py -q -k opencode`
Expected: failures (`usage` exit 1 on the unknown `--opencode` option).

- [ ] **Step 4: Implement in `scripts/claude-gateway`**

4a. Header comment. After the line `#   claude-gateway status`, add:

```
#   claude-gateway on --opencode --url https://claude.example.com --routes-key sk-proxy-r-...   # OpenCode, first time
#   claude-gateway on --opencode     # later: refreshes the model list
#   claude-gateway off --opencode
```

and before `set -euo pipefail`, add these comment lines:

```
# --opencode acts on OpenCode only (third-party models such as Muse; Anthropic accepts the subscription only from
# Claude Code). With the routes-only key the admin issues (`claude-proxy user routes-key`), it adds a "gateway"
# provider to ~/.config/opencode/opencode.json and a muse subagent; `off --opencode` removes exactly those. The key
# is kept in ~/.config/claude-gateway/routes.key (mode 600) and opencode.json only refers to that file.
```

4b. Variables. After the `STATUSLINE=...` line, add:

```bash
ROUTES_KEY=$(dirname "$CLIENT")/routes.key
OPENCODE_DIR=${CLAUDE_GATEWAY_OPENCODE_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}   # where OpenCode reads its global config
```

4c. Make `field` tolerate a missing `client.json` (the OpenCode set-up may run first):

```bash
field() { python3 -c 'import json,os,sys; c=json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {}; print(c.get(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""))' "$CLIENT" "$@"; }
```

4d. Replace `install_statusline` with `src_dir` plus the same function using it:

```bash
src_dir() {   # the directory this script really lives in (through any symlink)
  dirname "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")"
}

install_statusline() {   # copy statusline.sh from beside this script next to client.json
  local src
  src=$(src_dir)/statusline.sh
  if [ -f "$src" ]; then cp "$src" "$STATUSLINE" && chmod 755 "$STATUSLINE"; fi
}
```

4e. After the existing `preflight` function, add:

```bash
preflight_routes() {   # url key out: the gateway takes the OpenCode key the way OpenCode sends it; its model list goes to out
  local code
  code=$(curl -sS -o "$3" -w '%{http_code}' --max-time 15 -H "x-api-key: $2" -H "anthropic-version: 2023-06-01" \
         "$1/v1/models" 2>/dev/null) || code=000
  if [ "$code" != 200 ]; then
    echo "The gateway at $1 did not accept the OpenCode key (HTTP $code); leaving OpenCode as it is." >&2
    head -c 400 "$3" >&2; echo >&2; rm -f "$3"; exit 1
  fi
}

opencode_edit() {   # on <models.json> | off. `on` takes the URL and key from CG_URL and CG_KEY.
  python3 - "$1" "$OPENCODE_DIR" "$CLIENT" "$ROUTES_KEY" "$(src_dir)/../examples/opencode/muse.md" "${2:-}" <<'EOF'
import hashlib, json, os, shutil, sys, tempfile
mode, oc_dir, client, key_file, agent_src, models_file = sys.argv[1:7]
key_file = os.path.abspath(key_file)
conf = os.path.join(oc_dir, "opencode.json")      # never opencode.jsonc: OpenCode merges the two
agent = os.path.join(oc_dir, "agents", "muse.md")
SCHEMA = "https://opencode.ai/config.json"

def digest(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def write_json(path, data, perms=None):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, perms if perms is not None else (os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644))
    os.replace(tmp, path)

c = json.load(open(client)) if os.path.exists(client) else {}
rec = c.get("opencode") or {}
try:
    s = json.load(open(conf)) if os.path.exists(conf) else None
except ValueError:
    sys.exit(f"{conf} is not plain JSON (comments or a trailing comma?), so it was left as it is. "
             "Add the gateway provider by hand, or move the comments to opencode.jsonc.")
created = s is None
if created:
    s = {"$schema": SCHEMA}
providers = s.get("provider") if isinstance(s.get("provider"), dict) else {}

if mode == "on":
    url, key = os.environ["CG_URL"].rstrip("/"), os.environ["CG_KEY"]
    if "gateway" in providers and not rec.get("added_provider"):
        sys.exit(f"{conf} already has a provider named 'gateway' that claude-gateway did not add; left as it is.")
    models = {}
    for m in json.load(open(models_file)).get("data", []):
        entry = {"name": m.get("display_name") or m["id"]}
        if m.get("max_input_tokens") and m.get("max_tokens"):
            entry["limit"] = {"context": m["max_input_tokens"], "output": m["max_tokens"]}
        models[m["id"]] = entry
    s.setdefault("provider", {})["gateway"] = {
        "npm": "@ai-sdk/anthropic", "name": "Claude gateway",
        "options": {"baseURL": url + "/v1", "apiKey": "{file:" + key_file + "}"},
        "models": models}
    rec = {"added_provider": True, "created_file": rec.get("created_file", created), "agent_sha256": rec.get("agent_sha256")}
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    old = os.umask(0o077)
    try:
        with open(key_file, "w") as f:
            f.write(key)                           # no newline: OpenCode sends the file's content as the key
    finally:
        os.umask(old)
    os.chmod(key_file, 0o600)
    os.makedirs(oc_dir, exist_ok=True)
    write_json(conf, s)
    if not os.path.exists(agent) and os.path.exists(agent_src):
        os.makedirs(os.path.dirname(agent), exist_ok=True)
        shutil.copyfile(agent_src, agent)
        rec["agent_sha256"] = digest(agent)
    c["url"] = url
    c["opencode"] = rec
else:
    if rec.get("added_provider") and "gateway" in providers:
        del providers["gateway"]
        if not providers:
            s.pop("provider", None)
        if rec.get("created_file") and s == {"$schema": SCHEMA}:
            os.remove(conf)
        else:
            write_json(conf, s)
    sha = rec.get("agent_sha256")
    if sha and os.path.exists(agent) and digest(agent) == sha:   # untouched since `on` copied it
        os.remove(agent)
    if os.path.exists(key_file):
        os.remove(key_file)
    c.pop("opencode", None)
if c or os.path.exists(client):
    os.makedirs(os.path.dirname(client), exist_ok=True)
    write_json(client, c, 0o600)
EOF
}

opencode_on() {   # url-or-empty key-or-empty; empty means the saved one
  local url key models
  url=${1:-$(field url)}
  url=${url%/}
  key=${2:-$(cat "$ROUTES_KEY" 2>/dev/null || true)}
  [ -n "$url" ] || { echo "No gateway URL yet: claude-gateway on --opencode --url <url> --routes-key <key>" >&2; exit 1; }
  [ -n "$key" ] || { echo "No OpenCode key yet: claude-gateway on --opencode --routes-key <key> (the admin issues it)" >&2; exit 1; }
  models=$(mktemp)
  preflight_routes "$url" "$key" "$models"
  export CG_URL=$url CG_KEY=$key
  if ! opencode_edit on "$models"; then rm -f "$models"; exit 1; fi
  rm -f "$models"
  echo "OpenCode now has a 'gateway' provider for $url (third-party models only). Start a new OpenCode session to pick it up."
}

opencode_state() {
  if [ -f "$OPENCODE_DIR/opencode.json" ] && python3 -c 'import json,sys; sys.exit(0 if "gateway" in (json.load(open(sys.argv[1])).get("provider") or {}) else 1)' "$OPENCODE_DIR/opencode.json" 2>/dev/null; then
    echo "opencode: gateway provider installed"
  else
    echo "opencode: not set up"
  fi
}
```

4f. The `on` branch: change the first line of the branch to `url="" key="" dash="" mode="" opencode="" rkey=""`, add these two cases to its `while/case`:

```bash
        --opencode) opencode=1; shift ;;
        --routes-key) rkey=$2; shift 2 ;;
```

and directly after the `done` of that loop, add:

```bash
    if [ -n "$opencode" ]; then
      [ -z "$key$dash$mode" ] || { echo "--opencode takes only --url and --routes-key; set up Claude Code with a separate 'claude-gateway on'." >&2; exit 1; }
      opencode_on "$url" "$rkey"
      exit 0
    fi
    [ -z "$rkey" ] || { echo "--routes-key goes with --opencode." >&2; exit 1; }
```

4g. The `off` branch becomes:

```bash
  off)
    if [ "${1:-}" = --opencode ]; then
      opencode_edit off
      echo "OpenCode no longer uses the gateway; the OpenCode key file was deleted."
      exit 0
    fi
    edit_settings off
    echo "Claude Code uses this machine's own login again. Start a new session to pick it up."
    ;;
```

4h. The `status` branch: change its "off" line to

```bash
    if [ -z "${url:-}" ]; then echo "off: Claude Code uses this machine's own login"; opencode_state; exit 0; fi
```

and add `opencode_state` as the last command of the branch (before `;;`).

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_client_scripts.py -q && .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/claude-gateway examples/opencode/muse.md tests/test_client_scripts.py
git commit -m "claude-gateway: --opencode sets up OpenCode's gateway provider and a Muse subagent

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: README and live check

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-09-25-client-side-and-opencode-routes-design.md` (status line only)

- [ ] **Step 1: README**

In `README.md`, in "Onboard a person", after the paragraph that ends `They can also sign in to the dashboard with their key and see only their own data.`, add:

~~~markdown
`claude-gateway on` also adds a prompt hook (`statusline.sh --warn`): when any of those figures reaches 80%,
Claude Code shows the status line as a warning before the prompt is sent, again every 15 minutes, and at once
at 100%. It never blocks a prompt; the gateway is still the only place limits are enforced.
~~~

After the "Muse as a subagent" section, add:

~~~markdown
### OpenCode for Muse and other third-party models

Anthropic accepts a subscription login only from Claude.ai and Claude Code, so OpenCode can't use Claude
through the gateway. It can use the gateway's other routes. The admin issues a second key that works only for
them:

```sh
claude-proxy user routes-key maya          # or Users & limits → OpenCode key; --remove deletes it
```

On maya's machine:

```sh
scripts/claude-gateway on --opencode --url https://gateway.example.com --routes-key sk-proxy-r-...
scripts/claude-gateway on --opencode       # later: picks up new routes
scripts/claude-gateway off --opencode
```

This adds a `gateway` provider to `~/.config/opencode/opencode.json`, with each route model and the context
window set in the route's `model_info`, and a `muse` subagent in `~/.config/opencode/agents/`. The key stays in
`~/.config/claude-gateway/routes.key`. The gateway refuses it for Claude models and for the dashboard (it can
read `/api/me/status`), with a 403 that says to use the Claude Code key. Limits, metering and the dashboard
count it as maya's. Share limits don't apply to third-party models, so Muse still works after maya's share of
the subscription is used up. OpenCode sessions show up in usage but not as named sessions.
~~~

In the Operations bullet about Revoke, append: `Revoking also stops the person's OpenCode key.`

- [ ] **Step 2: Mark the spec implemented**

In the spec header, change `- **Status:** Draft, revised after a review by another agent; awaiting user review` to `- **Status:** Implemented (branch opencode-routes)`.

- [ ] **Step 3: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (177 existing + the new tests).

- [ ] **Step 4: Live check (spec section 9)**

Against a local gateway with a real `META_API_KEY` if one is available (otherwise point the meta route at the Task 1 recorder with a `config.toml` override of `base_url = "http://127.0.0.1:18099"` and say so in the report):

```bash
export CLAUDE_PROXY_DB=/tmp/live.db CLAUDE_PROXY_CREDENTIAL_KEY=$(.venv/bin/claude-proxy keygen) CLAUDE_PROXY_ADMIN_PASSWORD=live-check-password
.venv/bin/claude-proxy init && .venv/bin/claude-proxy user add maya
RK=$(.venv/bin/claude-proxy user routes-key maya | tail -1)
.venv/bin/claude-proxy serve   # as a background task, with the same environment
# then, with a throwaway HOME so the person's real OpenCode config is untouched:
mkdir -p /tmp/live-home && HOME=/tmp/live-home XDG_CONFIG_HOME=/tmp/live-home/.config bash scripts/claude-gateway on --opencode --url http://127.0.0.1:8080 --routes-key "$RK"
```

Then run `HOME=/tmp/live-home XDG_CONFIG_HOME=/tmp/live-home/.config opencode run -m gateway/muse-spark "Reply with ok"` (it reads `/tmp/live-home/.config/opencode/opencode.json`; wrap it in `perl -e 'alarm 180; exec @ARGV'`) and confirm a reply. The subscription login is not needed: Muse never touches it. Confirm refusal: `curl -s -H "x-api-key: $RK" -H 'content-type: application/json' -d '{"model":"claude-sonnet-5","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' http://127.0.0.1:8080/v1/messages` returns the 403 message from Global Constraints. Also confirm the warning hook is shown to the person: with the Task 1 pty driver and a gateway whose status line is at 80% or more for the user, submit a prompt and check that `Gateway: …` appears in the tty log. If it does not, record it in the report (the hook is then harmless but invisible). Stop the server.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-09-25-client-side-and-opencode-routes-design.md
git commit -m "README: onboarding an OpenCode user and the limit warning hook

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** 2.1 → Task 2; 2.2 → Tasks 2, 5, 6, 7; 2.3 → Task 4; 2.4 → Task 5 (+ Task 7 labels); 3.1–3.2 → Tasks 3, 4; 4 → Task 10; 5 → Tasks 8, 9; 6 (error table) → Tasks 4, 5, 10; 7–8 → Task 1; 9 tests 1–13 → Tasks 2–5, 14–15 → Task 10, 16 → Task 8; live check → Task 11; 10 delivery order → task order.
- **Spec test 11** (migration + unique index) is `test_migration_adds_the_columns_and_index_to_an_old_database` and `test_two_users_cannot_share_a_routes_key_hash` (Task 2). **Spec test 15** (no `limit` without sizes) is the `other-model` entry in `test_opencode_on_writes_provider_key_file_and_agent` (Task 10).
- **Names used across tasks:** `key_scope`, `set_routes_key`, `remove_routes_key`, `route_model_entries`, `scope_allows`, `routes_prefix`, `routes_key_prefix`, `added_warn_hook`, `opencode_edit`, `ROUTES_KEY`, `OPENCODE_DIR` — each defined once, above its first use.
