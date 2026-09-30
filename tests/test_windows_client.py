"""The Windows client (install.ps1, scripts/windows), run under Windows PowerShell 5.1 against a stub gateway and
dashboard (Windows client design section 5). Everything but the static checks runs only on Windows: CI's
client-scripts workflow."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from claude_proxy.config import Config
from claude_proxy.web import install_ps1

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.ps1"
WINDOWS = ROOT / "scripts" / "windows"
CODE = "BCDF-GHJK"
on_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell 5.1 runs only on Windows")


# ---------- static checks, on any OS ----------

def test_the_installer_never_exits_the_callers_powershell():
    """irm | iex runs install.ps1 in the user's own session, where `exit` would close their window."""
    code = "\n".join(l.split("#", 1)[0] for l in INSTALL.read_text().splitlines())
    assert not re.search(r"(?im)(^|[;{}]\s*|\s)exit\b", code)


def pwsh():
    return shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.skipif(not pwsh(), reason="no PowerShell here")
@pytest.mark.parametrize("script", [INSTALL, *sorted(WINDOWS.glob("*.ps1"))], ids=lambda p: p.name)
def test_the_scripts_parse(script):
    path = str(script).replace("'", "''")
    check = (f"$e = $null; [System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$e) | Out-Null;"
             " $e | ForEach-Object { $_.ToString() }")
    r = subprocess.run([pwsh(), "-NoProfile", "-Command", check], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and not r.stdout.strip(), r.stdout + r.stderr


# ---------- a stub gateway + dashboard ----------

class Stub:
    """The dashboard's device endpoints and /api/me/status, and the proxy's /v1/models, on one port."""

    def __init__(self):
        self.tokens: list[tuple[int, dict]] = []   # answers to /api/device/token, in order; then pending
        self.revoked: set[str] = set()   # keys /api/me/status answers 401 for, as for a computer removed in the dashboard
        self.status_line: str | None = "maya · daily $10/$100"   # None: /api/me/status fails
        self.account_line = "maya · user · key sk-proxy-ab1… for PC, authorized 2026-09-20"   # ?format=account
        self.models_status = 200
        self.logout = (200, {"ok": True, "revoked": True})   # POST /api/me/logout: status, body
        self.files: dict[str, bytes] = {}   # GET path -> body, e.g. the dashboard's /install.ps1
        self.requests: list[tuple[str, dict, dict]] = []
        d = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                d.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, {}))
                if self.path in d.files:
                    self.reply(200, d.files[self.path], "text/plain; charset=utf-8")
                elif self.path.startswith("/v1/models"):
                    self.reply(d.models_status, {"data": [], "has_more": False})
                elif self.path.startswith("/api/me/status") and self.headers.get("authorization", "").removeprefix("Bearer ") in d.revoked:
                    self.reply(401, {"detail": "Not signed in."})
                elif self.path.startswith("/api/me/status"):
                    line = d.account_line if "format=account" in self.path else d.status_line
                    self.reply(200 if line is not None else 503, ((line or "") + "\n").encode(), "text/plain; charset=utf-8")
                else:
                    self.reply(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                d.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if self.path == "/api/device/start":
                    self.reply(200, {"device_code": "dc-1", "user_code": CODE, "interval": 0.1, "expires_in": 20,
                                     "verification_uri": f"{d.url}/dashboard#authorize",
                                     "verification_uri_complete": f"{d.url}/dashboard#authorize/{CODE}"})
                elif self.path == "/api/device/token":
                    self.reply(*(d.tokens.pop(0) if d.tokens else (400, {"error": "authorization_pending"})))
                elif self.path == "/api/me/logout":
                    self.reply(*d.logout)
                else:
                    self.reply(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [p for p, *_ in self.requests]


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.server.shutdown()


@pytest.fixture(scope="session")
def source_zip(tmp_path_factory):
    """The repository's Windows scripts as GitHub's zip has them: under one top-level folder."""
    path = tmp_path_factory.mktemp("zip") / "claude-proxy-test.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.write(INSTALL, "claude-proxy-test/install.ps1")
        for f in WINDOWS.iterdir():
            z.write(f, f"claude-proxy-test/scripts/windows/{f.name}")
    return path


class Win:
    """A sandboxed Windows user: its own USERPROFILE and TEMP, the source zip, and no browser."""

    def __init__(self, tmp_path, source_zip, home="home"):
        self.home = tmp_path / home
        self.temp = tmp_path / "temp"
        self.bin = self.home / ".local" / "bin"
        for p in (self.home, self.temp):
            p.mkdir(parents=True, exist_ok=True)
        # A Claude Code that runs, as install.ps1 checks for one (CI has none); fake_claude's goes ahead of it.
        self.claudebin = tmp_path / "claudebin"
        self.claudebin.mkdir(exist_ok=True)
        (self.claudebin / "claude.cmd").write_text("@echo 2.1.0 (Claude Code)\r\n")
        self.env = {k: v for k, v in os.environ.items() if k != "SSH_CONNECTION"}
        self.env.update({"USERPROFILE": str(self.home), "TEMP": str(self.temp), "TMP": str(self.temp),
                         "CLAUDE_GATEWAY_ZIP": str(source_zip), "CLAUDE_GATEWAY_BIN": str(self.bin),
                         "CLAUDE_GATEWAY_OPEN": "none",
                         "CLAUDE_GATEWAY_CLAUDE_INSTALLER": str(tmp_path / "no-claude-installer.ps1"), "PATH": f"{self.bin};{self.claudebin};{os.environ['PATH']}"})
        self.config = self.home / ".config" / "claude-gateway"
        self.gdir = self.config / "claude"

    def run(self, argv, stdin=None, **env):   # a variable given as None is left out
        full = {k: v for k, v in {**self.env, **env}.items() if v is not None}
        r = subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=full, timeout=120)
        r.out = r.stdout + r.stderr
        return r

    def ps(self, command, **env):
        return self.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], **env)

    def install(self, *args, **env):
        """install.ps1 the way the dashboard's /install.ps1 runs it: a scriptblock in the caller's own session. A
        failure is a throw, which the session catches and goes on from; an `exit` would end it (try can't catch
        that), so 'session still open' never prints."""
        quoted = " ".join("'" + a.replace("'", "''") + "'" for a in args)
        return self.ps(f"try {{ & ([scriptblock]::Create((Get-Content -Raw -LiteralPath '{INSTALL}'))) {quoted} }}"
                       " catch { Write-Output \"install.ps1 threw: $_\" }; Write-Output 'session still open'", **env)

    def install_command(self, *args, **env):
        """install.ps1 the way gclaude.cmd's update runs it: the dashboard's /install.ps1 wrapper (web.install_ps1),
        `& ([scriptblock]::Create((irm <installer>))) <args>`, piped into iex inside the try/catch that Gclaude-On
        writes, since Windows PowerShell's -Command exits 0 when a throw happens inside iex."""
        quoted = " ".join("'" + a.replace("'", "''") + "'" for a in args)
        wrapper = f"& ([scriptblock]::Create((Get-Content -Raw -LiteralPath '{INSTALL}'))) {quoted}"
        return self.ps("try { '" + wrapper.replace("'", "''") + "' | iex } catch { [Console]::Error.WriteLine($_); exit 1 }", **env)

    def cg(self, *args, **env):
        return self.run(["cmd.exe", "/d", "/c", "claude-gateway", *args], **env)

    def client(self):
        return json.loads((self.config / "client.json").read_text(encoding="utf-8"))

    def settings(self):
        raw = (self.gdir / "settings.json").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "settings.json has a BOM"
        return json.loads(raw)


