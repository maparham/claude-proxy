"""The client-side scripts, run as subprocesses against a stub gateway (spec 4, 5, tests 14-16)."""
import json
import os
import shutil
import signal
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
        self.account_line: str | None = "alice · user · key sk-proxy-ab1… (your first key)"   # ?format=account
        self.models: dict = {"data": [], "has_more": False}
        self.models_status = 200
        self.logout = (200, {"ok": True, "revoked": True})   # POST /api/me/logout: status, body
        self.tokens: list[tuple[int, dict]] = []   # answers to /api/device/token, in order; then pending
        self.revoked: set[str] = set()   # keys /api/me/status answers 401 for, as for a computer removed in the dashboard
        self.install: str | None = None   # GET /install, the dashboard's installer; None: 404
        self.requests: list[tuple[str, dict]] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                stub.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                if self.path.startswith("/api/me/status") and self.headers.get("authorization", "").removeprefix("Bearer ") in stub.revoked:
                    code, body, ctype = 401, b'{"detail": "Not signed in."}', "application/json"
                elif self.path.startswith("/api/me/status"):
                    line = stub.account_line if "format=account" in self.path else stub.status_line
                    code, body, ctype = (200 if line is not None else 503), ((line or "") + "\n").encode(), "text/plain; charset=utf-8"
                elif self.path.startswith("/v1/models"):
                    code, body, ctype = stub.models_status, json.dumps(stub.models).encode(), "application/json"
                elif self.path == "/install" and stub.install is not None:
                    code, body, ctype = 200, stub.install.encode(), "text/x-shellscript"
                elif self.path == "/health":
                    code, body, ctype = 200, b'{"ok": true}', "application/json"
                else:
                    code, body, ctype = 404, b"{}", "application/json"
                self.send_response(code)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                stub.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                if self.path == "/api/me/logout":
                    code, body = stub.logout
                elif self.path == "/api/device/start":
                    code, body = 200, {"device_code": "dc-1", "user_code": "ABCD-EFGH", "interval": 0.1, "expires_in": 20,
                                       "verification_uri_complete": f"{stub.url}/dashboard#authorize/ABCD-EFGH"}
                elif self.path == "/api/device/token":
                    code, body = stub.tokens.pop(0) if stub.tokens else (400, {"error": "authorization_pending"})
                else:
                    code, body = 404, {}
                data = json.dumps(body, separators=(",", ":")).encode()   # as FastAPI writes it
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.server.shutdown()


def run(args, env, stdin=""):
    # SIGINT back to default: a suite started in the background inherits it ignored, and sh can't trap a signal that
    # was ignored on entry, so the logout test's fake claude would never record the hook's Ctrl-Cs.
    return subprocess.run(args, input=stdin, capture_output=True, text=True, env=env, timeout=60,
                          preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))


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
    assert json.loads(r.stdout) == {"systemMessage": "Gateway: alice · daily 80/100 req 80%"}


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
    assert json.loads(warn(warn_env).stdout)["systemMessage"].endswith("$100/$100 100%")


def test_warn_again_after_15_minutes(stub, warn_env):
    stub.status_line = "alice · 5h 85%"
    assert warn(warn_env).stdout
    fresh(warn_env)
    state = next(Path(warn_env["TMPDIR"]).glob("*.warned"))
    old = state.stat().st_mtime - 901
    os.utime(state, (old, old))
    assert warn(warn_env).stdout


def test_warn_no_reentry_after_dropping_bands_within_15_minutes(stub, warn_env):
    stub.status_line = "alice · daily 80/100 req"
    assert warn(warn_env).stdout
    fresh(warn_env)
    stub.status_line = "alice · daily 70/100 req"
    assert warn(warn_env).stdout == ""
    fresh(warn_env)
    stub.status_line = "alice · daily 80/100 req"
    assert warn(warn_env).stdout == ""


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
    assert json.loads(warn(warn_env).stdout) == {"systemMessage": 'Gateway: a"b\\c · daily 90/100 req 90%'}


def test_statusline_mode_still_colours_figures(stub, warn_env):
    stub.status_line = "alice · daily 90/100 req"
    r = run(["sh", str(STATUSLINE)], warn_env)
    assert r.returncode == 0
    assert "\033[33m90/100 req 90%\033[36m" in r.stdout and r.stdout.startswith("\033[36m◆ alice")


def plain(text):
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


def test_statusline_shows_each_used_limit_pair_as_a_percentage(stub, warn_env):
    stub.status_line = "alice · daily $305/$500 · weekly 4.2M/5.0M tok (resets in 7.0 days) · 5h 30% · x 996/1000 req"
    r = run(["sh", str(STATUSLINE)], warn_env)
    assert plain(r.stdout) == ("◆ alice · daily $305/$500 61% · weekly 4.2M/5.0M tok 84% (resets in 7.0 days) · 5h 30%"
                               " · x 996/1000 req 99%\n")                  # rounded down: 100% only once reached


def own_statusline(tmp_path, body):
    script = tmp_path / "my line.sh"      # a space, so the command needs quoting
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


def test_statusline_then_shows_the_users_own_line_after_the_gateway_line(stub, warn_env, tmp_path):
    import shlex
    mine = own_statusline(tmp_path, 'printf "mine %s" "$(cat)"\n')
    r = run(["sh", str(STATUSLINE), "--then", shlex.quote(str(mine))], warn_env, stdin='{"model": "x"}')
    assert r.returncode == 0, r.stderr
    assert plain(r.stdout) == '◆ alice · daily 10/100 req 10% | mine {"model": "x"}\n'


def test_statusline_then_shows_the_gateway_line_once_when_the_users_line_has_it_too(stub, warn_env, tmp_path):
    import shlex
    mine = own_statusline(tmp_path, f'printf "%s mine" "$(sh {shlex.quote(str(STATUSLINE))} </dev/null)"\n')
    r = run(["sh", str(STATUSLINE), "--then", shlex.quote(str(mine))], warn_env)
    assert plain(r.stdout) == "◆ alice · daily 10/100 req 10% |  mine\n"


def test_statusline_then_without_dashboard_still_shows_the_users_line(warn_env, tmp_path):
    import shlex
    mine = own_statusline(tmp_path, 'echo mine\n')
    env = {k: v for k, v in warn_env.items() if k != "CLAUDE_GATEWAY_DASHBOARD"}
    r = run(["sh", str(STATUSLINE), "--then", shlex.quote(str(mine))], env)
    assert (r.returncode, r.stdout) == (0, "mine\n")


def test_statusline_skips_the_percentage_of_a_zero_limit(stub, warn_env):
    stub.status_line = "alice · daily $0/$0"
    assert plain(run(["sh", str(STATUSLINE)], warn_env).stdout) == "◆ alice · daily $0/$0\n"


def test_warning_and_usage_carry_the_percentage_too(stub, warn_env):
    stub.status_line = "alice · daily 90/100 req"
    assert json.loads(warn(warn_env).stdout)["systemMessage"] == "Gateway: alice · daily 90/100 req 90%"
    fresh(warn_env)
    assert json.loads(usage_prompt(warn_env).stdout)["reason"].startswith("Gateway: alice · daily 90/100 req 90% ·")


# ---------- statusline.sh --warn answers gclaude's /usage (the hook blocks the prompt; no model call) ----------

def gclaude_config(env, usage_text="<!-- # Installed by claude-gateway on --gclaude. -->\n"):
    """A config folder like gclaude's: CLAUDE_CONFIG_DIR with its commands/usage.md."""
    cfg = Path(env["HOME"]) / "gcfg"
    (cfg / "commands").mkdir(parents=True, exist_ok=True)
    (cfg / "commands" / "usage.md").write_text(usage_text)
    return {**env, "CLAUDE_CONFIG_DIR": str(cfg)}


def usage_prompt(env, prompt="/usage", config=True):
    return run(["sh", str(STATUSLINE), "--warn"], gclaude_config(env) if config else env,
               stdin=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt}))


def test_usage_prompt_is_left_alone_outside_gclaude(stub, warn_env):
    stub.status_line = "alice · daily 10/100 req"
    r = usage_prompt(warn_env, config=False)                         # plain claude, e.g. global mode
    assert (r.returncode, r.stdout) == (0, "")


def test_usage_prompt_is_left_alone_when_gclaude_has_its_own_usage_command(stub, warn_env):
    stub.status_line = "alice · daily 10/100 req"
    env = gclaude_config(warn_env, usage_text="my own usage command\n")
    r = run(["sh", str(STATUSLINE), "--warn"], env, stdin=json.dumps({"prompt": "/usage"}))
    assert (r.returncode, r.stdout) == (0, "")


def test_usage_prompt_is_blocked_with_the_gateway_line_at_any_level(stub, warn_env):
    stub.status_line = 'a"b · daily 10/100 req'
    r = usage_prompt(warn_env)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    assert out["reason"] == f'Gateway: a"b · daily 10/100 req 10% · details: {stub.url}/dashboard'
    assert not list(Path(warn_env["TMPDIR"]).glob("*.warned"))   # not a warning: the 80% bands are untouched


def test_usage_prompt_skips_the_status_cache(stub, warn_env):
    stub.status_line = "alice · daily 10/100 req"
    assert warn(warn_env).stdout == ""                              # fills the 30-second cache
    stub.status_line = "alice · daily 11/100 req"
    assert "11/100" in json.loads(usage_prompt(warn_env).stdout)["reason"]


def test_usage_prompt_says_when_the_gateway_is_down(stub, warn_env):
    stub.status_line = None
    out = json.loads(usage_prompt(warn_env).stdout)
    assert out["decision"] == "block" and "unavailable" in out["reason"]


def test_usage_prompt_without_dashboard_still_blocks(warn_env):
    del warn_env["CLAUDE_GATEWAY_DASHBOARD"]
    out = json.loads(usage_prompt(warn_env).stdout)
    assert out["decision"] == "block" and "gclaude update" in out["reason"]


@pytest.mark.parametrize("prompt", ["what does /usage show?", "/usages", "/usage-report"])
def test_other_prompts_mentioning_usage_are_left_alone(stub, warn_env, prompt):
    stub.status_line = "alice · daily 10/100 req"
    r = usage_prompt(warn_env, prompt)
    assert (r.returncode, r.stdout) == (0, "")


def account_prompt(env, prompt="/account", text="<!-- # Installed by claude-gateway on --gclaude. -->\n"):
    env = gclaude_config(env)
    (Path(env["CLAUDE_CONFIG_DIR"]) / "commands" / "account.md").write_text(text)
    return run(["sh", str(STATUSLINE), "--warn"], env, stdin=json.dumps({"prompt": prompt}))


def test_account_mode_prints_the_account_and_dashboard_link(stub, warn_env):
    r = run(["sh", str(STATUSLINE), "--account"], warn_env)
    assert r.stdout == f"Account: alice · user · key sk-proxy-ab1… (your first key) · dashboard: {stub.url}/dashboard\n"
    assert "format=account" in stub.requests[-1][0]
    stub.account_line = None
    r = run(["sh", str(STATUSLINE), "--account"], warn_env)
    assert r.stdout == f"Account details unavailable; see {stub.url}/dashboard\n"


