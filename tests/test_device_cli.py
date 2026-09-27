"""`claude-gateway on` without a key: it authorizes in the browser (sign-up design section 6)."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_proxy.config import Config
from claude_proxy.web import install_sh
from tests.test_client_scripts import GATEWAY, ROOT, run
from tests.test_install import tarball  # noqa: F401  (a fixture)

CODE = "BCDF-GHJK"


class Dashboard:
    """The dashboard's device endpoints plus the proxy's /v1/models, which `on` checks the new key against."""

    def __init__(self):
        self.tokens: list[tuple[int, dict]] = []   # answers to /api/device/token, in order; then "pending"
        self.files: dict[str, bytes] = {}           # GET path -> body, e.g. the dashboard's /install
        self.requests: list[tuple[str, dict, dict]] = []
        d = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                d.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, {}))
                if self.path in d.files:
                    body = d.files[self.path]
                    self.send_response(200)
                    self.send_header("content-type", "text/plain; charset=utf-8")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.reply(200, {"data": [], "has_more": False} if self.path.startswith("/v1/models") else {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                d.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if self.path == "/api/device/start":
                    self.reply(200, {"device_code": "dc-1", "user_code": CODE, "interval": 0.05, "expires_in": 5,
                                     "verification_uri": f"{d.url}/dashboard#authorize",
                                     "verification_uri_complete": f"{d.url}/dashboard#authorize/{CODE}"})
                elif self.path == "/api/device/token":
                    assert body == {"device_code": "dc-1"}
                    self.reply(*(d.tokens.pop(0) if d.tokens else (400, {"error": "authorization_pending"})))
                else:
                    self.reply(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [p for p, *_ in self.requests]


@pytest.fixture
def dash():
    d = Dashboard()
    yield d
    d.server.shutdown()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "tmp").mkdir(parents=True)
    return h


def cg(home, *args, **env):
    base = {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(home / "tmp")}
    return run(["bash", str(GATEWAY), *args], {**base, **env})


def opener(tmp_path, name="opener"):
    """A stand-in for `open`/`xdg-open` that records what it was asked to open."""
    log = tmp_path / f"{name}.log"
    path = tmp_path / name
    path.write_text(f'#!/bin/sh\necho "$1" >> {log}\n')
    path.chmod(0o755)
    return path, log


def client(home):
    return json.loads((home / ".config" / "claude-gateway" / "client.json").read_text())


def test_on_without_a_key_authorizes_in_the_browser(dash, home, tmp_path):
    dash.tokens = [(400, {"error": "authorization_pending"}), (200, {"key": "sk-proxy-machine", "user": "maya"})]
    path, log = opener(tmp_path)
    r = cg(home, "on", "--url", dash.url, "--dashboard", dash.url, CLAUDE_GATEWAY_OPEN=str(path))
    assert r.returncode == 0, r.stderr
    assert f"open {dash.url}/dashboard#authorize/{CODE}" in r.stderr and f"shows the code {CODE}" in r.stderr
    assert "Authorized as maya" in r.stderr and "sk-proxy-machine" not in r.stdout + r.stderr
    assert log.read_text().strip() == f"{dash.url}/dashboard#authorize/{CODE}"
    c = client(home)
    assert (c["url"], c["key"], c["dashboard"]) == (dash.url, "sk-proxy-machine", dash.url)
    start = next(b for p, _, b in dash.requests if p == "/api/device/start")
    assert start["label"] and "." not in start["label"]   # the short host name
    g = json.loads((home / ".config" / "claude-gateway" / "claude" / "settings.json").read_text())
    assert g["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-machine"   # gclaude set up with the new key
    models = [h for p, h, _ in dash.requests if p.startswith("/v1/models")]
    assert models and models[-1]["authorization"] == "Bearer sk-proxy-machine"


def test_over_ssh_it_only_prints_the_link(dash, home, tmp_path):
    dash.tokens = [(200, {"key": "sk-proxy-machine", "user": "maya"})]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("open", "xdg-open"):
        opener(bindir, name)
    r = cg(home, "on", "--url", dash.url, "--dashboard", dash.url, SSH_CONNECTION="10.0.0.1 5000 10.0.0.2 22",
           PATH=f"{bindir}:{os.environ['PATH']}")
    assert r.returncode == 0, r.stderr
    assert f"{dash.url}/dashboard#authorize/{CODE}" in r.stderr and "open in your browser" not in r.stderr
    assert not list(bindir.glob("*.log"))


def test_cancelled_in_the_browser_changes_nothing(dash, home):
    dash.tokens = [(400, {"error": "access_denied"})]
    r = cg(home, "on", "--url", dash.url, "--dashboard", dash.url, CLAUDE_GATEWAY_OPEN="")
    assert r.returncode == 1 and "Cancelled in the browser" in r.stderr
    assert not (home / ".config" / "claude-gateway" / "client.json").exists()
    assert not (home / ".local" / "bin" / "gclaude").exists()


def test_rerunning_with_a_saved_key_for_that_gateway_does_not_ask_again(dash, home):
    dash.tokens = [(200, {"key": "sk-proxy-machine", "user": "maya"})]
    assert cg(home, "on", "--url", dash.url, "--dashboard", dash.url, CLAUDE_GATEWAY_OPEN="").returncode == 0
    dash.requests.clear()
    r = cg(home, "on", "--url", dash.url + "/", "--dashboard", dash.url, CLAUDE_GATEWAY_OPEN="")   # e.g. the installer again
    assert r.returncode == 0, r.stderr
    assert "/api/device/start" not in dash.paths() and client(home)["key"] == "sk-proxy-machine"
    dash.tokens = [(200, {"key": "sk-proxy-again", "user": "maya"})]
    r = cg(home, "on", "--login", CLAUDE_GATEWAY_OPEN="")   # saved URL and dashboard
    assert r.returncode == 0, r.stderr
    assert "/api/device/start" in dash.paths() and client(home)["key"] == "sk-proxy-again"


def test_a_gateway_restart_while_waiting_is_not_fatal(dash, home):
    dash.tokens = [(502, {}), (429, {}), (200, {"key": "sk-proxy-machine", "user": "maya"})]
    r = cg(home, "on", "--url", dash.url, "--dashboard", dash.url, CLAUDE_GATEWAY_OPEN="")
    assert r.returncode == 0, r.stderr
    assert client(home)["key"] == "sk-proxy-machine"


def test_expired_code_and_unreachable_dashboard(dash, home):
    dash.tokens = [(400, {"error": "expired_token"})]
    r = cg(home, "on", "--url", dash.url, "--dashboard", dash.url, CLAUDE_GATEWAY_OPEN="")
    assert r.returncode == 1 and "The code expired" in r.stderr
    r = cg(home, "on", "--url", "http://127.0.0.1:9", "--dashboard", "http://127.0.0.1:9", CLAUDE_GATEWAY_OPEN="")
    assert r.returncode == 1 and "Can't reach the dashboard at http://127.0.0.1:9" in r.stderr
    assert cg(home, "on", "--key", "sk-proxy-x", "--login").returncode == 1
    r = cg(home, "on")
    assert r.returncode == 1 and "claude-gateway on --url <gateway url>" in r.stderr


def test_the_dashboard_url_follows_the_gateway_naming(home):
    # eval, not `source <(...)`: bash 3.2, a Mac's own, reads nothing from a process substitution there.
    r = run(["bash", "-c", f'eval "$(sed -n "/^dashboard_for()/,/^}}/p;/^field()/p" {GATEWAY})"; CLIENT=/nonexistent; '
                           'dashboard_for https://claude.example.com'], {"PATH": os.environ["PATH"], "HOME": str(home)})
    assert r.stdout.strip() == "https://claude-dash.example.com", r.stderr


def test_the_dashboards_one_liner_installs_and_authorizes(dash, home, tmp_path, tarball):
    """`curl -fsSL <dashboard>/install | sh`, as the dashboard serves it: install.sh, then `on`, then gclaude."""
    c = Config()
    c.listener.public_url = c.listener.dashboard_url = dash.url
    c.signup.installer_url = f"{dash.url}/raw/install.sh"
    dash.files = {"/install": install_sh(c).encode(), "/raw/install.sh": (ROOT / "install.sh").read_bytes()}
    dash.tokens = [(400, {"error": "authorization_pending"}), (200, {"key": "sk-proxy-machine", "user": "maya"})]
    path, log = opener(tmp_path)
    r = run(["sh", "-c", f"curl -fsSL {dash.url}/install | sh"],
            {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(home / "tmp"),
             "CLAUDE_GATEWAY_TARBALL": f"file://{tarball}", "CLAUDE_GATEWAY_OPEN": str(path)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "claude-gateway is installed" in r.stdout and "Authorized as maya" in r.stderr
    assert log.read_text().strip() == f"{dash.url}/dashboard#authorize/{CODE}"
    c = client(home)
    assert (c["url"], c["key"], c["dashboard"]) == (dash.url, "sk-proxy-machine", dash.url)
    assert (home / ".local" / "bin" / "gclaude").is_file()
    g = json.loads((home / ".config" / "claude-gateway" / "claude" / "settings.json").read_text())
    assert g["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy-machine"