@pytest.fixture
def win(tmp_path, source_zip):
    return Win(tmp_path, source_zip)


@pytest.fixture
def installed(win):
    r = win.install()
    assert r.returncode == 0 and "session still open" in r.out, r.out
    return win


# ---------- install.ps1 ----------

@on_windows
def test_install_puts_the_scripts_and_the_launcher_in_place(installed):
    win = installed
    share = win.home / ".local" / "share" / "claude-gateway" / "scripts" / "windows"
    assert (share / "claude-gateway.ps1").is_file()
    assert "rem Installed by claude-gateway install.ps1." in (win.bin / "claude-gateway.cmd").read_text()
    r = win.cg("help")
    assert r.returncode == 0 and "claude-gateway (Windows)" in r.out, r.out
    again = win.install()   # an update replaces the copy and the launcher
    assert again.returncode == 0 and "session still open" in again.out, again.out
    assert not (win.home / ".local" / "share" / "claude-gateway.new").exists()


@on_windows
def test_install_leaves_a_foreign_launcher_alone(win):
    win.bin.mkdir(parents=True)
    (win.bin / "claude-gateway.cmd").write_bytes(b"@echo mine\r\n")
    r = win.install()
    assert "session still open" in r.out and "left as it is" in r.out, r.out
    assert (win.bin / "claude-gateway.cmd").read_bytes() == b"@echo mine\r\n"
    assert not (win.home / ".local" / "share" / "claude-gateway").exists()


@on_windows
def test_install_reports_a_bad_archive_without_closing_the_session(win, tmp_path):
    r = win.install(CLAUDE_GATEWAY_ZIP=str(tmp_path / "missing.zip"))
    assert "session still open" in r.out and "could not get" in r.out, r.out


@on_windows
def test_a_failed_install_fails_the_command_that_ran_it(win, tmp_path):
    """`powershell -Command "irm ... | iex"` (gclaude update) must exit non-zero when install.ps1 fails: a bad
    archive, a foreign launcher, or the chained `on` failing after a good install."""
    r = win.install_command(CLAUDE_GATEWAY_ZIP=str(tmp_path / "missing.zip"))
    assert r.returncode != 0 and "could not get" in r.out, r.out
    win.bin.mkdir(parents=True)
    (win.bin / "claude-gateway.cmd").write_bytes(b"@echo mine\r\n")
    r = win.install_command()
    assert r.returncode != 0 and "left as it is" in r.out, r.out
    (win.bin / "claude-gateway.cmd").unlink()
    r = win.install_command("on", "--url", "http://127.0.0.1:9", "--dashboard", "http://127.0.0.1:9")
    assert r.returncode != 0 and "Can't reach the dashboard" in r.out and "claude-gateway on failed" in r.out, r.out
    assert (win.bin / "claude-gateway.cmd").is_file()   # the install itself went through
    assert win.install_command().returncode == 0


def no_claude_path(win, tmp_path):
    """PATH without any claude: the machine's own folders that hold one left out."""
    keep = [d for d in os.environ["PATH"].split(";") if d and not any((Path(d) / f"claude{x}").exists() for x in (".exe", ".cmd", ".bat"))]
    return ";".join([str(win.bin), *keep])


