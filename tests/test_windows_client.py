"""The Windows client (install.ps1, scripts/windows), run under Windows PowerShell 5.1 against a stub gateway and
dashboard (Windows client design section 5). Everything but the static checks runs only on Windows: CI's
windows-client workflow."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

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
        self.status_line: str | None = "maya · daily $10/$100"   # None: /api/me/status fails
        self.models_status = 200
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
                if self.path.startswith("/v1/models"):
                    self.reply(d.models_status, {"data": [], "has_more": False})
                elif self.path.startswith("/api/me/status"):
                    ok = d.status_line is not None
                    self.reply(200 if ok else 503, ((d.status_line or "") + "\n").encode(), "text/plain; charset=utf-8")
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

    def __init__(self, tmp_path, source_zip):
        self.home = tmp_path / "home"
        self.temp = tmp_path / "temp"
        self.bin = self.home / ".local" / "bin"
        for p in (self.home, self.temp):
            p.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if k != "SSH_CONNECTION"}
        self.env.update({"USERPROFILE": str(self.home), "TEMP": str(self.temp), "TMP": str(self.temp),
                         "CLAUDE_GATEWAY_ZIP": str(source_zip), "CLAUDE_GATEWAY_BIN": str(self.bin),
                         "CLAUDE_GATEWAY_OPEN": "none"})
        self.config = self.home / ".config" / "claude-gateway"
        self.gdir = self.config / "claude"

    def run(self, argv, stdin=None, **env):
        r = subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env={**self.env, **env}, timeout=120)
        r.out = r.stdout + r.stderr
        return r

    def ps(self, command, **env):
        return self.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], **env)

    def install(self, *args, **env):
        """install.ps1 the way the dashboard's /install.ps1 runs it: a scriptblock in the caller's own session."""
        quoted = " ".join("'" + a.replace("'", "''") + "'" for a in args)
        return self.ps(f"& ([scriptblock]::Create((Get-Content -Raw -LiteralPath '{INSTALL}'))) {quoted};"
                       " Write-Output 'session still open'", **env)

    def cg(self, *args, **env):
        return self.run(["cmd.exe", "/d", "/c", str(self.bin / "claude-gateway.cmd"), *args], **env)

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
    (win.bin / "claude-gateway.cmd").write_text("@echo mine\r\n")
    r = win.install()
    assert "session still open" in r.out and "left as it is" in r.out, r.out
    assert (win.bin / "claude-gateway.cmd").read_text() == "@echo mine\r\n"
    assert not (win.home / ".local" / "share" / "claude-gateway").exists()


@on_windows
def test_install_reports_a_bad_archive_without_closing_the_session(win, tmp_path):
    r = win.install(CLAUDE_GATEWAY_ZIP=str(tmp_path / "missing.zip"))
    assert "session still open" in r.out and "could not get" in r.out, r.out
