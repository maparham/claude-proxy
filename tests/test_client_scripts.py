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
    assert json.loads(warn(warn_env).stdout) == {"systemMessage": 'Gateway: a"b\\c · daily 90/100 req'}


def test_statusline_mode_still_colours_figures(stub, warn_env):
    stub.status_line = "alice · daily 90/100 req"
    r = run(["sh", str(STATUSLINE)], warn_env)
    assert r.returncode == 0
    assert "\033[33m90/100\033[36m" in r.stdout and r.stdout.startswith("\033[36m◆ alice")


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


@pytest.mark.parametrize("malformed", [None, "nope"])
def test_off_leaves_a_malformed_user_prompt_submit_alone(stub, home, malformed):
    settings = home / ".claude" / "settings.json"
    assert cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
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
    r = cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full", "--routes-key", "sk-proxy-r-k")
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
    assert cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
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
    assert cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    settings = home / ".claude" / "settings.json"
    client = home / ".config" / "claude-gateway" / "client.json"
    before = settings.read_bytes()
    n_requests = len(stub.requests)
    r = cg(home, "on", "--url", stub.url, "--key", "sk-proxy-r-routeskey")
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
    assert cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
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
    assert not (gdir / "commands").exists()                         # nothing to share, nothing linked
    assert json.loads((gdir / ".claude.json").read_text()) == {"hasCompletedOnboarding": True, "theme": "light"}
    assert os.access(launcher, os.X_OK)


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


def test_gclaude_and_global_mode_keep_separate_records(stub, home):
    assert cg(home, "on", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
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