@on_windows
def test_install_installs_claude_code_when_it_is_missing(win, tmp_path):
    # Like Claude Code's own installer: claude into .local\bin, and `exit` at the end, which mustn't end the session
    installer = tmp_path / "claude-install.ps1"
    installer.write_text("Write-Output 'Setting up Claude Code...'\n$d = Join-Path $env:USERPROFILE '.local\\bin'; New-Item -ItemType Directory -Force $d | Out-Null\n"
                         "Set-Content -LiteralPath (Join-Path $d 'claude.cmd') -Value '@echo 2.1.0 (Claude Code)'\nexit 0\n")
    r = win.install(PATH=no_claude_path(win, tmp_path), CLAUDE_GATEWAY_CLAUDE_INSTALLER=str(installer))
    assert "session still open" in r.out and "Installing Claude Code" in r.out and "threw" not in r.out, r.out
    assert "Installed Claude Code 2.1.0." in r.out and "Setting up" not in r.out, r.out   # its installer's output hidden
    assert (win.bin / "claude-gateway.cmd").exists()


@on_windows
@pytest.mark.parametrize("script", [None, "Write-Error broke\nexit 1\n", "exit 0\n"])   # no installer, fails, installs nothing
def test_install_stops_when_claude_code_cannot_be_installed(win, tmp_path, script):
    env = {"PATH": no_claude_path(win, tmp_path)}
    if script:
        (tmp_path / "claude-install.ps1").write_text(script)
        env["CLAUDE_GATEWAY_CLAUDE_INSTALLER"] = str(tmp_path / "claude-install.ps1")
    r = win.install(**env)
    assert "session still open" in r.out and "Claude Code (claude) could not be installed" in r.out, r.out
    assert ("broke" in r.out) == ("broke" in (script or "")), r.out   # its installer's output, on a failure
    assert not (win.bin / "claude-gateway.cmd").exists()
    r = win.install_command(**env)   # gclaude update's way: a failure exits non-zero
    assert r.returncode != 0 and "could not be installed" in r.out, r.out


@on_windows
def test_install_stops_when_claude_does_not_run(win, tmp_path):
    bad = tmp_path / "badclaude"
    bad.mkdir()
    (bad / "claude.cmd").write_text("@echo Error: claude native binary not installed. 1>&2\r\n@exit /b 1\r\n")
    r = win.install(PATH=f"{bad};{no_claude_path(win, tmp_path)}")
    assert "session still open" in r.out and "doesn't run" in r.out and "native binary not installed" in r.out, r.out
    assert "npm install -g @anthropic-ai/claude-code" in r.out
    assert not (win.bin / "claude-gateway.cmd").exists()


@on_windows
def test_help_prints_the_whole_header(installed):
    r = installed.cg("help")
    assert r.returncode == 0, r.out
    header = WINDOWS / "claude-gateway.ps1"
    lines = header.read_text(encoding="utf-8").splitlines()[1:]
    comment = [l[2:] if l.startswith("# ") else l[1:] for l in lines[:next(i for i, l in enumerate(lines) if not l.startswith("#"))]]
    assert comment[-1].startswith("Installed by install.ps1") and all(l in r.out for l in comment), r.out


# ---------- claude-gateway on / off / status ----------

def fake_claude(win, tmp_path):
    """A claude.cmd first on PATH that records, as UTF-8, the config dir gclaude gave it and its arguments. Not
    with echo: cmd would split a folder name with & in it, and print in the console's code page."""
    d = tmp_path / "fakebin"
    d.mkdir()
    (d / "saw.ps1").write_text("if ($args -contains '--version') { '2.1.0 (Claude Code)'; exit 0 }   # install.ps1's check\n"
                               "[IO.File]::WriteAllText($env:GW_OUT, $env:CLAUDE_CONFIG_DIR + '|' + ($args -join ' '))\n"
                               "if ($env:GW_SIGNOUT) { [IO.File]::WriteAllText((Join-Path $env:CLAUDE_CONFIG_DIR 'signed-out'), $env:GW_SIGNOUT) }\n")
    (d / "claude.cmd").write_text('@powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0saw.ps1" %*\r\n')
    return {"PATH": f"{d};{win.env['PATH']}", "GW_OUT": str(tmp_path / "claude-saw.txt")}


def claude_saw(tmp_path):
    return (tmp_path / "claude-saw.txt").read_text(encoding="utf-8")