def test_account_prompt_goes_to_the_model_while_it_can_answer(stub, warn_env):
    stub.status_line = "alice · daily 50/100 req"
    r = account_prompt(warn_env)
    assert (r.returncode, r.stdout) == (0, "")


def test_account_prompt_is_answered_by_the_hook_when_a_limit_is_reached(stub, warn_env):
    stub.status_line = "alice · credit $5.00/$5.00"
    out = json.loads(account_prompt(warn_env).stdout)
    assert out == {"decision": "block", "reason": f"Account: alice · user · key sk-proxy-ab1… (your first key) · "
                                                  f"dashboard: {stub.url}/dashboard · limit reached: alice · credit $5.00/$5.00 100%"}


def test_account_prompt_is_answered_by_the_hook_when_the_gateway_is_down(stub, warn_env):
    stub.status_line = stub.account_line = None
    out = json.loads(account_prompt(warn_env).stdout)
    assert out == {"decision": "block", "reason": f"Account details unavailable; see {stub.url}/dashboard"}


@pytest.mark.parametrize("prompt,text", [("/account", "my own account command\n"), ("/accounts", None), ("my /account", None)])
def test_other_account_prompts_are_left_alone(stub, warn_env, prompt, text):
    r = account_prompt(warn_env, prompt, **({"text": text} if text else {}))
    assert (r.returncode, r.stdout) == (0, "")


def logout_prompt(env, prompt="/logout_gclaude", text="<!-- # Installed by claude-gateway on --gclaude. -->\n"):
    """/logout_gclaude in a gclaude-like folder whose settings.json and client.json hold the key sk-proxy-k."""
    env = gclaude_config(env)
    cfg = Path(env["CLAUDE_CONFIG_DIR"])
    (cfg / "commands" / "logout_gclaude.md").write_text(text)
    settings = {"env": {"ANTHROPIC_BASE_URL": "https://gw", "ANTHROPIC_AUTH_TOKEN": "sk-proxy-k"}, "model": "opus"}
    for name in ("settings.json", "settings.json.bak-claude-gateway"):
        (cfg / name).write_text(json.dumps(settings))
    client = Path(env["HOME"]) / "client.json"
    client.write_text(json.dumps({"url": "https://gw", "key": "sk-proxy-k", "gclaude": {"links": []}}))
    return run(["sh", str(STATUSLINE), "--warn"], {**env, "CLAUDE_GATEWAY_CLIENT": str(client)},
               stdin=json.dumps({"prompt": prompt}))


def signed_out(env):
    """The key is gone from every file that held it; the rest of each file stays."""
    cfg = Path(env["HOME"]) / "gcfg"
    assert json.loads((cfg / "settings.json").read_text()) == {"env": {"ANTHROPIC_BASE_URL": "https://gw"}, "model": "opus"}
    assert not (cfg / "settings.json.bak-claude-gateway").exists()
    assert json.loads((Path(env["HOME"]) / "client.json").read_text()) == {"url": "https://gw", "gclaude": {"links": []}}
    return True


def test_logout_prompt_revokes_this_computers_key_and_removes_it(stub, warn_env):
    out = json.loads(logout_prompt(warn_env).stdout)
    assert out["continue"] is False and "revoked on the gateway" in out["stopReason"]
    assert "run gclaude again" in out["stopReason"]
    path, headers = stub.requests[-1]
    assert path == "/api/me/logout" and headers["authorization"] == "Bearer sk-proxy-k"
    assert signed_out(warn_env)


def test_logout_prompt_with_the_first_key_says_it_still_works_elsewhere(stub, warn_env):
    stub.logout = (200, {"ok": True, "revoked": False})
    out = json.loads(logout_prompt(warn_env).stdout)
    assert "your first key" in out["stopReason"] and signed_out(warn_env)


def test_logout_prompt_with_a_key_the_gateway_no_longer_takes(stub, warn_env):
    stub.logout = (401, {"error": "Invalid or revoked gateway key."})
    out = json.loads(logout_prompt(warn_env).stdout)
    assert "no longer accepted it" in out["stopReason"] and signed_out(warn_env)


def test_logout_prompt_still_signs_out_here_when_the_gateway_is_down(stub, warn_env):
    stub.logout = (502, {})
    out = json.loads(logout_prompt(warn_env).stdout)
    assert "could not be reached" in out["stopReason"] and f"{stub.url}/dashboard" in out["stopReason"]
    assert signed_out(warn_env)


