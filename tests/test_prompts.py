"""The prompts a person types in a Claude Code session are kept for the admin: the typed text only, once each."""
import time

import pytest
from fastapi.responses import JSONResponse

from claude_proxy import prompts
from claude_proxy.app import create_app
from claude_proxy.db import cleanup, create_user, delete_user, revoke
from claude_proxy.web import create_dashboard_app
from tests.conftest import asgi_client, make_gateway, message_json, seed_oauth
from tests.test_proxy import TITLE_REQ

REMINDER = {"type": "text", "text": "<system-reminder>\nAttribution for git commits\n</system-reminder>\n"}
ENV = {"role": "system", "content": [{"type": "text", "text": "# Environment\nToday's date is 2026-11-17."}]}
TOOLS = [{"name": "Bash", "input_schema": {"type": "object"}}]


def user(*blocks):
    return {"role": "user", "content": [b if isinstance(b, dict) else {"type": "text", "text": b} for b in blocks]}


def body(*messages, **kw):
    return {"model": "claude-sonnet-5", "max_tokens": 64, "tools": TOOLS, "messages": list(messages), **kw}


# ---- what counts as typed (shapes sent by Claude Code 2.1.326) ----

def test_the_typed_text_of_the_last_user_turn():
    assert prompts.extract(body(user(REMINDER, "fix the login"), ENV)) == (0, "fix the login")
    assert prompts.extract(body({"role": "user", "content": "plain string"})) == (0, "plain string")
    later = body(user(REMINDER, "first"), ENV, {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                 user("<system-reminder>\nResult of calling the Read tool:\n1\thello\n</system-reminder>\n", "second  prompt\nline two"), ENV)
    assert prompts.extract(later) == (3, "second  prompt\nline two")


def test_turns_the_person_did_not_type_hold_no_prompt():
    tool_result = {"type": "tool_result", "tool_use_id": "t1", "content": "secret file contents"}
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    assert prompts.extract(body(user(REMINDER, "go"), ENV, {"role": "assistant", "content": []}, user(tool_result))) is None
    assert prompts.extract(body(user(image, "what is this?"))) == (0, "what is this?")
    for harness in ("Another Claude session sent a message:\n<agent-message from=\"a1\">4</agent-message>",
                    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.",
                    "This session is being continued from a previous conversation that ran out of context.",
                    "[Request interrupted by user]", "<system-reminder>\nCalled the Read tool\n</system-reminder>"):
        assert prompts.extract(body({"role": "user", "content": harness})) is None, harness
    assert prompts.extract(body(user("hi"), {"role": "assistant", "content": "pre"})) is None   # a prefilled reply
    assert prompts.extract({"messages": "x"}) is None and prompts.extract(body()) is None


def test_a_message_typed_while_claude_works_arrives_beside_tool_results():
    tool_result = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
    note = ("<system-reminder>\nThe user sent a new message while you were working:\nalso record prompts\n\n"
            "This is how Claude Code surfaces messages the user sends mid-turn.\n</system-reminder>")
    assert prompts.extract(body(user(REMINDER, "go"), {"role": "assistant", "content": []}, user(tool_result, note))) == (2, "also record prompts")


def test_slash_commands():
    command = "<command-name>/review</command-name>\n<command-message>review</command-message>\n<command-args>the diff</command-args>\n"
    # A skill or custom command: its name and arguments, not the instructions it expands to.
    assert prompts.extract(body(user(command, "Base directory for this skill: /x\n# Review\nLong instructions"))) == (0, "/review the diff")
    # One Claude Code ran itself (/compact) is no prompt; what was typed after it is.
    local = ["<local-command-caveat>The command below was run directly in Claude Code.</local-command-caveat>\n",
             "<command-name>/compact</command-name>\n<command-message>compact</command-message>\n<command-args></command-args>\n",
             "<local-command-stdout>Compacted</local-command-stdout>\n"]
    assert prompts.extract(body(user(*local, "say bye"))) == (0, "say bye")
    assert prompts.extract(body(user(*local))) is None


def test_long_prompts_are_cut_and_titles_are_one_short_line():
    assert len(prompts.extract(body(user("x" * (prompts.MAX_PROMPT + 5))))[1]) == prompts.MAX_PROMPT
    assert prompts.title_of("\n  fix   the\tlogin \nsecond line") == "fix the login"
    assert prompts.title_of("y" * 200) == "y" * 79 + "…"


def test_which_requests_are_looked_at():
    d = body(user("hi"))
    assert prompts.wanted("s", "POST", "/v1/messages", {}, d)
    assert not prompts.wanted(None, "POST", "/v1/messages", {}, d)                                # SDKs and curl: contents.py
    assert not prompts.wanted("s", "POST", "/v1/messages", {prompts.AGENT_HEADER: "a1"}, d)       # a subagent
    assert not prompts.wanted("s", "POST", "/v1/messages", {}, {**d, "tools": []})                # a side request
    assert not prompts.wanted("s", "POST", "/v1/messages/count_tokens", {}, d) and not prompts.wanted("s", "POST", "/v1/messages", {}, None)


# ---- through the gateway ----

@pytest.fixture
def setup(cfg, db, anthropic):
    conn = db[1]
    admin_id, admin_key = create_user(conn, "admin", role="admin")
    uid, key = create_user(conn, "alice")
    seed_oauth(conn, access="oauth-secret-token")
    anthropic.default = lambda req: JSONResponse(message_json(text="the answer"))
    h = {"Authorization": f"Bearer {key}", "anthropic-beta": "claude-code-20250219", "user-agent": "claude-cli/2.1.326 (external, cli)",
         "x-claude-code-session-id": "sess-1"}
    return make_gateway(cfg, conn, anthropic), conn, uid, h, key, admin_key


def kept(conn):
    return [(r["user_id"], r["session_id"], r["seq"], r["text"]) for r in conn.execute("SELECT * FROM prompts ORDER BY id")]


def titles(conn):
    return {(r["user_id"], r["session_id"]): r["title"] for r in conn.execute("SELECT * FROM session_titles")}


async def test_a_prompt_is_kept_once_with_its_request_and_never_the_reply(setup):
    gw, conn, uid, h, key, admin_key = setup
    first = body(user(REMINDER, "fix the login\nit loops"), ENV)
    tool_turn = body(*first["messages"], {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
                     user({"type": "tool_result", "tool_use_id": "t1", "content": "secret output"}))
    second = body(*tool_turn["messages"], {"role": "assistant", "content": [{"type": "text", "text": "fixed"}]}, user("thanks"))
    async with asgi_client(create_app(gw)) as c:
        for b in (first, first, tool_turn, second):   # the second `first` is a retry
            assert (await c.post("/v1/messages", json=b, headers=h)).status_code == 200
        await c.post("/v1/messages", json=first, headers={**h, prompts.AGENT_HEADER: "a1"})
        await c.post("/v1/messages", json=first, headers={k: v for k, v in h.items() if k != "x-claude-code-session-id"})
    assert kept(conn) == [(uid, "sess-1", 0, "fix the login\nit loops"), (uid, "sess-1", 5, "thanks")]
    row = conn.execute("SELECT request_id, at FROM prompts ORDER BY id LIMIT 1").fetchone()
    assert row["request_id"] == conn.execute("SELECT MIN(id) FROM requests").fetchone()[0] and row["at"] <= time.time()
    stored = " ".join(str(v) for r in conn.execute("SELECT * FROM prompts") for v in tuple(r))
    assert "the answer" not in stored and "secret output" not in stored and "Attribution" not in stored
    assert conn.execute("SELECT COUNT(*) FROM request_contents").fetchone()[0] == 0


async def test_a_refused_request_keeps_its_prompt(setup):
    gw, conn, uid, h, key, admin_key = setup
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,0)", (uid, "requests_daily", "*", "1", "count"))
    async with asgi_client(create_app(gw)) as c:
        assert (await c.post("/v1/messages", json=body(user("first")), headers=h)).status_code == 200
        assert (await c.post("/v1/messages", json=body(user("first"), {"role": "assistant", "content": "ok"}, user("are you there?")), headers=h)).status_code == 429
    assert kept(conn) == [(uid, "sess-1", 0, "first"), (uid, "sess-1", 2, "are you there?")]


async def test_the_first_prompt_names_a_session_until_claude_code_titles_it(setup, anthropic):
    gw, conn, uid, h, key, admin_key = setup
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json=body(user(REMINDER, "fix the login")), headers=h)
        await c.post("/v1/messages", json=body(user("fix the login"), {"role": "assistant", "content": "ok"}, user("and the logout")), headers=h)
        assert titles(conn) == {(uid, "sess-1"): "fix the login"}
        anthropic.default = lambda req: JSONResponse(message_json(text='{"title": "Login redirect loop"}'))
        await c.post("/v1/messages", json=TITLE_REQ, headers=h)
        assert titles(conn) == {(uid, "sess-1"): "Login redirect loop"}
        await c.post("/v1/messages", json=body(user("one more")), headers=h)
    assert titles(conn) == {(uid, "sess-1"): "Login redirect loop"}
    assert [p[3] for p in kept(conn)] == ["fix the login", "and the logout", "one more"]   # the title request holds no prompt