@on_windows
def test_on_authorizes_in_the_browser_and_sets_up_gclaude(installed, stub, tmp_path):
    win = installed
    stub.tokens = [(400, {"error": "authorization_pending"}), (400, {"error": "authorization_pending"}),
                   (200, {"key": "sk-proxy-new", "user": "ana"})]
    r = win.cg("on", "--url", stub.url + "/", "--dashboard", stub.url)
    assert r.returncode == 0, r.out
    assert f"{stub.url}/dashboard#authorize/{CODE}" in r.out and CODE in r.out and "Authorized as ana" in r.out
    assert "sk-proxy-new" not in r.out
    start = next(b for p, _, b in stub.requests if p == "/api/device/start")
    assert start == {"label": os.environ["COMPUTERNAME"]}
    assert [h["authorization"] for p, h, _ in stub.requests if p.startswith("/v1/models")] == ["Bearer sk-proxy-new"]
    c = win.client()
    assert (c["url"], c["key"], c["dashboard"]) == (stub.url, "sk-proxy-new", stub.url)
    s = win.settings()
    assert s["env"] == {"ANTHROPIC_BASE_URL": stub.url, "ANTHROPIC_AUTH_TOKEN": "sk-proxy-new", "CLAUDE_GATEWAY_DASHBOARD": stub.url,
                        "ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-fable-5-1[1m]", "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5[1m]",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5[1m]"}
    assert s["disableClaudeAiConnectors"] is True
    line = s["statusLine"]["command"]
    assert line.startswith("powershell -NoProfile -ExecutionPolicy Bypass -File \"") and line.endswith("/statusline.ps1\"")
    assert "\\" not in line and s["statusLine"]["refreshInterval"] == 30
    assert s["hooks"]["UserPromptSubmit"] == [{"hooks": [{"type": "command", "command": line + " --warn", "timeout": 10}]}]
    assert (win.config / "statusline.ps1").is_file()
    for name in "usage", "account", "logout_gclaude":
        assert "# Installed by claude-gateway on --gclaude." in (win.gdir / "commands" / f"{name}.md").read_text()
    assert json.loads((win.gdir / ".claude.json").read_text())["hasCompletedOnboarding"] is True
    r = win.run(["cmd.exe", "/d", "/c", "gclaude", "-p", "hi"], **fake_claude(win, tmp_path))
    assert r.returncode == 0 and claude_saw(tmp_path) == f"{win.gdir}|-p hi", r.out
    r = win.cg("status")
    assert r.returncode == 0 and f"gateway: {stub.url}" in r.out and "gclaude: installed" in r.out, r.out
    assert "status: maya" in r.out
    # Again with the saved key: no new authorization, and nothing added twice.
    stub.requests.clear()
    r = win.cg("on")
    assert r.returncode == 0, r.out
    assert "/api/device/start" not in stub.paths()
    assert win.settings() == s


@on_windows
def test_on_with_a_key_keeps_the_users_settings_and_off_gives_them_back(installed, stub):
    win = installed
    mine = {"model": "opus", "statusLine": {"type": "command", "command": "mine"},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "beep"}]}]}}
    win.gdir.mkdir(parents=True)
    (win.gdir / "settings.json").write_text(json.dumps(mine))
    r = win.cg("on", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url)
    assert r.returncode == 0, r.out
    assert "/api/device/start" not in stub.paths() and "already has a statusline" in r.out
    s = win.settings()
    assert s["model"] == "opus" and s["statusLine"] == mine["statusLine"] and s["hooks"]["Stop"] == mine["hooks"]["Stop"]
    assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("--warn")
    r = win.cg("off")
    assert r.returncode == 0 and "gclaude is removed" in r.out, r.out
    assert win.settings() == mine
    assert not (win.bin / "gclaude.cmd").exists() and not (win.gdir / "commands").exists()
    assert (win.gdir / ".claude.json").exists()   # gclaude's own state and history stay
    assert "gclaude" not in win.client() and win.client()["key"] == "sk-proxy-k"
    assert "gclaude: not set up" in win.cg("status").out


@on_windows
@pytest.mark.parametrize("error, message", [("access_denied", "Cancelled in the browser"),
                                            ("expired_token", "The code expired")])
def test_a_refused_authorization_changes_nothing(installed, stub, error, message):
    win = installed
    stub.tokens = [(400, {"error": error})]
    r = win.cg("on", "--url", stub.url, "--dashboard", stub.url)
    assert r.returncode != 0 and message in r.out, r.out
    assert not (win.config / "client.json").exists() and not (win.gdir / "settings.json").exists()
    assert not (win.bin / "gclaude.cmd").exists()


@on_windows
def test_a_key_the_gateway_refuses_changes_nothing(installed, stub):
    win = installed
    stub.models_status = 401
    r = win.cg("on", "--url", stub.url, "--key", "sk-proxy-bad", "--dashboard", stub.url)
    assert r.returncode != 0 and "did not accept the key (HTTP 401)" in r.out, r.out
    assert not (win.config / "client.json").exists() and not (win.gdir / "settings.json").exists()


@on_windows
def test_on_leaves_a_foreign_gclaude_alone(installed, stub):
    win = installed
    (win.bin / "gclaude.cmd").write_bytes(b"@echo mine\r\n")
    r = win.cg("on", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url)
    assert r.returncode != 0 and "did not install it" in r.out, r.out
    assert (win.bin / "gclaude.cmd").read_bytes() == b"@echo mine\r\n"
    assert not stub.requests


@on_windows
@pytest.mark.parametrize("flag", ["--global", "--opencode", "--own-login"])
def test_modes_not_on_windows_yet_point_at_the_issue(installed, flag):
    r = installed.cg("on", flag)
    assert r.returncode != 0 and "issues/22" in r.out, r.out


# ---------- statusline.ps1, run the way Claude Code runs gclaude's settings ----------

@pytest.fixture
def gclaude(installed, stub):
    win = installed
    r = win.cg("on", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url)
    assert r.returncode == 0, r.out
    s = win.settings()
    env = {**s["env"], "CLAUDE_CONFIG_DIR": str(win.gdir)}
    line = s["statusLine"]["command"]
    return lambda extra="", stdin="{}", **more: win.run(f"cmd.exe /d /c {line}{extra}", stdin=stdin, **{**env, **more})


@on_windows
def test_the_statusline_shows_the_figures(gclaude, stub):
    stub.status_line = "maya \u00b7 daily $85/$100 \u00b7 5h 10%"
    r = gclaude()
    assert r.returncode == 0, r.out
    assert "\u25c6" in r.stdout and "\x1b[33m$85/$100 85%" in r.stdout and "5h 10%" in r.stdout, r.out
    assert [h["authorization"] for p, h, _ in stub.requests if p.startswith("/api/me/status")][-1] == "Bearer sk-proxy-k"