def test_logout_prompt_ends_the_claude_session_that_ran_it(stub, warn_env, tmp_path):
    """Two SIGINTs, as Ctrl-C twice: Claude Code then exits the usual way. Only to a claude running the hook."""
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.symlink_to("/bin/sh")   # a process named claude (a copy would lose its code signature on macOS)
    log = tmp_path / "signals"
    env = gclaude_config(warn_env)
    (Path(env["CLAUDE_CONFIG_DIR"]) / "commands" / "logout_gclaude.md").write_text("<!-- # Installed by claude-gateway on --gclaude. -->\n")
    (Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text("{}")
    prompt = tmp_path / "prompt.json"
    prompt.write_text(json.dumps({"prompt": "/logout_gclaude"}))
    script = (f"trap 'echo INT >> {log}' INT; sh {STATUSLINE} --warn < {prompt}; "
              "i=0; while [ $i -lt 30 ]; do sleep 0.1; i=$((i + 1)); done")
    r = run([str(fake), "-c", script], {**env, "CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert json.loads(r.stdout)["stopReason"].startswith("Signed out") and "gclaude is closing" in r.stdout
    assert log.read_text() == "INT\nINT\n"
    log.unlink()
    run([str(fake), "-c", script], env)   # not run by Claude Code (no CLAUDE_PROJECT_DIR): nothing is signalled
    assert not log.exists()


@pytest.mark.parametrize("prompt,text", [("/logout_gclaude", "my own command\n"), ("/logout", None), ("/logout_gclauded", None), ("how do I /logout_gclaude", None)])
def test_other_logout_prompts_are_left_alone(stub, warn_env, prompt, text):
    r = logout_prompt(warn_env, prompt, **({"text": text} if text else {}))
    assert (r.returncode, r.stdout) == (0, "")
    assert json.loads((Path(warn_env["HOME"]) / "client.json").read_text())["key"] == "sk-proxy-k"


# ---------- claude-gateway ----------

@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
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
    r = cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full")
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
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert cg(home, "off").returncode == 0
    assert "hooks" not in json.loads((home / ".claude" / "settings.json").read_text())


@pytest.mark.parametrize("malformed", [None, "nope"])
def test_off_leaves_a_malformed_user_prompt_submit_alone(stub, home, malformed):
    settings = home / ".claude" / "settings.json"
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    s = json.loads(settings.read_text())
    s["hooks"]["UserPromptSubmit"] = malformed
    settings.write_text(json.dumps(s, indent=2) + "\n")
    r = cg(home, "off")
    assert r.returncode == 0, r.stderr
    assert json.loads(settings.read_text())["hooks"]["UserPromptSubmit"] == malformed


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
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    assert cg(home, "off", "--opencode").returncode == 0
    assert conf.read_text() == text
    assert not key_file.exists() and not agent.exists()
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    edited = json.loads(conf.read_text())
    edited["theme"] = "light"
    edited["provider"]["theirs"] = {"npm": "y"}
    conf.write_text(json.dumps(edited, indent=2) + "\n")
    assert cg(home, "off", "--opencode").returncode == 0
    assert json.loads(conf.read_text()) == {**original, "theme": "light", "provider": {"mine": {"npm": "x"}, "theirs": {"npm": "y"}}}


def test_opencode_off_deletes_a_config_it_created(stub, home):
    stub.models = MODELS
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    assert cg(home, "off", "--opencode").returncode == 0
    conf, key_file, agent = oc_paths(home)
    assert not conf.exists() and not key_file.exists() and not agent.exists()
    assert "opencode: not set up" in cg(home, "status").stdout


def test_opencode_rerun_refreshes_models_and_spares_an_edited_agent(stub, home):
    stub.models = {"data": [MODELS["data"][0]], "has_more": False}
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
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
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-bad")
    assert r.returncode == 1 and "did not accept the OpenCode key (HTTP 403)" in r.stderr
    conf, key_file, agent = oc_paths(home)
    assert not conf.exists() and not key_file.exists() and not agent.exists()


def test_opencode_on_leaves_a_non_json_config_alone(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    conf.parent.mkdir(parents=True)
    conf.write_text('{\n  // a comment\n  "theme": "dark"\n}\n')
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k")
    assert r.returncode == 1 and "not plain JSON" in r.stderr
    assert conf.read_text() == '{\n  // a comment\n  "theme": "dark"\n}\n'
    assert not key_file.exists()


def test_opencode_on_refuses_a_gateway_provider_it_did_not_add(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    conf.parent.mkdir(parents=True)
    conf.write_text(json.dumps({"provider": {"gateway": {"npm": "mine"}}}, indent=2) + "\n")
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k")
    assert r.returncode == 1 and "did not add" in r.stderr
    assert json.loads(conf.read_text()) == {"provider": {"gateway": {"npm": "mine"}}}


def test_opencode_on_never_touches_opencode_jsonc(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    conf.parent.mkdir(parents=True)
    jsonc = conf.parent / "opencode.jsonc"
    jsonc.write_text('{\n  // mine\n  "theme": "dark"\n}\n')
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    assert jsonc.read_text() == '{\n  // mine\n  "theme": "dark"\n}\n'
    assert "gateway" in json.loads(conf.read_text())["provider"]


def test_routes_key_without_opencode_is_refused(stub, home):
    r = cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full", "--routes-key", "sk-proxy-r-k")
    assert r.returncode == 1 and "--routes-key goes with --opencode" in r.stderr


def test_opencode_on_refuses_a_provider_field_that_is_not_an_object(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    conf.parent.mkdir(parents=True)
    conf.write_text(json.dumps({"provider": ["not", "an", "object"]}, indent=2) + "\n")
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k")
    assert r.returncode == 1 and "not a JSON object" in r.stderr
    assert json.loads(conf.read_text()) == {"provider": ["not", "an", "object"]}
    assert not key_file.exists() and not agent.exists()
    assert not (home / ".config" / "claude-gateway" / "client.json").exists()


# ---------- claude-gateway --opencode: half-installed recovery and shared URL (review fixes) ----------

def test_opencode_on_survives_a_dangling_agent_symlink_and_off_still_fully_undoes(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    agent.parent.mkdir(parents=True)
    agent.symlink_to(agent.parent / "no-such-dir" / "target")   # broken symlink whose target's parent doesn't exist
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k")
    assert "Traceback" not in r.stderr
    assert cg(home, "off", "--opencode").returncode == 0
    assert not key_file.exists()
    assert not conf.exists() or "gateway" not in json.loads(conf.read_text()).get("provider", {})


def test_opencode_on_survives_agents_existing_as_a_plain_file(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    agent.parent.parent.mkdir(parents=True)                      # ~/.config/opencode
    agent.parent.write_text("not a directory")                   # agents is a file, not a directory
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k")
    assert "Traceback" not in r.stderr
    assert cg(home, "off", "--opencode").returncode == 0
    assert not key_file.exists()
    assert not conf.exists() or "gateway" not in json.loads(conf.read_text()).get("provider", {})


def test_opencode_off_keeps_the_key_file_when_client_json_is_gone_but_the_provider_remains(stub, home):
    stub.models = MODELS
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    conf, key_file, agent = oc_paths(home)
    (home / ".config" / "claude-gateway" / "client.json").unlink()
    assert cg(home, "off", "--opencode").returncode == 0
    assert key_file.exists()
    assert "gateway" in json.loads(conf.read_text())["provider"]


def test_opencode_on_does_not_move_claude_codes_url(stub, home):
    stub.models = MODELS
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    client = home / ".config" / "claude-gateway" / "client.json"
    url_a = json.loads(client.read_text())["url"]
    stub_b = Stub()
    try:
        stub_b.models = MODELS
        assert cg(home, "on", "--opencode", "--url", stub_b.url, "--routes-key", "sk-proxy-r-k").returncode == 0
        data = json.loads(client.read_text())
        assert data["url"] == url_a and url_a == stub.url.rstrip("/")
        assert data["opencode"]["url"] == stub_b.url
        r = cg(home, "on", "--opencode")               # no --url: must reuse B, not Claude Code's A
        assert r.returncode == 0, r.stderr
        path, _ = stub_b.requests[-1]
        assert path == "/v1/models"
    finally:
        stub_b.server.shutdown()


def test_plain_on_refuses_when_only_opencode_is_configured(stub, home):
    stub.models = MODELS
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    r = cg(home, "on")
    assert r.returncode == 1 and "No gateway configured yet" in r.stderr


# ---------- claude-gateway: full/routes key mismatches (review finding 1) ----------

CLAUDE_MODELS = {"data": [{"type": "model", "id": "claude-sonnet-5", "display_name": "Claude Sonnet 5",
                          "created_at": "2026-01-01T00:00:00Z"}],
                 "has_more": False, "first_id": "claude-sonnet-5", "last_id": "claude-sonnet-5"}


def test_opencode_on_refuses_a_key_that_is_not_a_routes_key(stub, home):
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-fullkey")
    assert r.returncode == 1
    assert "This is a Claude Code key; ask the admin for an OpenCode key (claude-proxy user routes-key)." in r.stderr
    conf, key_file, agent = oc_paths(home)
    assert not conf.exists() and not key_file.exists() and not agent.exists()
    assert not (home / ".config" / "claude-gateway" / "client.json").exists()
    assert stub.requests == []   # refused before ever reaching the gateway


def test_plain_on_refuses_a_routes_key(stub, home):
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    settings = home / ".claude" / "settings.json"
    client = home / ".config" / "claude-gateway" / "client.json"
    before = settings.read_bytes()
    n_requests = len(stub.requests)
    r = cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-r-routeskey")
    assert r.returncode == 1
    assert "This is an OpenCode key; Claude Code needs your Claude Code key." in r.stderr
    assert settings.read_bytes() == before
    assert json.loads(client.read_text())["key"] == "sk-proxy-full"
    assert len(stub.requests) == n_requests   # refused before ever reaching the gateway again


def test_opencode_on_refuses_when_the_gateway_answers_with_claude_models(stub, home):
    stub.models = CLAUDE_MODELS
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-fooled")
    assert r.returncode == 1
    assert "looks like a Claude Code key" in r.stderr
    conf, key_file, agent = oc_paths(home)
    assert not conf.exists() and not key_file.exists() and not agent.exists()
    assert not (home / ".config" / "claude-gateway" / "client.json").exists()


# ---------- claude-gateway --opencode: a symlinked opencode.json survives (review finding 2) ----------

def test_opencode_on_off_preserves_a_symlinked_opencode_json(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    conf.parent.mkdir(parents=True)
    real = home / "dotfiles" / "opencode.json"
    real.parent.mkdir(parents=True)
    original = {"$schema": "https://opencode.ai/config.json", "theme": "dark"}
    real.write_text(json.dumps(original, indent=2) + "\n")
    conf.symlink_to(real)
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    assert conf.is_symlink() and conf.resolve() == real.resolve()
    assert "gateway" in json.loads(real.read_text())["provider"]
    assert cg(home, "off", "--opencode").returncode == 0
    assert conf.is_symlink() and conf.resolve() == real.resolve()
    assert real.read_text() == json.dumps(original, indent=2) + "\n"   # spec 4: byte-identical after on/off


# ---------- claude-gateway off --opencode: message reflects what happened (review finding 3) ----------

def test_off_opencode_says_nothing_was_installed(stub, home):
    r = cg(home, "off", "--opencode")
    assert r.returncode == 0
    assert "nothing to undo" in r.stdout


def test_off_opencode_says_it_removed_the_provider(stub, home):
    stub.models = MODELS
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    r = cg(home, "off", "--opencode")
    assert r.returncode == 0
    assert "key file was deleted" in r.stdout


def test_off_opencode_says_it_kept_the_key(stub, home):
    stub.models = MODELS
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    (home / ".config" / "claude-gateway" / "client.json").unlink()
    r = cg(home, "off", "--opencode")
    assert r.returncode == 0
    assert "did not remove" in r.stdout and "kept" in r.stdout


# ---------- claude-gateway off: rejects unknown arguments, including the --opencode typo (review finding 4) ----------

def test_off_rejects_a_typo_instead_of_falling_through_to_claude_code_off(stub, home):
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    settings = home / ".claude" / "settings.json"
    before = settings.read_text()
    r = cg(home, "off", "--opencdoe")
    assert r.returncode == 1
    assert settings.read_text() == before


def test_off_rejects_extra_arguments_after_opencode(stub, home):
    r = cg(home, "off", "--opencode", "extra")
    assert r.returncode == 1


# ---------- claude-gateway --opencode: an empty "provider": {} round-trips (review finding 5) ----------

def test_opencode_on_off_keeps_an_originally_empty_provider_object(stub, home):
    stub.models = MODELS
    conf, key_file, agent = oc_paths(home)
    conf.parent.mkdir(parents=True)
    original = {"$schema": "https://opencode.ai/config.json", "provider": {}}
    text = json.dumps(original, indent=2) + "\n"
    conf.write_text(text)
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    assert cg(home, "off", "--opencode").returncode == 0
    assert conf.read_text() == text


# ---------- claude-gateway --gclaude: Claude Code on the gateway beside the machine's own `claude` ----------

def gc_paths(home):
    gdir = home / ".config" / "claude-gateway" / "claude"
    return gdir, gdir / "settings.json", home / ".local" / "bin" / "gclaude"


def own_claude(home):
    """This machine's own Claude Code setup, which --gclaude must never change."""
    own = home / ".claude"
    (own / "agents").mkdir(parents=True)
    (own / "agents" / "mine.md").write_text("mine\n")
    (own / "CLAUDE.md").write_text("my rules\n")
    settings = own / "settings.json"
    settings.write_text(json.dumps({"model": "opus", "env": {"FOO": "1"}}, indent=2) + "\n")
    (home / ".claude.json").write_text(json.dumps({"theme": "light", "hasCompletedOnboarding": True}))
    return settings


def test_gclaude_on_sets_up_its_own_dir_and_leaves_claude_code_alone(stub, home):
    settings = own_claude(home)
    before = settings.read_bytes()
    r = cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 0, r.stderr
    assert "gclaude" in r.stdout
    assert settings.read_bytes() == before
    gdir, gsettings, launcher = gc_paths(home)
    s = json.loads(gsettings.read_text())
    assert s["env"]["ANTHROPIC_BASE_URL"] == stub.url and s["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-full"
    assert s["env"]["CLAUDE_GATEWAY_DASHBOARD"]
    assert s["statusLine"]["command"].endswith("statusline.sh")
    assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("statusline.sh --warn")
    assert os.readlink(gdir / "CLAUDE.md") == str(home / ".claude" / "CLAUDE.md")
    assert os.readlink(gdir / "agents") == str(home / ".claude" / "agents")
    assert sorted(p.name for p in (gdir / "commands").iterdir()) == ["account.md", "logout_gclaude.md", "usage.md"]   # gclaude's own only
    assert json.loads((gdir / ".claude.json").read_text()) == {"hasCompletedOnboarding": True, "theme": "light"}
    assert os.access(launcher, os.X_OK)


ONE_M = {"ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-fable-5-1[1m]", "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5[1m]",
         "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5[1m]"}


def test_gclaude_picks_the_1m_context_forms_of_fable_opus_and_sonnet(stub, home):
    """Without a claude.ai login Claude Code gives the plain model names a 200K window; the [1m] forms get 1M."""
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, gsettings, _ = gc_paths(home)
    env = json.loads(gsettings.read_text())["env"]
    assert {k: env[k] for k in ONE_M} == ONE_M
    assert cg(home, "off", "--gclaude").returncode == 0
    assert json.loads(gsettings.read_text()) == {}


def test_gclaude_keeps_a_model_the_user_chose_for_an_alias(stub, home):
    _, gsettings, _ = gc_paths(home)
    gsettings.parent.mkdir(parents=True)
    gsettings.write_text(json.dumps({"env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5"}}))
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    env = json.loads(gsettings.read_text())["env"]
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-5"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == ONE_M["ANTHROPIC_DEFAULT_FABLE_MODEL"]
    assert cg(home, "off", "--gclaude").returncode == 0
    assert json.loads(gsettings.read_text()) == {"env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5"}}


def test_gclaude_rerun_replaces_its_own_model_but_not_one_the_user_changed(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, gsettings, _ = gc_paths(home)
    s = json.loads(gsettings.read_text())
    s["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"] = "claude-fable-5"          # the user's own choice since
    s["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "claude-sonnet-4-6[1m]"   # stands in for an older id written by an earlier install
    gsettings.write_text(json.dumps(s))
    client = home / ".config" / "claude-gateway" / "client.json"
    c = json.loads(client.read_text())
    c["gclaude"]["added_models"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "claude-sonnet-4-6[1m]"
    client.write_text(json.dumps(c))
    assert cg(home, "on", "--gclaude").returncode == 0
    env = json.loads(gsettings.read_text())["env"]
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "claude-fable-5"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == ONE_M["ANTHROPIC_DEFAULT_SONNET_MODEL"]


def test_own_login_mode_leaves_the_model_aliases_alone(stub, home):
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    env = json.loads((home / ".claude" / "settings.json").read_text())["env"]
    assert {k: env[k] for k in ONE_M} == ONE_M                           # key-only: no claude.ai login either
    client = home / ".config" / "claude-gateway" / "client.json"
    data = json.loads(client.read_text())
    data["mode"] = "own-login"
    client.write_text(json.dumps(data))
    (home / ".claude" / ".credentials.json").write_text("{}")             # a claude.ai login on this machine
    assert cg(home, "on", "--global").returncode == 0
    env = json.loads((home / ".claude" / "settings.json").read_text())["env"]
    assert "ANTHROPIC_CUSTOM_HEADERS" in env and not set(ONE_M) & set(env)


def test_gclaude_launcher_runs_claude_with_its_own_config_dir_and_every_argument(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, _, launcher = gc_paths(home)
    fake = home / "fakebin"
    fake.mkdir()
    (fake / "claude").write_text('#!/bin/sh\necho "$CLAUDE_CONFIG_DIR"\nfor a in "$@"; do echo "[$a]"; done\n')
    (fake / "claude").chmod(0o755)
    r = run([str(launcher), "-p", "two words", "--resume"], {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [str(gdir), "[-p]", "[two words]", "[--resume]"]


def test_gclaude_launcher_says_when_claude_code_is_missing(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, _, launcher = gc_paths(home)
    empty = home / "emptybin"
    empty.mkdir()
    r = run(["/bin/sh", str(launcher)], {"PATH": str(empty), "HOME": str(home)})
    assert r.returncode == 127 and "Claude Code" in r.stderr


def test_gclaude_is_key_only_even_when_claude_code_uses_own_login(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    client = home / ".config" / "claude-gateway" / "client.json"
    data = json.loads(client.read_text())
    data["mode"] = "own-login"
    client.write_text(json.dumps(data))
    assert cg(home, "on", "--gclaude").returncode == 0
    env = json.loads(gc_paths(home)[1].read_text())["env"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-full" and "ANTHROPIC_CUSTOM_HEADERS" not in env
    r = cg(home, "on", "--gclaude", "--own-login")
    assert r.returncode == 1 and "gclaude always sends the gateway key" in r.stderr


def test_gclaude_refuses_a_routes_key_and_opencode_flags(stub, home):
    r = cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-r-k")
    assert r.returncode == 1 and "This is an OpenCode key" in r.stderr
    assert cg(home, "on", "--gclaude", "--opencode").returncode == 1
    assert not gc_paths(home)[0].exists()


def test_gclaude_on_refuses_a_launcher_it_did_not_install(stub, home):
    _, gsettings, launcher = gc_paths(home)
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\necho someone else's\n")
    r = cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 1 and str(launcher) in r.stderr
    assert launcher.read_text() == "#!/bin/sh\necho someone else's\n"
    assert not gsettings.exists()


def test_gclaude_leaves_its_dirs_own_files_alone(stub, home):
    own_claude(home)
    gdir, _, _ = gc_paths(home)
    gdir.mkdir(parents=True)
    (gdir / "CLAUDE.md").write_text("gateway-only rules\n")
    (gdir / ".claude.json").write_text('{"theme": "dark"}')
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert (gdir / "CLAUDE.md").read_text() == "gateway-only rules\n"
    assert (gdir / ".claude.json").read_text() == '{"theme": "dark"}'
    assert cg(home, "off", "--gclaude").returncode == 0
    assert (gdir / "CLAUDE.md").read_text() == "gateway-only rules\n"


def test_gclaude_off_undoes_its_setup_but_keeps_history(stub, home):
    settings = own_claude(home)
    before = settings.read_bytes()
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, gsettings, launcher = gc_paths(home)
    (gdir / "history.jsonl").write_text("{}\n")
    r = cg(home, "off", "--gclaude")
    assert r.returncode == 0, r.stderr
    assert "history" in r.stdout
    assert not launcher.exists()
    assert not (gdir / "CLAUDE.md").is_symlink() and not (gdir / "agents").is_symlink()
    assert json.loads(gsettings.read_text()) == {}
    assert (gdir / "history.jsonl").exists()
    assert settings.read_bytes() == before
    assert "gclaude" not in json.loads((home / ".config" / "claude-gateway" / "client.json").read_text())


def test_gclaude_statusline_keeps_the_users_own_after_the_gateway_line(stub, home):
    settings = own_claude(home)
    mine = own_statusline(home, 'echo mine\n')
    import shlex
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": shlex.quote(str(mine)), "padding": 0}}))
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, gsettings, _ = gc_paths(home)
    line = json.loads(gsettings.read_text())["statusLine"]
    assert line["padding"] == 0 and line["refreshInterval"] == 30
    tmp = home / "tmp"
    r = run(["sh", "-c", line["command"]], {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(tmp),
                                            "CLAUDE_GATEWAY_DASHBOARD": stub.url, "ANTHROPIC_AUTH_TOKEN": "sk-proxy-full"})
    assert plain(r.stdout) == "◆ alice · daily 10/100 req 10% | mine\n"
    assert cg(home, "on", "--gclaude").returncode == 0                   # a rerun doesn't wrap it twice
    assert json.loads(gsettings.read_text())["statusLine"] == line
    assert cg(home, "off", "--gclaude").returncode == 0
    assert "statusLine" not in json.loads(gsettings.read_text())


def test_gclaude_leaves_a_statusline_set_in_its_own_settings_alone(stub, home):
    settings = own_claude(home)
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo global"}}))
    _, gsettings, _ = gc_paths(home)
    gsettings.parent.mkdir(parents=True)
    gsettings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo mine"}}))
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert json.loads(gsettings.read_text())["statusLine"] == {"type": "command", "command": "echo mine"}


def test_gclaude_and_global_mode_keep_separate_records(stub, home):
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert cg(home, "on", "--gclaude").returncode == 0
    assert cg(home, "off", "--gclaude").returncode == 0
    settings = home / ".claude" / "settings.json"
    s = json.loads(settings.read_text())
    assert s["statusLine"]["command"].endswith("statusline.sh") and s["hooks"]["UserPromptSubmit"]
    assert cg(home, "off").returncode == 0
    s = json.loads(settings.read_text())
    assert "statusLine" not in s and "hooks" not in s and "disableClaudeAiConnectors" not in s and "env" not in s


def test_off_gclaude_says_nothing_was_installed(stub, home):
    r = cg(home, "off", "--gclaude")
    assert r.returncode == 0 and "nothing to undo" in r.stdout
    assert not gc_paths(home)[0].exists()


def test_status_reports_gclaude(stub, home):
    assert "gclaude: not set up" in cg(home, "status").stdout
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    out = cg(home, "status").stdout
    assert f"gclaude: installed ({gc_paths(home)[2]})" in out
    assert out.startswith("off: Claude Code uses this machine's own login")


def test_gclaude_status_shows_the_gateway_the_account_and_the_limits(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    launcher = gc_paths(home)[2]
    r = run_gclaude(home, launcher, "status")
    assert r.returncode == 0, r.stderr
    assert "claude started" not in r.stdout
    assert f"gateway: {stub.url} (reachable)" in r.stdout
    assert "account: alice · user · key sk-proxy-ab1… (your first key)" in r.stdout
    assert "usage: alice · daily 10/100 req" in r.stdout
    stub.revoked.add("sk-proxy-full")
    r = run_gclaude(home, launcher, "status")
    assert "account: this computer's key no longer works; run gclaude to sign in again" in r.stdout, r.stdout


def test_gclaude_uninstall_removes_gclaude(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, gsettings, launcher = gc_paths(home)
    r = run_gclaude(home, launcher, "uninstall")
    assert r.returncode == 0 and "claude started" not in r.stdout, r.stderr
    assert not launcher.exists() and "sk-proxy-full" not in gsettings.read_text()


def test_gclaude_keeps_its_key_private(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, gsettings, _ = gc_paths(home)
    assert gdir.stat().st_mode & 0o777 == 0o700
    assert gsettings.stat().st_mode & 0o777 == 0o600


def test_gclaude_rerun_tightens_a_readable_settings_file(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gsettings = gc_paths(home)[1]
    gsettings.chmod(0o644)
    assert cg(home, "on", "--gclaude").returncode == 0
    assert gsettings.stat().st_mode & 0o777 == 0o600


# ---------- claude-gateway --gclaude: /usage and per-command links ----------

def run_gclaude(home, launcher, *args):
    fake = home / "fakebin"
    fake.mkdir(exist_ok=True)
    (fake / "claude").write_text('#!/bin/sh\necho "claude started $*"\n')
    (fake / "claude-gateway").write_text(f'#!/bin/sh\nexec bash {GATEWAY} "$@"\n')   # this checkout's, not the machine's
    for f in ("claude", "claude-gateway"):
        (fake / f).chmod(0o755)
    return run([str(launcher), *args], {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home),
                                        "TMPDIR": str(home / "tmp"), "CLAUDE_GATEWAY_OPEN": ""})   # no browser


def test_gclaude_gets_a_usage_command_that_plain_claude_never_sees(stub, home):
    own_claude(home)
    own_cmds = home / ".claude" / "commands"
    own_cmds.mkdir()
    (own_cmds / "mine.md").write_text("mine\n")
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, _, launcher = gc_paths(home)
    cmds = gdir / "commands"
    assert cmds.is_dir() and not cmds.is_symlink()
    usage = cmds / "usage.md"
    assert "disable-model-invocation: true" in usage.read_text()
    account = (cmds / "account.md").read_text()
    line = f"sh {home}/.config/claude-gateway/statusline.sh --account"
    assert f"!`{line}`" in account and f"allowed-tools: Bash({line})" in account and "model: haiku" in account
    assert sorted(p.name for p in own_cmds.iterdir()) == ["mine.md"]
    assert run_gclaude(home, launcher).returncode == 0
    assert os.readlink(cmds / "mine.md") == str(own_cmds / "mine.md")


def test_gclaude_on_replaces_the_older_logout_command_and_off_removes_it(stub, home):
    """An older gclaude named /logout_gclaude /logout, beside the built-in /logout it can't hide."""
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    cmds = gc_paths(home)[0] / "commands"
    (cmds / "logout.md").write_text("<!-- # Installed by claude-gateway on --gclaude. -->\n")
    assert cg(home, "on", "--gclaude").returncode == 0
    assert sorted(p.name for p in cmds.iterdir()) == ["account.md", "logout_gclaude.md", "usage.md"]
    (cmds / "logout.md").write_text("<!-- # Installed by claude-gateway on --gclaude. -->\n")
    assert cg(home, "off", "--gclaude").returncode == 0
    assert not cmds.exists()


def test_gclaude_on_leaves_the_users_own_logout_command_alone(stub, home):
    cmds = gc_paths(home)[0] / "commands"
    cmds.mkdir(parents=True)
    (cmds / "logout.md").write_text("mine\n")
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert (cmds / "logout.md").read_text() == "mine\n"


def logged_out(stub, home):
    """gclaude set up, then signed out with its /logout: the key is gone from settings.json and client.json."""
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, gsettings, launcher = gc_paths(home)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(home / "tmp"), "CLAUDE_CONFIG_DIR": str(gdir),
           **json.loads(gsettings.read_text())["env"]}
    hook = json.loads(gsettings.read_text())["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    out = json.loads(run(["sh", "-c", hook], env, stdin=json.dumps({"prompt": "/logout_gclaude"})).stdout)
    assert out["continue"] is False
    assert "sk-proxy-full" not in gsettings.read_text()
    assert "sk-proxy-full" not in (home / ".config" / "claude-gateway" / "client.json").read_text()
    return gsettings, launcher


def test_gclaude_signs_in_again_in_the_browser_when_started_signed_out(stub, home):
    """Like plain claude's login: no separate command; the next gclaude authorizes this computer, then starts."""
    gsettings, launcher = logged_out(stub, home)
    stub.tokens = [(400, {"error": "authorization_pending"}), (200, {"key": "sk-proxy-new", "user": "ana"})]
    r = run_gclaude(home, launcher, "-p", "hi")
    assert r.returncode == 0, r.stderr
    assert f"{stub.url}/dashboard#authorize/ABCD-EFGH" in r.stderr and "Authorized as ana" in r.stderr
    assert "Next:" not in r.stdout   # it is starting gclaude already
    assert r.stdout.strip().endswith("claude started -p hi")
    assert json.loads(gsettings.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-new"
    r = run_gclaude(home, launcher)   # signed in now: no authorization
    assert r.returncode == 0 and "authorize" not in r.stderr, r.stderr


def test_the_installers_on_leaves_a_signed_out_gclaude_to_sign_in_when_it_next_starts(stub, home):
    """gclaude update runs the dashboard's installer, whose `on` must not stop for the browser: the next gclaude signs in."""
    gsettings, launcher = logged_out(stub, home)
    launcher.write_text(launcher.read_text().replace("Checking for a Claude Code update", "an older gclaude"))
    r = cg(home, "on", "--url", stub.url, "--dashboard", stub.url)   # what the dashboard's /install runs
    assert r.returncode == 0 and r.stdout == "", r.stdout + r.stderr
    assert "Checking for a Claude Code update" in launcher.read_text()   # the launcher is refreshed all the same
    assert "ANTHROPIC_AUTH_TOKEN" not in gsettings.read_text()             # still signed out
    assert not any(p == "/api/device/start" for p, _ in stub.requests)
    stub.tokens = [(200, {"key": "sk-proxy-new", "user": "ana"})]
    r = run_gclaude(home, launcher, "-p", "hi")
    assert r.returncode == 0 and "Authorized as ana" in r.stderr, r.stderr


def test_gclaude_signs_in_again_when_its_key_was_removed_in_the_dashboard(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, gsettings, launcher = gc_paths(home)
    stub.revoked.add("sk-proxy-full")
    stub.tokens = [(200, {"key": "sk-proxy-new", "user": "ana"})]
    r = run_gclaude(home, launcher, "-p", "hi")
    assert r.returncode == 0, r.stderr
    assert "key no longer works" in r.stderr and "Authorized as ana" in r.stderr
    assert r.stdout.strip().endswith("claude started -p hi")
    assert json.loads(gsettings.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-new"
    status = [h for p, h in stub.requests if p.startswith("/api/me/status")]
    assert status and all("sk-proxy" not in p for p, _ in stub.requests)   # the key goes in a header, never the URL


def test_gclaude_starts_when_the_dashboard_cannot_be_reached(stub, home):
    """Only a clear 401 means signed out: offline, gclaude starts as usual."""
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    launcher = gc_paths(home)[2]
    stub.server.shutdown()
    stub.server.server_close()
    r = run_gclaude(home, launcher, "-p", "hi")
    assert r.returncode == 0 and r.stdout.strip() == "claude started -p hi", r.stderr
    assert r.stderr == ""


def test_gclaude_does_not_start_when_the_sign_in_is_cancelled(stub, home):
    gsettings, launcher = logged_out(stub, home)
    stub.tokens = [(400, {"error": "access_denied"})]
    r = run_gclaude(home, launcher)
    assert r.returncode == 1 and "Cancelled in the browser" in r.stderr, r.stderr
    assert "claude started" not in r.stdout
    assert "ANTHROPIC_AUTH_TOKEN" not in gsettings.read_text()
    assert cg(home, "on", "--gclaude", "--key", "sk-proxy-new").returncode == 0   # the old way still works
    assert run_gclaude(home, launcher).returncode == 0


def test_gclaude_update_runs_the_dashboards_installer_then_claude_update(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, _, launcher = gc_paths(home)
    fake = home / "fakebin"
    fake.mkdir()
    (fake / "claude").write_text('#!/bin/sh\necho "claude [${CLAUDE_CONFIG_DIR:-own config}] $*"\n')
    (fake / "claude").chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home)}
    version = home / "version"   # what the claude-gateway beside gclaude says its version is; the installer moves it on
    (launcher.parent / "claude-gateway").write_text(f'#!/bin/sh\n[ "$1" = version ] && cat {version}\n')
    (launcher.parent / "claude-gateway").chmod(0o755)
    version.write_text("1.0.7\n")
    stub.install = f"echo installer ran; echo 1.0.8 > {version}\n"
    r = run([str(launcher), "update"], env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["installer ran", "Checking for a Claude Code update...", "claude [own config] update",
                                   "gclaude updated from 1.0.7 to 1.0.8"]   # plain claude's own update, shown as it did something
    (fake / "claude").write_text('#!/bin/sh\necho "Claude Code is up to date (2.1.286)"\n')
    r = run([str(launcher), "update"], env)
    assert r.stdout.splitlines() == ["installer ran", "Checking for a Claude Code update...", "gclaude is up to date (1.0.8)"]
    stub.install = f"echo installer ran; echo 1.0.9 > {version}\n"
    version.unlink()   # an older claude-gateway, which has no version
    r = run([str(launcher), "update"], env)
    assert r.stdout.splitlines()[-1] == "gclaude updated to 1.0.9"
    stub.install = "echo installer ran\n"
    version.unlink()   # nor the new one: no version to show
    r = run([str(launcher), "update"], env)
    assert r.stdout.splitlines()[-1] == "gclaude is up to date"
    (fake / "claude").write_text('#!/bin/sh\necho "no network" >&2; exit 4\n')
    r = run([str(launcher), "update"], env)
    assert r.returncode == 4 and "no network" in r.stderr and "gclaude is up to date" not in r.stdout
    (fake / "claude").write_text('#!/bin/sh\necho "claude [${CLAUDE_CONFIG_DIR:-own config}] $*"\n')
    stub.install = "exit 3\n"
    r = run([str(launcher), "update"], env)
    assert r.returncode == 1 and "Claude Code was not updated" in r.stderr and "claude [" not in r.stdout
    stub.install = None                                                           # the dashboard can't be reached
    r = run([str(launcher), "update"], env)
    assert r.returncode == 1 and "claude [" not in r.stdout


def test_gclaude_tells_a_claude_that_cannot_run_from_a_missing_one(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, _, launcher = gc_paths(home)
    fake = home / "fakebin"
    fake.mkdir()
    (fake / "claude").write_text("not a program\n")   # on PATH, but not executable
    r = run([str(launcher)], {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(home)})
    assert r.returncode == 126 and f"Claude Code at {fake / 'claude'} can't be run" in r.stderr
    (fake / "claude").unlink()
    r = run([str(launcher)], {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(home)})
    assert r.returncode == 127 and "not installed or not on PATH" in r.stderr


def test_gclaude_update_works_after_logout(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, gsettings, launcher = gc_paths(home)
    gsettings.write_text("{}")   # as /logout_gclaude leaves it
    stub.install = "echo installer ran\n"
    r = run_gclaude(home, launcher)
    assert r.returncode == 1
    r = run([str(launcher), "update"], {"PATH": f"{home / 'fakebin'}:{os.environ['PATH']}", "HOME": str(home)})
    assert r.returncode == 0 and "installer ran" in r.stdout


def test_gclaude_launcher_follows_later_command_changes(stub, home):
    own_claude(home)
    own_cmds = home / ".claude" / "commands"
    own_cmds.mkdir()
    (own_cmds / "old.md").write_text("old\n")
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, _, launcher = gc_paths(home)
    cmds = gdir / "commands"
    (cmds / "gateway-only.md").write_text("mine here\n")
    assert run_gclaude(home, launcher).returncode == 0
    (own_cmds / "old.md").unlink()
    (own_cmds / "new.md").write_text("new\n")
    (own_cmds / "usage.md").write_text("my own usage\n")          # gclaude's /usage wins in gclaude
    assert run_gclaude(home, launcher).returncode == 0
    assert sorted(p.name for p in cmds.iterdir()) == ["account.md", "gateway-only.md", "logout_gclaude.md", "new.md", "usage.md"]
    assert "disable-model-invocation" in (cmds / "usage.md").read_text()


def test_gclaude_replaces_its_old_commands_link_with_a_folder(stub, home):
    own_claude(home)
    (home / ".claude" / "commands").mkdir()
    gdir, _, _ = gc_paths(home)
    gdir.mkdir(parents=True)
    (gdir / "commands").symlink_to(home / ".claude" / "commands")
    client = home / ".config" / "claude-gateway" / "client.json"
    client.write_text(json.dumps({"gclaude": {"links": ["commands"]}}))
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert (gdir / "commands").is_dir() and not (gdir / "commands").is_symlink()
    assert not (home / ".claude" / "commands" / "usage.md").exists()


def test_gclaude_off_removes_its_usage_command_and_command_links(stub, home):
    own_claude(home)
    own_cmds = home / ".claude" / "commands"
    own_cmds.mkdir()
    (own_cmds / "mine.md").write_text("mine\n")
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, _, launcher = gc_paths(home)
    assert run_gclaude(home, launcher).returncode == 0
    assert cg(home, "off", "--gclaude").returncode == 0
    assert not (gdir / "commands").exists()
    assert (own_cmds / "mine.md").read_text() == "mine\n"



# ---------- gclaude review fixes ----------

def test_gclaude_off_removes_links_left_by_a_failed_on(stub, home):
    own_claude(home)
    gdir, _, _ = gc_paths(home)
    gdir.mkdir(parents=True)
    (gdir / "commands").write_text("a file where the folder goes")      # makes `on` fail after linking
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode != 0
    assert (gdir / "agents").is_symlink()
    (gdir / "commands").unlink()
    assert cg(home, "on", "--gclaude").returncode == 0
    assert cg(home, "off", "--gclaude").returncode == 0
    assert not (gdir / "agents").is_symlink() and not (gdir / "CLAUDE.md").is_symlink()


def test_gclaude_refuses_a_dangling_symlink_where_the_command_goes(stub, home, tmp_path):
    _, _, launcher = gc_paths(home)
    launcher.parent.mkdir(parents=True)
    target = tmp_path / "elsewhere" / "mytool"
    launcher.symlink_to(target)
    r = cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 1 and str(launcher) in r.stderr
    assert launcher.is_symlink() and not target.exists()


def test_gclaude_launcher_leaves_a_linked_commands_folder_alone(stub, home, tmp_path):
    own_claude(home)
    own_cmds = home / ".claude" / "commands"
    own_cmds.mkdir()
    (own_cmds / "mine.md").write_text("mine\n")
    team = tmp_path / "team-commands"
    team.mkdir()
    gdir, _, launcher = gc_paths(home)
    gdir.mkdir(parents=True)
    (gdir / "commands").symlink_to(team)
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert run_gclaude(home, launcher).returncode == 0
    assert list(team.iterdir()) == []


def test_gclaude_off_drops_the_settings_backup_that_holds_the_key(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, _, _ = gc_paths(home)
    assert cg(home, "off", "--gclaude").returncode == 0
    assert not (gdir / "settings.json.bak-claude-gateway").exists()
    assert "sk-proxy-full" not in "".join(p.read_text(errors="ignore") for p in gdir.rglob("*") if p.is_file())


def test_gclaude_launcher_never_puts_its_folder_path_in_a_comment(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, _, launcher = gc_paths(home)
    assert not [l for l in launcher.read_text().splitlines() if l.startswith("#") and str(gdir) in l]


def test_off_gclaude_says_when_the_command_is_not_ours(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, _, launcher = gc_paths(home)
    launcher.write_text("#!/bin/sh\necho mine\n")
    r = cg(home, "off", "--gclaude")
    assert r.returncode == 0 and "not installed by claude-gateway" in r.stdout
    assert launcher.read_text() == "#!/bin/sh\necho mine\n"

# ---------- review leftovers: no key in curl's argv, off leaves no debris, setup warnings ----------

def logging_curl(home):
    """A curl first on PATH that records its arguments, then runs the real one."""
    import shutil
    fake = home / "argvbin"
    fake.mkdir(exist_ok=True)
    log = home / "curl-argv.log"
    (fake / "curl").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\nexec {shutil.which("curl")} "$@"\n')
    (fake / "curl").chmod(0o755)
    return fake, log


def test_keys_never_appear_in_curls_arguments(stub, home, tmp_path):
    stub.models = MODELS
    fake, log = logging_curl(home)
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "TMPDIR": str(tmp)}
    for args in (["on", "--global", "--url", stub.url, "--key", "sk-proxy-fullsecret"],
                 ["on", "--gclaude"],
                 ["status"],
                 ["on", "--opencode", "--routes-key", "sk-proxy-r-routesecret"]):
        r = run(["bash", str(GATEWAY), *args], env)
        assert r.returncode == 0, (args, r.stderr)
    r = run(["sh", str(STATUSLINE)], {**env, "CLAUDE_GATEWAY_DASHBOARD": stub.url, "ANTHROPIC_AUTH_TOKEN": "sk-proxy-fullsecret"})
    assert r.returncode == 0
    argv = log.read_text()
    assert argv.count("\n") >= 6 and "secret" not in argv
    sent = [h.get("authorization", "") + h.get("x-api-key", "") for _, h in stub.requests]
    assert "Bearer sk-proxy-fullsecret" in sent and "sk-proxy-r-routesecret" in sent


def logging_python3(where):
    """A python3 first on PATH that records each of its arguments (one per line), then runs the real one."""
    import shutil
    fake = Path(where) / "py-argvbin"
    fake.mkdir(exist_ok=True)
    log = Path(where) / "python3-argv.log"
    (fake / "python3").write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a" >> {log}; done\n'
                                  f'exec {shutil.which("python3")} "$@"\n')
    (fake / "python3").chmod(0o755)
    return fake, log


def test_keys_never_appear_in_python3s_arguments(stub, home):
    stub.models = MODELS
    fake, log = logging_python3(home)
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "TMPDIR": str(tmp)}
    for args in (["on", "--global", "--url", stub.url, "--key", "sk-proxy-fullsecret"],
                 ["on", "--gclaude"],
                 ["on", "--gclaude", "--key", "sk-proxy-newsecret", "--dashboard", stub.url],
                 ["on", "--opencode", "--routes-key", "sk-proxy-r-routesecret"]):
        r = run(["bash", str(GATEWAY), *args], env)
        assert r.returncode == 0, (args, r.stderr)
    argv = log.read_text().splitlines()
    assert len(argv) >= 6 and not [a for a in argv if "secret" in a]
    client = json.loads((home / ".config" / "claude-gateway" / "client.json").read_text())   # and the key still lands
    assert client["key"] == "sk-proxy-newsecret" and client["dashboard"] == stub.url
    assert json.loads(gc_paths(home)[1].read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-newsecret"


def test_logout_keeps_the_key_out_of_python3s_arguments(stub, warn_env):
    fake, log = logging_python3(warn_env["HOME"])
    env = {**warn_env, "PATH": f"{fake}:{warn_env['PATH']}"}
    out = json.loads(logout_prompt(env).stdout)
    assert out["continue"] is False and "revoked on the gateway" in out["stopReason"]
    argv = log.read_text().splitlines()
    assert argv and not [a for a in argv if "sk-proxy-k" in a]
    assert signed_out(env)


def test_opencode_off_removes_the_agents_folder_it_created_and_an_empty_client_json(stub, home):
    stub.models = MODELS
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    assert cg(home, "off", "--opencode").returncode == 0
    conf, key_file, agent = oc_paths(home)
    assert not agent.parent.exists()
    assert not (home / ".config" / "claude-gateway" / "client.json").exists()


def test_opencode_off_keeps_an_agents_folder_that_was_already_there(stub, home):
    stub.models = MODELS
    conf, _, agent = oc_paths(home)
    agent.parent.mkdir(parents=True)
    assert cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k").returncode == 0
    assert cg(home, "off", "--opencode").returncode == 0
    assert agent.parent.is_dir() and not agent.exists()


def test_opencode_off_keeps_client_json_that_still_has_claude_code_settings(stub, home):
    stub.models = MODELS
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert cg(home, "on", "--opencode", "--routes-key", "sk-proxy-r-k").returncode == 0
    assert cg(home, "off", "--opencode").returncode == 0
    assert json.loads((home / ".config" / "claude-gateway" / "client.json").read_text())["key"] == "sk-proxy-full"


def test_opencode_on_warns_when_the_gateway_has_no_models_for_the_key(stub, home):
    r = cg(home, "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k")
    assert r.returncode == 0
    assert "no third-party models" in r.stderr


def test_opencode_on_warns_when_the_muse_agent_is_missing(stub, home, tmp_path):
    import shutil
    stub.models = MODELS
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy(GATEWAY, alone / "claude-gateway")
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    r = run(["bash", str(alone / "claude-gateway"), "on", "--opencode", "--url", stub.url, "--routes-key", "sk-proxy-r-k"],
            {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(tmp)})
    assert r.returncode == 0, r.stderr
    assert "muse agent" in r.stderr and not oc_paths(home)[2].exists()


# ---------- gclaude shares plugins, memory and sessions with plain claude (gclaude-sync.py) ----------

def own_plugins_and_memory(home):
    own = home / ".claude"
    own.mkdir(exist_ok=True)
    (own / "plugins" / "cache").mkdir(parents=True)
    (own / "plugins" / "installed_plugins.json").write_text('{"version": 2, "plugins": {}}')
    (own / "settings.json").write_text(json.dumps({
        "enabledPlugins": {"superpowers@m": True, "swift@m": False},
        "extraKnownMarketplaces": {"extra": {"source": {"source": "github", "repo": "a/b"}}},
        "model": "opus"}))
    for name in ("-p1", "-p3"):
        (own / "projects" / name / "memory").mkdir(parents=True)
        (own / "projects" / name / "memory" / "MEMORY.md").write_text(f"{name} memories\n")
    (own / "projects" / "-nomem").mkdir(parents=True)
    return own


def test_gclaude_shares_plugins_and_their_enabled_list(stub, home):
    own = own_plugins_and_memory(home)
    before = (own / "settings.json").read_text()
    gdir, gsettings, launcher = gc_paths(home)
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert os.readlink(gdir / "plugins") == str(own / "plugins")
    s = json.loads(gsettings.read_text())
    assert s["enabledPlugins"] == {"superpowers@m": True, "swift@m": False}
    assert s["extraKnownMarketplaces"] == {"extra": {"source": {"source": "github", "repo": "a/b"}}}
    assert "model" not in s and s["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-full"
    assert gsettings.stat().st_mode & 0o777 == 0o600
    s["enabledPlugins"]["gclaude-only@m"] = True                         # enabled only in gclaude: kept
    gsettings.write_text(json.dumps(s))
    own_s = json.loads(before)
    own_s["enabledPlugins"]["swift@m"] = True                            # changed in plain claude: followed
    (own / "settings.json").write_text(json.dumps(own_s))
    assert run_gclaude(home, launcher).returncode == 0
    assert json.loads(gsettings.read_text())["enabledPlugins"] == \
        {"superpowers@m": True, "swift@m": True, "gclaude-only@m": True}
    assert cg(home, "off", "--gclaude").returncode == 0
    assert not (gdir / "plugins").exists()
    assert json.loads(gsettings.read_text()) == {"enabledPlugins": {"gclaude-only@m": True}}
    assert (own / "plugins" / "installed_plugins.json").exists()


def test_gclaude_replaces_the_untouched_plugins_folder_claude_code_made_but_keeps_real_installs(stub, home):
    own_plugins_and_memory(home)
    gdir, _, _ = gc_paths(home)
    (gdir / "plugins" / "marketplaces" / "claude-plugins-official").mkdir(parents=True)   # as Claude Code makes it
    (gdir / "plugins" / "known_marketplaces.json").write_text('{"claude-plugins-official": {}}')
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert (gdir / "plugins").is_symlink()
    assert cg(home, "off", "--gclaude").returncode == 0
    (gdir / "plugins").mkdir()
    (gdir / "plugins" / "installed_plugins.json").write_text('{"plugins": {"mine@m": []}}')
    r = cg(home, "on", "--gclaude")
    assert r.returncode == 0 and "not shared" in r.stderr
    assert not (gdir / "plugins").is_symlink() and (gdir / "plugins" / "installed_plugins.json").exists()


def test_gclaude_shares_memory_per_project_and_keeps_its_own_history(stub, home):
    own = own_plugins_and_memory(home)
    gdir, _, launcher = gc_paths(home)
    (gdir / "projects" / "-p1").mkdir(parents=True)
    (gdir / "projects" / "-p1" / "session.jsonl").write_text("{}\n")
    (gdir / "projects" / "-p3" / "memory").mkdir(parents=True)         # empty, as Claude Code makes it
    (gdir / "projects" / "-p2" / "memory").mkdir(parents=True)
    (gdir / "projects" / "-p2" / "memory" / "MEMORY.md").write_text("gclaude's own\n")
    r = cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 0
    for name in ("-p1", "-p3"):
        assert os.readlink(gdir / "projects" / name / "memory") == str(own / "projects" / name / "memory")
    assert (gdir / "projects" / "-p1" / "session.jsonl").exists()        # history stays gclaude's
    assert not (gdir / "projects" / "-p2" / "memory").is_symlink()
    assert not (gdir / "projects" / "-nomem").exists()                   # nothing to share there
    assert not (own / "projects" / "-nomem" / "memory").exists()
    assert cg(home, "off", "--gclaude").returncode == 0
    assert not (gdir / "projects" / "-p1" / "memory").exists()
    assert (gdir / "projects" / "-p2" / "memory" / "MEMORY.md").read_text() == "gclaude's own\n"
    assert (own / "projects" / "-p1" / "memory" / "MEMORY.md").read_text() == "-p1 memories\n"


def test_gclaude_links_the_current_projects_memory_once_plain_claude_has_been_used_there(stub, home, tmp_path):
    import re, subprocess as sp
    own = own_plugins_and_memory(home)
    repo = tmp_path / "my_repo"
    (repo / "sub").mkdir(parents=True)
    sp.run(["git", "init", "-q", str(repo)], check=True)
    name = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(repo))
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, _, launcher = gc_paths(home)
    fake = home / "fakebin"
    fake.mkdir(exist_ok=True)
    (fake / "claude").write_text("#!/bin/sh\nexit 0\n")
    (fake / "claude").chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home)}
    assert sp.run([str(launcher)], cwd=repo / "sub", env=env).returncode == 0
    assert not (gdir / "projects" / name).exists()                       # plain claude never used there
    (own / "projects" / name).mkdir()
    assert sp.run([str(launcher)], cwd=repo / "sub", env=env).returncode == 0
    assert os.readlink(gdir / "projects" / name / "memory") == str(own / "projects" / name / "memory")
    assert (own / "projects" / name / "memory").is_dir()


def test_gclaude_starts_even_when_sharing_fails(stub, home):
    own = own_plugins_and_memory(home)
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    (own / "settings.json").write_text("{not json")
    (own / "projects").chmod(0o000)
    try:
        r = run_gclaude(home, gc_paths(home)[2])
    finally:
        (own / "projects").chmod(0o755)
    assert r.returncode == 0


# ---------- gclaude-sync review fixes ----------

SYNC = ROOT / "scripts" / "gclaude-sync.py"


def sync(home, mode="sync", cwd=None):
    import subprocess as sp
    gdir = home / ".config" / "claude-gateway" / "claude"
    return sp.run(["python3", str(SYNC), mode, str(gdir), str(home / ".claude")], capture_output=True, text=True,
                  cwd=cwd or home, env={"PATH": os.environ["PATH"], "HOME": str(home)})


def test_sync_never_touches_own_memory_through_a_linked_projects_folder(home, tmp_path):
    import re, subprocess as sp
    own = own_plugins_and_memory(home)
    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    name = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(repo))
    (own / "projects" / name).mkdir()
    gdir = gc_paths(home)[0]
    gdir.mkdir(parents=True)
    (gdir / "projects").symlink_to(own / "projects")               # someone shares history too
    r = sync(home, cwd=repo)
    assert r.returncode == 0
    for p in ("-p1", name):
        mem = own / "projects" / p / "memory"
        assert mem.is_dir() and not mem.is_symlink()
    assert (own / "projects" / "-p1" / "memory" / "MEMORY.md").read_text() == "-p1 memories\n"


def settings_of(home):
    return json.loads(gc_paths(home)[1].read_text())


def test_sync_keeps_what_gclaude_changed_and_follows_what_plain_claude_changed(home):
    own = own_plugins_and_memory(home)
    gdir, gsettings, _ = gc_paths(home)
    gdir.mkdir(parents=True)
    gsettings.write_text("{}")
    assert sync(home).returncode == 0
    s = settings_of(home)
    s["enabledPlugins"]["swift@m"] = True                           # plain claude has it off; enabled in gclaude
    s["enabledPlugins"]["superpowers@m"] = False                    # and this one turned off in gclaude
    gsettings.write_text(json.dumps(s))
    own_s = json.loads((own / "settings.json").read_text())
    own_s["enabledPlugins"]["new@m"] = True                         # plain claude adds one
    del own_s["extraKnownMarketplaces"]                             # and drops its marketplace
    (own / "settings.json").write_text(json.dumps(own_s))
    assert sync(home).returncode == 0
    s = settings_of(home)
    assert s["enabledPlugins"] == {"superpowers@m": False, "swift@m": True, "new@m": True}
    assert "extraKnownMarketplaces" not in s
    own_s["enabledPlugins"]["swift@m"] = True                       # plain claude catches up: no conflict
    own_s["enabledPlugins"]["superpowers@m"] = True                 # plain claude changes it again: followed
    (own / "settings.json").write_text(json.dumps(own_s))
    own_s["enabledPlugins"]["superpowers@m"] = False
    (own / "settings.json").write_text(json.dumps(own_s))
    assert sync(home).returncode == 0
    assert settings_of(home)["enabledPlugins"]["superpowers@m"] is False


def test_unsync_removes_exactly_what_sync_added(home):
    own = own_plugins_and_memory(home)
    gdir, gsettings, _ = gc_paths(home)
    gdir.mkdir(parents=True)
    gsettings.write_text(json.dumps({"enabledPlugins": {"mine@m": True}}))
    assert sync(home).returncode == 0
    own_s = json.loads((own / "settings.json").read_text())
    own_s["enabledPlugins"]["swift@m"] = True                       # changed in plain claude after the sync
    (own / "settings.json").write_text(json.dumps(own_s))
    s = settings_of(home)
    s["enabledPlugins"]["superpowers@m"] = False                    # changed in gclaude: gclaude's own now
    gsettings.write_text(json.dumps(s))
    assert sync(home, "unsync").returncode == 0
    assert settings_of(home) == {"enabledPlugins": {"mine@m": True, "superpowers@m": False}}
    assert not [p for p in gdir.iterdir() if p.name.startswith(".gclaude-sync")]
    assert not (gdir / "projects" / "-p1").exists()                 # empty project folders go too


def test_sync_leaves_a_plugins_folder_with_a_marketplace_added_in_gclaude(home):
    own_plugins_and_memory(home)
    gdir = gc_paths(home)[0]
    (gdir / "plugins" / "marketplaces" / "mine").mkdir(parents=True)
    (gdir / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"claude-plugins-official": {}, "mine": {}}))
    r = sync(home)
    assert not (gdir / "plugins").is_symlink() and (gdir / "plugins" / "marketplaces" / "mine").is_dir()
    assert "not shared" in r.stderr


def test_sync_drops_memory_links_whose_target_is_gone_and_keeps_going_after_an_error(home):
    import shutil
    own = own_plugins_and_memory(home)
    gdir = gc_paths(home)[0]
    gdir.mkdir(parents=True)
    assert sync(home).returncode == 0
    shutil.rmtree(own / "projects" / "-p1")
    (gdir / "projects" / "-p3" / "memory").unlink()
    (gdir / "projects" / "-p3" / "memory").write_text("a file in the way")
    (own / "projects" / "-p4" / "memory").mkdir(parents=True)
    r = sync(home)
    assert r.returncode == 0
    assert not os.path.lexists(gdir / "projects" / "-p1" / "memory")
    assert (gdir / "projects" / "-p4" / "memory").is_symlink()     # after -p3's problem


def test_sync_makes_private_project_folders(home):
    own_plugins_and_memory(home)
    gdir = gc_paths(home)[0]
    gdir.mkdir(parents=True)
    assert sync(home).returncode == 0
    assert (gdir / "projects" / "-p1").stat().st_mode & 0o777 == 0o700


def test_off_gclaude_finishes_even_when_unsync_fails(stub, home):
    own_plugins_and_memory(home)
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, gsettings, launcher = gc_paths(home)
    (gdir / "projects").chmod(0o500)                               # links can't be removed
    try:
        r = cg(home, "off", "--gclaude")
    finally:
        (gdir / "projects").chmod(0o700)
    assert r.returncode == 0
    assert not launcher.exists() and "ANTHROPIC_AUTH_TOKEN" not in gsettings.read_text()


S1, S2, S3 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
              "33333333-3333-4333-8333-333333333333")


def own_sessions(own):
    folder = own / "projects" / "-p1"
    (folder / f"{S1}.jsonl").write_text('{"n": 1}\n')
    (folder / S1 / "subagents").mkdir(parents=True)
    (folder / f"{S3}.jsonl").write_text('{"plain": 3}\n')
    (folder / "notes.txt").write_text("not a session\n")
    (own / "projects" / "-sessions-only").mkdir()
    (own / "projects" / "-sessions-only" / f"{S2}.jsonl").write_text('{"n": 2}\n')
    for sid in (S1, S2):   # rewind checkpoints
        (own / "file-history" / sid).mkdir(parents=True)
        (own / "file-history" / sid / "abc@v1").write_text("before\n")
    (own / "file-history" / "not-a-session").mkdir()
    return folder


def test_gclaude_lists_plain_claudes_sessions_and_resuming_one_goes_on_in_plain_claude(stub, home):
    own = own_plugins_and_memory(home)
    folder = own_sessions(own)
    gdir, _, launcher = gc_paths(home)
    mine = gdir / "projects" / "-p1"
    mine.mkdir(parents=True)
    (mine / f"{S2}.jsonl").write_text('{"gclaude": 2}\n')                 # a session started in gclaude
    (mine / f"{S3}.jsonl").write_text('{"gclaude": 3}\n')                 # same name, gclaude's own: left alone
    (gdir / "file-history" / S2).mkdir(parents=True)                      # gclaude's own checkpoints for S2
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    linked = mine / f"{S1}.jsonl"
    assert not linked.is_symlink() and os.path.samefile(linked, folder / f"{S1}.jsonl")   # the picker skips symlinks
    assert os.readlink(mine / S1) == str(folder / S1)
    assert not (mine / "notes.txt").exists()
    assert (mine / f"{S3}.jsonl").read_text() == '{"gclaude": 3}\n'
    assert os.path.samefile(gdir / "projects" / "-sessions-only" / f"{S2}.jsonl",
                            own / "projects" / "-sessions-only" / f"{S2}.jsonl")
    assert (gdir / "projects" / "-sessions-only").stat().st_mode & 0o777 == 0o700
    assert os.readlink(gdir / "file-history" / S1) == str(own / "file-history" / S1)   # so /rewind restores code
    assert not (gdir / "file-history" / S2).is_symlink() and not (gdir / "file-history" / "not-a-session").exists()
    with open(linked, "a") as f:                                           # gclaude resumes it: appends in place
        f.write('{"n": "from gclaude"}\n')
    assert "from gclaude" in (folder / f"{S1}.jsonl").read_text()
    assert run_gclaude(home, launcher).returncode == 0                     # a second start changes nothing
    assert os.path.samefile(linked, folder / f"{S1}.jsonl")
    assert cg(home, "off", "--gclaude").returncode == 0
    assert not os.path.lexists(linked) and not os.path.lexists(mine / S1)
    assert not os.path.lexists(gdir / "file-history" / S1) and (gdir / "file-history" / S2).is_dir()
    assert (own / "file-history" / S1 / "abc@v1").read_text() == "before\n"
    assert not (gdir / "projects" / "-sessions-only").exists()
    assert (mine / f"{S2}.jsonl").read_text() == '{"gclaude": 2}\n'
    assert (mine / f"{S3}.jsonl").read_text() == '{"gclaude": 3}\n'
    assert "from gclaude" in (folder / f"{S1}.jsonl").read_text() and (folder / S1 / "subagents").is_dir()
    assert (own / "projects" / "-sessions-only" / f"{S2}.jsonl").exists()


def test_sync_keeps_a_session_plain_claude_dropped_and_prunes_its_folder_links(home):
    import shutil
    own = own_plugins_and_memory(home)
    folder = own_sessions(own)
    gdir = gc_paths(home)[0]
    gdir.mkdir(parents=True)
    assert sync(home).returncode == 0
    (folder / f"{S1}.jsonl").unlink()                                      # plain claude's cleanup
    shutil.rmtree(folder / S1)
    shutil.rmtree(own / "file-history" / S1)
    assert sync(home).returncode == 0
    assert not os.path.lexists(gdir / "file-history" / S1)
    assert (gdir / "file-history" / S2).is_symlink()
    mine = gdir / "projects" / "-p1"
    assert (mine / f"{S1}.jsonl").read_text() == '{"n": 1}\n'             # gclaude's copy now
    assert not os.path.lexists(mine / S1)
    assert sync(home, "unsync").returncode == 0
    assert (mine / f"{S1}.jsonl").exists()                                 # can't be told from gclaude's own


def test_sync_shares_no_sessions_through_a_linked_projects_folder(home):
    own = own_plugins_and_memory(home)
    own_sessions(own)
    gdir = gc_paths(home)[0]
    gdir.mkdir(parents=True)
    (gdir / "projects").symlink_to(own / "projects")
    assert sync(home).returncode == 0
    assert sorted(p.name for p in (own / "projects" / "-p1").iterdir()) == \
        sorted([f"{S1}.jsonl", S1, f"{S3}.jsonl", "notes.txt", "memory"])
    assert sync(home, "unsync").returncode == 0
    assert (own / "projects" / "-p1" / f"{S1}.jsonl").exists()


# ---------- gclaude is the default; global mode is --global ----------

def test_on_sets_up_gclaude_by_default(stub, home):
    settings = own_claude(home)
    before = settings.read_bytes()
    r = cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 0, r.stderr
    assert gc_paths(home)[2].exists() and settings.read_bytes() == before


def test_bare_on_keeps_refreshing_global_mode_where_it_is_on(stub, home):
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    r = cg(home, "on")
    assert r.returncode == 0 and "global mode" in r.stdout
    assert not gc_paths(home)[2].exists()
    assert json.loads((home / ".claude" / "settings.json").read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-full"


def test_key_only_means_global_mode(stub, home):
    assert cg(home, "on", "--key-only", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert "ANTHROPIC_AUTH_TOKEN" in json.loads((home / ".claude" / "settings.json").read_text())["env"]
    assert not gc_paths(home)[2].exists()


def test_gclaude_and_global_together_are_refused(stub, home):
    r = cg(home, "on", "--gclaude", "--global", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 1 and "Choose one" in r.stderr
    assert not (home / ".config" / "claude-gateway" / "client.json").exists()


def test_bare_off_removes_whichever_is_set_up(stub, home):
    assert cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0      # gclaude
    r = cg(home, "off")
    assert r.returncode == 0 and "gclaude is removed" in r.stdout and not gc_paths(home)[2].exists()
    assert cg(home, "on", "--global").returncode == 0
    r = cg(home, "off")
    assert r.returncode == 0 and "own login again" in r.stdout
    assert "env" not in json.loads((home / ".claude" / "settings.json").read_text())
    r = cg(home, "off")
    assert r.returncode == 0 and "nothing to undo" in r.stdout


def test_bare_off_asks_which_when_both_are_set_up(stub, home):
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert cg(home, "on", "--gclaude").returncode == 0
    r = cg(home, "off")
    assert r.returncode == 1 and "off --global, or off --gclaude" in r.stderr
    assert gc_paths(home)[2].exists()


# ---------- gclaude shares user-scope MCP servers (gclaude-sync.py) ----------

def own_mcp(home, servers):
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"email": "me@x"}, "mcpServers": servers}))


def gclaude_json(home):
    return gc_paths(home)[0] / ".claude.json"


def test_sync_shares_mcp_servers_and_keeps_gclaudes_own(home):
    own_mcp(home, {"a": {"command": "a"}, "b": {"command": "b"}})
    gdir = gc_paths(home)[0]
    gdir.mkdir(parents=True)
    gclaude_json(home).write_text(json.dumps({"numStartups": 3, "mcpServers": {"mine": {"command": "m"}}}))
    assert sync(home).returncode == 0
    g = json.loads(gclaude_json(home).read_text())
    assert g["mcpServers"] == {"a": {"command": "a"}, "b": {"command": "b"}, "mine": {"command": "m"}}
    assert g["numStartups"] == 3 and "oauthAccount" not in g          # nothing else of plain claude's follows
    g["mcpServers"]["b"] = {"command": "b2"}                            # changed in gclaude: gclaude's own now
    gclaude_json(home).write_text(json.dumps(g))
    own_mcp(home, {"a": {"command": "a2"}, "c": {"command": "c"}})      # plain claude changes a, drops b, adds c
    assert sync(home).returncode == 0
    assert json.loads(gclaude_json(home).read_text())["mcpServers"] == \
        {"a": {"command": "a2"}, "b": {"command": "b2"}, "c": {"command": "c"}, "mine": {"command": "m"}}
    assert sync(home, "unsync").returncode == 0
    assert json.loads(gclaude_json(home).read_text()) == \
        {"numStartups": 3, "mcpServers": {"b": {"command": "b2"}, "mine": {"command": "m"}}}


def test_sync_makes_gclaudes_claude_json_for_mcp_servers_before_its_first_start(home):
    own_mcp(home, {"a": {"command": "a"}})
    gc_paths(home)[0].mkdir(parents=True)
    assert sync(home).returncode == 0
    assert json.loads(gclaude_json(home).read_text()) == {"mcpServers": {"a": {"command": "a"}}}
    assert gclaude_json(home).stat().st_mode & 0o777 == 0o600


def test_sync_leaves_an_unreadable_gclaude_claude_json_alone(home):
    own_mcp(home, {"a": {"command": "a"}})
    gc_paths(home)[0].mkdir(parents=True)
    gclaude_json(home).write_text("{not json")
    assert sync(home).returncode == 0
    assert gclaude_json(home).read_text() == "{not json"


def test_the_installers_on_says_nothing_under_gclaude_update(stub, home):
    """gclaude update runs the installer with CLAUDE_GATEWAY_UPDATE set: its own lines say what happened, not on's."""
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    (home / "tmp").mkdir(exist_ok=True)
    r = run(["bash", str(GATEWAY), "on", "--url", stub.url, "--dashboard", stub.url],
            {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(home / "tmp"), "CLAUDE_GATEWAY_UPDATE": "1"})
    assert r.returncode == 0 and r.stdout == "", r.stdout + r.stderr
    assert "gclaude now runs" in cg(home, "on", "--url", stub.url).stdout   # a plain `on` still says it


def test_version_comes_from_the_archives_describe_or_git(tmp_path):
    """GitHub's archive fills scripts/VERSION in with `git describe` (export-subst); MAJOR.MINOR.<commits since the tag>."""
    shutil.copy(GATEWAY, tmp_path / "claude-gateway")
    for described, shown in (("v1.0-8-gabc1234", "1.0.8"), ("v1.2", "1.2.0")):
        (tmp_path / "VERSION").write_text(described + "\n")
        r = run(["bash", str(tmp_path / "claude-gateway"), "version"], {"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
        assert r.returncode == 0 and r.stdout == shown + "\n", r.stdout + r.stderr
    (tmp_path / "VERSION").write_text("$Format:%(describe:tags,match=v[0-9]*)$\n")   # not an archive, nor a checkout
    r = run(["bash", str(tmp_path / "claude-gateway"), "version"], {"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert r.returncode == 1 and r.stdout == "" and "version is unknown" in r.stderr
    assert (GATEWAY.parent / "VERSION").read_text().startswith("$Format:%(describe:tags")   # what GitHub fills in


def test_gclaude_version_shows_gclaudes_then_claude_codes(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, _, launcher = gc_paths(home)
    (launcher.parent / "claude-gateway").write_text('#!/bin/sh\n[ "$1" = version ] && echo 1.0.8\n')
    (launcher.parent / "claude-gateway").chmod(0o755)
    r = run_gclaude(home, launcher, "--version")
    assert r.returncode == 0 and r.stdout.splitlines() == ["1.0.8 (gclaude)", "claude started --version"], r.stdout
    assert not any(p.startswith("/api/me/status") for p, _ in stub.requests)   # answered before any sign-in check


def test_the_statusline_sends_gclaudes_version_with_its_status_call(stub, warn_env):
    """So the dashboard can list which gclaude each computer runs; the key still goes only in the header from stdin."""
    stub.status_line = "maya · daily $10/$100"
    warn({**warn_env, "CLAUDE_GATEWAY_VERSION": "1.0.8"})
    path, headers = [(p, h) for p, h in stub.requests if p.startswith("/api/me/status")][-1]
    assert headers["x-gclaude-version"] == "1.0.8" and headers["authorization"] == "Bearer sk-proxy-k"


def test_on_puts_gclaudes_version_in_its_settings_for_the_statusline(stub, home, tmp_path):
    scripts = tmp_path / "scripts"   # an installed copy, whose VERSION GitHub's archive filled in
    shutil.copytree(GATEWAY.parent, scripts, ignore=shutil.ignore_patterns("windows"))
    (scripts / "VERSION").write_text("v1.0-8-gabc1234\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(home / "tmp")}
    (home / "tmp").mkdir(exist_ok=True)
    r = run(["bash", str(scripts / "claude-gateway"), "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full"], env)
    assert r.returncode == 0, r.stderr
    _, gsettings, _ = gc_paths(home)
    assert json.loads(gsettings.read_text())["env"]["CLAUDE_GATEWAY_VERSION"] == "1.0.8"
    r = run(["bash", str(scripts / "claude-gateway"), "off", "--gclaude"], env)
    assert r.returncode == 0, r.stderr


def test_gclaude_update_refreshes_gclaude_on_a_machine_also_in_global_mode(stub, home):
    """The installer runs a bare `on`, which refreshes global mode there; gclaude update means gclaude."""
    assert cg(home, "on", "--global", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, _, launcher = gc_paths(home)
    launcher.write_text(launcher.read_text().replace("Checking for a Claude Code update", "an older gclaude"))
    r = run(["bash", str(GATEWAY), "on", "--url", stub.url, "--dashboard", stub.url],
            {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(home / "tmp"), "CLAUDE_GATEWAY_UPDATE": "1"})
    assert r.returncode == 0 and r.stdout == "", r.stdout + r.stderr
    assert "Checking for a Claude Code update" in launcher.read_text()
    assert "global mode" in cg(home, "on", "--url", stub.url).stdout   # a plain `on` there still refreshes global mode