async def test_only_the_admin_reads_prompts(setup):
    gw, conn, uid, h, key, admin_key = setup
    async with asgi_client(create_app(gw)) as c:
        await c.post("/v1/messages", json=body(user("my secret plan")), headers=h)
    bearer = lambda k: {"Authorization": f"Bearer {k}"}   # noqa: E731
    async with asgi_client(create_dashboard_app(gw)) as c:
        url = f"/api/sessions/sess-1/prompts?user_id={uid}"
        assert (await c.get(url, headers=bearer(key))).status_code == 403
        assert (await c.get(url)).status_code in (401, 403)
        got = (await c.get(url, headers=bearer(admin_key))).json()["prompts"]
        assert [p["text"] for p in got] == ["my secret plan"] and got[0]["at"] > 0
        assert (await c.get("/api/sessions/sess-1/prompts?user_id=999", headers=bearer(admin_key))).json() == {"prompts": []}
        mine = (await c.get("/api/sessions", headers=bearer(key))).json()["sessions"][0]
        assert "prompts" not in mine and "user_id" not in mine and mine["title"] == "my secret plan"
        theirs = (await c.get("/api/sessions", headers=bearer(admin_key))).json()["sessions"][0]
        assert (theirs["prompts"], theirs["user_id"]) == (1, uid)
        for route in ("/api/requests", "/api/overview", "/api/me/status"):
            assert "my secret plan" not in (await c.get(route, headers=bearer(key))).text.replace('"title":"my secret plan"', "")


def test_prompts_go_at_retention_even_when_usage_is_kept_and_with_their_user(db):
    conn = db[1]
    uid, _ = create_user(conn, "alice")
    now = time.time()
    prompts.save(conn, uid, "old", None, now - 200 * 86400, 0, "old prompt")
    prompts.save(conn, uid, "new", None, now, 0, "new prompt")
    assert cleanup(conn, 180, keep_usage=True)["prompts"] == 1
    assert [r["text"] for r in conn.execute("SELECT text FROM prompts")] == ["new prompt"]
    revoke(conn, uid)
    delete_user(conn, uid)
    assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