@on_windows
def test_the_statusline_says_so_when_the_gateway_is_down(gclaude, stub):
    stub.status_line = None
    r = gclaude()
    assert r.returncode == 0 and "gateway status unavailable" in r.stdout, r.out


@on_windows
@pytest.mark.parametrize("which", ["statusline", "hook"])
def test_the_statusline_and_hook_leave_the_consoles_code_page_alone(installed, stub, tmp_path, which):
    """Claude Code shares its console with the statusline and hook. Were they to switch it to UTF-8 (65001), the old
    console (conhost) would draw Claude Code's logo and bullets as boxes, as plain claude never does. A console of
    its own, since pytest's pipes have none, and cmd reports the code page it's left with."""
    win = installed
    assert win.cg("on", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url).returncode == 0
    s = win.settings()
    env = {**win.env, **s["env"], "CLAUDE_CONFIG_DIR": str(win.gdir)}
    command = s["statusLine"]["command"] if which == "statusline" else s["hooks"]["UserPromptSubmit"][-1]["hooks"][0]["command"]
    out, cp, prompt = tmp_path / "out.txt", tmp_path / "cp.txt", tmp_path / "prompt.json"
    prompt.write_text('{"prompt": "hi"}', encoding="ascii")
    script = tmp_path / "run.cmd"
    script.write_text(f'@chcp 437 >nul\r\n@{command} <"{prompt}" >"{out}"\r\n@chcp >"{cp}"\r\n', encoding="ascii")
    subprocess.run(["cmd.exe", "/d", "/c", str(script)], env=env, timeout=120, creationflags=subprocess.CREATE_NEW_CONSOLE)
    if which == "statusline":
        assert "\u25c6" in out.read_text(encoding="utf-8"), out.read_text(encoding="utf-8", errors="replace")
    assert cp.read_text().strip().endswith("437"), cp.read_text()


@on_windows
def test_the_warning_hook_warns_once_and_answers_usage(gclaude, stub):
    stub.status_line = "maya \u00b7 daily $85/$100"
    r = gclaude(" --warn", '{"prompt": "hi"}')
    assert r.returncode == 0, r.out
    assert json.loads(r.stdout)["systemMessage"].startswith("Gateway: maya \u00b7 daily $85/$100 85%")
    r = gclaude(" --warn", '{"prompt": "hi"}')
    assert r.returncode == 0 and not r.stdout.strip(), r.out   # once per band, not on every prompt
    r = gclaude(" --warn", '{"prompt": "/usage"}')
    out = json.loads(r.stdout)
    assert out["decision"] == "block" and "$85/$100 85%" in out["reason"] and f"{stub.url}/dashboard" in out["reason"]


@on_windows
def test_account_prints_the_line_and_the_hook_answers_only_at_a_limit(gclaude, installed, stub):
    r = gclaude(" --account")
    assert r.returncode == 0, r.out
    assert r.stdout.strip() == f"Account: maya \u00b7 user \u00b7 key sk-proxy-ab1\u2026 for PC, authorized 2026-09-20 \u00b7 dashboard: {stub.url}/dashboard"
    assert "--account`" in (installed.gdir / "commands" / "account.md").read_text()
    stub.status_line = "maya \u00b7 credit $1/$5"
    r = gclaude(" --warn", '{"prompt": "/account"}')
    assert r.returncode == 0 and not r.stdout.strip(), r.out   # the model answers it
    stub.status_line = "maya \u00b7 credit $5/$5"
    out = json.loads(gclaude(" --warn", '{"prompt": "/account"}').stdout)
    assert out["decision"] == "block" and "for PC" in out["reason"] and "limit reached" in out["reason"]


@on_windows
def test_on_replaces_the_older_logout_command(installed, stub):
    win = installed
    assert win.cg("on", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url).returncode == 0
    old = win.gdir / "commands" / "logout.md"   # what an older gclaude named /logout_gclaude
    old.write_text("<!-- # Installed by claude-gateway on --gclaude. -->\n")
    r = win.cg("on")
    assert r.returncode == 0, r.out
    assert not old.exists() and (win.gdir / "commands" / "logout_gclaude.md").is_file()


@on_windows
def test_gclaude_signs_in_again_when_its_key_was_removed_in_the_dashboard(gclaude, installed, stub, tmp_path):
    win = installed
    stub.revoked.add("sk-proxy-k")
    stub.tokens = [(200, {"key": "sk-proxy-new", "user": "ana"})]
    fake = fake_claude(win, tmp_path)
    r = win.run(["cmd.exe", "/d", "/c", "gclaude", "-p", "hi"], **fake)
    assert r.returncode == 0 and "key no longer works" in r.out and "Authorized as ana" in r.out, r.out
    assert win.settings()["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-new"
    assert claude_saw(tmp_path) == f"{win.gdir}|-p hi"
    assert all("sk-proxy" not in p for p, *_ in stub.requests)   # the key goes in a header, never the URL


@on_windows
def test_gclaude_starts_as_usual_while_its_key_works(gclaude, installed, stub, tmp_path):
    win = installed
    fake = fake_claude(win, tmp_path)
    r = win.run(["cmd.exe", "/d", "/c", "gclaude", "-p", "hi"], **fake)
    assert r.returncode == 0 and "sign" not in r.out and claude_saw(tmp_path) == f"{win.gdir}|-p hi", r.out
    assert "/api/device/start" not in stub.paths()


@on_windows
def test_gclaude_status_and_uninstall(gclaude, installed, stub, tmp_path):
    win = installed
    fake = fake_claude(win, tmp_path)
    r = win.run(["cmd.exe", "/d", "/c", "gclaude", "status"], **fake)
    assert r.returncode == 0 and f"gateway: {stub.url}" in r.out and "status: maya" in r.out, r.out
    assert not (tmp_path / "claude-saw.txt").exists()
    r = win.run(["cmd.exe", "/d", "/c", "gclaude", "uninstall"], **fake)
    assert r.returncode == 0 and "gclaude is removed" in r.out, r.out
    assert not (win.bin / "gclaude.cmd").exists() and not (tmp_path / "claude-saw.txt").exists()


@on_windows
def test_logout_revokes_the_key_and_the_next_gclaude_signs_in_again(gclaude, installed, stub, tmp_path):
    win = installed
    out = json.loads(gclaude(" --warn", '{"prompt": "/logout_gclaude"}').stdout)
    assert out["continue"] is False and "revoked on the gateway" in out["stopReason"], out
    assert "run gclaude again" in out["stopReason"]
    assert [h["authorization"] for p, h, _ in stub.requests if p == "/api/me/logout"] == ["Bearer sk-proxy-k"]
    s = win.settings()
    assert "ANTHROPIC_AUTH_TOKEN" not in s["env"] and s["env"]["ANTHROPIC_BASE_URL"] == stub.url
    assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("--warn")   # arrays stay arrays
    assert "key" not in win.client() and win.client()["url"] == stub.url
    assert (win.gdir / "signed-out").is_file()
    fake = fake_claude(win, tmp_path)
    stub.tokens = [(400, {"error": "access_denied"})]   # cancelled: Claude Code is not started
    r = win.run(["cmd.exe", "/d", "/c", "gclaude", "-p", "hi"], **fake)
    assert r.returncode == 1 and "Cancelled in the browser" in r.out, r.out
    assert (win.gdir / "signed-out").is_file() and not (tmp_path / "claude-saw.txt").exists()
    stub.tokens = [(200, {"key": "sk-proxy-new", "user": "ana"})]   # like plain claude: gclaude signs in, then starts
    r = win.run(["cmd.exe", "/d", "/c", "gclaude", "-p", "hi"], **fake)
    assert r.returncode == 0 and "Authorized as ana" in r.out, r.out
    assert not (win.gdir / "signed-out").exists()
    assert win.settings()["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-new"
    assert claude_saw(tmp_path) == f"{win.gdir}|-p hi"


@on_windows
@pytest.mark.skipif(not shutil.which("node"), reason="no node here")
def test_logout_ends_the_claude_session_that_ran_it(installed, stub, tmp_path):
    """Stopped, as Windows has no SIGINT: only a claude running the hook, and only when Claude Code runs it."""
    win = installed
    assert win.cg("on", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url).returncode == 0
    s = win.settings()
    fake = tmp_path / "claude.js"   # node running claude, as an npm install does
    fake.write_text("""
const hook = require("child_process").spawn(process.argv[2], { shell: true, stdio: ["pipe", "inherit", "inherit"] });
hook.stdin.end(JSON.stringify({ prompt: "/logout_gclaude" }));
hook.on("close", () => setTimeout(() => process.exit(7), 15000));
""")
    argv = ["node", str(fake), s["statusLine"]["command"] + " --warn"]
    started = time.monotonic()
    r = win.run(argv, **s["env"], CLAUDE_CONFIG_DIR=str(win.gdir), CLAUDE_PROJECT_DIR=str(tmp_path))
    assert r.returncode not in (0, 7) and time.monotonic() - started < 14, r.out
    assert "gclaude is closing" in r.stdout
    said = (win.gdir / "signed-out").read_text(encoding="utf-8")
    assert said.startswith("gclaude: Signed out: this computer's key is revoked") and "Run gclaude again" in said
    assert win.cg("on", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url).returncode == 0
    r = win.run(argv, **s["env"], CLAUDE_CONFIG_DIR=str(win.gdir))   # not run by Claude Code: left running
    assert r.returncode == 7 and "Exit gclaude now (/exit)" in r.stdout, r.out


@on_windows
def test_gclaude_shows_why_claude_closed_after_a_logout(gclaude, installed, tmp_path):
    win = installed
    fake = fake_claude(win, tmp_path)
    r = win.run(["cmd.exe", "/d", "/c", "gclaude", "-p", "hi"], **fake, GW_SIGNOUT="gclaude: Signed out: test.\n")
    assert r.returncode == 0 and "gclaude: Signed out: test." in r.out and "\x1b[?25h" in r.stdout, r.out


@on_windows
@pytest.mark.parametrize("body", [b"", b"<html>maintenance</html>"], ids=["empty", "html"])
def test_logout_after_a_200_with_an_odd_reply_still_signs_out_and_says_so(gclaude, installed, stub, body):
    """A 2xx whose body isn't the JSON expected is not 'the gateway could not be reached'."""
    stub.logout = (200, body)
    out = json.loads(gclaude(" --warn", '{"prompt": "/logout_gclaude"}').stdout)
    assert out["continue"] is False, out
    assert "unexpected reply" in out["stopReason"] and "HTTP 200" in out["stopReason"], out
    assert "could not be reached" not in out["stopReason"] and "revoked on the gateway" not in out["stopReason"], out
    assert "ANTHROPIC_AUTH_TOKEN" not in installed.settings()["env"] and "key" not in installed.client()
    assert (installed.gdir / "signed-out").is_file()


@on_windows
def test_logout_tells_a_refusal_and_an_unreachable_gateway_apart(gclaude, installed, stub):
    stub.logout = (500, {"error": "boom"})
    out = json.loads(gclaude(" --warn", '{"prompt": "/logout_gclaude"}').stdout)
    assert "refused to revoke it (HTTP 500)" in out["stopReason"] and "could not be reached" not in out["stopReason"], out
    assert "ANTHROPIC_AUTH_TOKEN" not in installed.settings()["env"]
    out = json.loads(gclaude(" --warn", '{"prompt": "/logout_gclaude"}', CLAUDE_GATEWAY_DASHBOARD="http://127.0.0.1:9").stdout)
    assert "could not be reached" in out["stopReason"] and "HTTP" not in out["stopReason"], out


@on_windows
def test_a_key_file_that_cannot_be_made_private_gets_a_warning(win, tmp_path):
    """icacls fails on FAT/exFAT and some shared drives; the file is written all the same, and the warning names it."""
    script = str(WINDOWS / "claude-gateway.ps1").replace("'", "''")
    real, shown = tmp_path / "ok.tmp", tmp_path / "client.json"
    real.write_text("{}")
    r = win.ps(f". '{script}'; Protect '{real}' '{shown}'; Protect '{tmp_path / 'missing.tmp'}' '{shown}'; Write-Output done",
               CLAUDE_GATEWAY_CLIENT=str(tmp_path / "none.json"))
    assert "done" in r.out and r.out.count("WARNING") == 1 and "client.json" in r.out and "icacls" in r.out, r.out


@on_windows
def test_gclaude_update_stops_when_the_gateway_update_fails(gclaude, installed, stub, tmp_path):
    """install.ps1 throws, the try/catch in gclaude.cmd exits 1, and its :updatefailed path exits 1 instead of running
    `claude update`."""
    win = installed
    stub.files["/install.ps1"] = INSTALL.read_bytes()
    fake = fake_claude(win, tmp_path)
    r = win.run(["cmd.exe", "/d", "/c", "gclaude", "update"], CLAUDE_GATEWAY_ZIP=str(tmp_path / "missing.zip"), **fake)
    assert r.returncode == 1 and "could not get" in r.out and "gateway update failed" in r.out, r.out
    assert not (tmp_path / "claude-saw.txt").exists()   # Claude Code's own update did not run


@on_windows
def test_gclaude_update_runs_the_dashboards_installer_then_claude_update(gclaude, installed, stub, tmp_path):
    win = installed
    marker = tmp_path / "installer-ran.txt"
    stub.files["/install.ps1"] = f"[IO.File]::WriteAllText('{marker}', 'yes')\n".encode()
    fake = fake_claude(win, tmp_path)
    r = win.run(["cmd.exe", "/d", "/c", "gclaude", "update"], **fake)
    assert r.returncode == 0 and marker.read_text() == "yes", r.out
    assert claude_saw(tmp_path).endswith("|update") and str(win.gdir) not in claude_saw(tmp_path)   # plain claude's own update


@on_windows
def test_the_warning_hook_is_quiet_below_80_percent(gclaude, stub):
    stub.status_line = "maya \u00b7 daily $50/$100"
    r = gclaude(" --warn", '{"prompt": "hi"}')
    assert r.returncode == 0 and not r.stdout.strip(), r.out


# ---------- a profile folder with spaces, cmd metacharacters and non-ASCII letters ----------

GIT_BASH = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"


@on_windows
def test_everything_works_from_a_profile_folder_like_ivan_and_co(tmp_path, source_zip, stub):
    """cmd.exe reads .cmd files in the console's code page, not UTF-8, and expands % in them; Claude Code may run
    the statusline through cmd, PowerShell or Git Bash."""
    win = Win(tmp_path, source_zip, home="Иван Müller & 100%x")
    r = win.install()
    assert r.returncode == 0 and "session still open" in r.out, r.out
    assert win.cg("help").returncode == 0
    r = win.cg("on", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url)
    assert r.returncode == 0, r.out
    for f in (win.bin / "claude-gateway.cmd", win.bin / "gclaude.cmd"):
        f.read_bytes().decode("ascii")   # nothing a code page could garble
    r = win.run(["cmd.exe", "/d", "/c", "gclaude"], **fake_claude(win, tmp_path))
    assert r.returncode == 0 and claude_saw(tmp_path) == f"{win.gdir}|", r.out
    s = win.settings()
    env = {**s["env"], "CLAUDE_CONFIG_DIR": str(win.gdir)}
    line = s["statusLine"]["command"]
    stub.status_line = "maya \u00b7 daily $85/$100"
    shells = [["cmd.exe", "/d", "/s", "/c", f'"{line}"'], ["powershell.exe", "-NoProfile", "-Command", line]]
    if GIT_BASH.exists():
        shells.append([str(GIT_BASH), "-c", line])
    for argv in shells:
        r = win.run(argv if argv[0] != "cmd.exe" else " ".join(argv), stdin="{}", **env)
        assert r.returncode == 0 and "$85/$100 85%" in r.stdout, (argv[0], r.out)


@on_windows
def test_adding_to_path_keeps_its_variables(win):
    r"""The user's PATH is REG_EXPAND_SZ with entries like %USERPROFILE%\...\WindowsApps; rewriting it must not
    freeze them to today's values."""
    import winreg
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS)
    try:
        before = winreg.QueryValueEx(k, "Path")
    except FileNotFoundError:
        before = None
    try:
        winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, r"%SystemRoot%\gw-test;C:\gw-plain")
        env = {k2: v for k2, v in win.env.items() if k2 != "CLAUDE_GATEWAY_BIN"}   # the default bin: PATH is ours to edit
        install = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                   f"& ([scriptblock]::Create((Get-Content -Raw -LiteralPath '{INSTALL}')))"]
        r = subprocess.run(install, capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        value, kind = winreg.QueryValueEx(k, "Path")
        assert kind == winreg.REG_EXPAND_SZ
        assert value == r"%SystemRoot%\gw-test;C:\gw-plain;" + str(win.bin)
        subprocess.run(install, capture_output=True, text=True, env=env, timeout=120)
        assert winreg.QueryValueEx(k, "Path")[0] == value   # not added twice
    finally:
        if before is None:
            winreg.DeleteValue(k, "Path")
        else:
            winreg.SetValueEx(k, "Path", 0, before[1], before[0])
        winreg.CloseKey(k)


# ---------- the dashboard's one-liner, end to end ----------

@on_windows
def test_the_dashboards_one_liner_installs_and_authorizes(win, stub):
    """`irm <dashboard>/install.ps1 | iex` in the user's own PowerShell, as the dashboard serves it: install.ps1, which
    hands `on --url … --dashboard …` to claude-gateway in a child PowerShell, then gclaude."""
    c = Config()
    c.listener.public_url = c.listener.dashboard_url = stub.url
    c.signup.installer_ps1_url = f"{stub.url}/raw/install.ps1"
    stub.files = {"/install.ps1": install_ps1(c).encode(), "/raw/install.ps1": INSTALL.read_bytes()}
    stub.tokens = [(400, {"error": "authorization_pending"}), (200, {"key": "sk-proxy-new", "user": "ana"})]
    r = win.ps(f"irm {stub.url}/install.ps1 | iex; Write-Output 'session still open'")
    assert "session still open" in r.out and "claude-gateway is installed" in r.out, r.out
    assert "Authorized as ana" in r.out and "gclaude now runs Claude Code" in r.out, r.out
    c = win.client()
    assert (c["url"], c["key"], c["dashboard"]) == (stub.url, "sk-proxy-new", stub.url)
    assert win.settings()["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-new"
    assert (win.bin / "gclaude.cmd").is_file()


# ---------- authorizing: the paths the macOS/Linux tests cover too ----------

def opener(tmp_path):
    """A stand-in browser that records the link it was asked to open."""
    log = tmp_path / "opened.txt"
    (tmp_path / "opener.cmd").write_text(f'@echo %~1> "{log}"\r\n')
    return str(tmp_path / "opener.cmd"), log


@on_windows
def test_slow_down_and_a_gateway_restart_while_waiting_are_not_fatal(installed, stub, tmp_path):
    win = installed
    stub.tokens = [(400, {"error": "slow_down"}), (503, {}), (502, {}), (429, {}),
                   (200, {"key": "sk-proxy-new", "user": "ana"})]
    path, log = opener(tmp_path)
    r = win.cg("on", "--url", stub.url, "--dashboard", stub.url, CLAUDE_GATEWAY_OPEN=path)
    assert r.returncode == 0 and "Authorized as ana" in r.out, r.out
    assert win.client()["key"] == "sk-proxy-new"
    for _ in range(50):   # Start-Process doesn't wait for the browser
        if log.exists():
            break
        time.sleep(0.1)
    assert log.read_text().strip() == f"{stub.url}/dashboard#authorize/{CODE}"


@on_windows
def test_over_ssh_it_only_prints_the_link(installed, stub):
    stub.tokens = [(200, {"key": "sk-proxy-new", "user": "ana"})]
    r = installed.cg("on", "--url", stub.url, "--dashboard", stub.url, CLAUDE_GATEWAY_OPEN=None,
                     SSH_CONNECTION="10.0.0.1 5000 10.0.0.2 22")
    assert r.returncode == 0, r.out
    assert f"{stub.url}/dashboard#authorize/{CODE}" in r.out and "open in your browser" not in r.out


@on_windows
def test_an_unreachable_or_wrong_dashboard_says_so(installed, stub):
    win = installed
    r = win.cg("on", "--url", "http://127.0.0.1:9", "--dashboard", "http://127.0.0.1:9")
    assert r.returncode != 0 and "Can't reach the dashboard at http://127.0.0.1:9" in r.out, r.out
    r = win.cg("on", "--url", stub.url, "--dashboard", f"{stub.url}/nope")
    assert r.returncode != 0 and "Give it with --dashboard" in r.out, r.out
    r = win.cg("on", "--url", "https://claude.invalid")   # the dashboard follows the gateway's naming
    assert r.returncode != 0 and "https://claude-dash.invalid" in r.out, r.out
    assert not (win.config / "client.json").exists()


@on_windows
def test_login_authorizes_again_and_a_key_alone_keeps_the_saved_gateway(installed, stub):
    win = installed
    stub.tokens = [(200, {"key": "sk-proxy-one", "user": "ana"})]
    assert win.cg("on", "--url", stub.url, "--dashboard", stub.url).returncode == 0
    stub.requests.clear()
    stub.tokens = [(200, {"key": "sk-proxy-two", "user": "ana"})]
    r = win.cg("on", "--login")
    assert r.returncode == 0 and "/api/device/start" in stub.paths(), r.out
    assert win.client()["key"] == "sk-proxy-two" and win.settings()["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-two"
    stub.requests.clear()
    r = win.cg("on", "--key", "sk-proxy-three")
    assert r.returncode == 0 and "/api/device/start" not in stub.paths(), r.out
    c = win.client()
    assert (c["url"], c["key"], c["dashboard"]) == (stub.url, "sk-proxy-three", stub.url)
