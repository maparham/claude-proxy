"""End to end: a new computer joins the gateway the way a person does it, and asks Claude something.

1. A gateway starts here: the Docker image the deploy ships (--docker), or `claude-proxy serve` from this checkout.
   The installers it points to are this checkout's, served locally.
2. On a clean user account, the dashboard's own one-liner runs: `curl -fsSL <dashboard>/install | sh`, or
   `irm <dashboard>/install.ps1 | iex` on Windows.
3. A real browser (Playwright's Chromium) opens the link it prints, signs in with the gateway key of the user `e2e`,
   checks the code and clicks Authorize.
4. The one-liner finishes and sets up gclaude. `gclaude -p` asks Claude Code for "pong"; the status line and
   `claude-gateway status` read the dashboard; `claude-gateway off` removes gclaude again.

Claude's answer comes from E2E_UPSTREAM when it is set (e.g. https://claude.rahkar.pro, with E2E_UPSTREAM_TOKEN a
gateway key there: this gateway then sends its Claude requests to that one, so the answer is real), else from
e2e/fake_anthropic.py. Needs Claude Code (`claude`) on PATH, and Playwright:

  uv run --with playwright python -m playwright install chromium     # once
  uv run --with playwright python e2e/run.py [--docker]

Nothing outside a temporary folder is touched: the "computer" is a fresh home folder there. Exits non-zero on a
failure, after printing the gateway's log and the one-liner's output; E2E_ARTIFACTS keeps screenshots and logs.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "e2e"
WIN = sys.platform == "win32"
PROXY, DASH, FAKE, FILES = 18480, 18481, 18490, 18491
PROXY_URL, DASH_URL = f"http://127.0.0.1:{PROXY}", f"http://127.0.0.1:{DASH}"
COMPOSE = ["docker", "compose", "-f", str(E2E / "docker-compose.yml")]


class Failed(Exception):
    pass


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def check(ok, msg: str) -> None:
    if not ok:
        raise Failed(msg)


def get(url: str, timeout=5) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def wait_for(what: str, test, timeout: float, proc=None, log: Path | None = None):
    end = time.time() + timeout
    while time.time() < end:
        try:
            value = test()
            if value:
                return value
        except OSError:
            pass
        if proc is not None and proc.poll() is not None:
            raise Failed(f"{what}: the process ended (exit {proc.returncode}) first"
                         + (f":\n{log.read_text(errors='replace')}" if log and log.exists() else ""))
        time.sleep(0.5)
    raise Failed(f"{what}: not within {timeout:.0f} s")


def serve_installers(tmp: Path) -> None:
    """install.sh and install.ps1 over HTTP, as raw.githubusercontent.com serves them; the source archives the
    installers unpack go through CLAUDE_GATEWAY_TARBALL / _ZIP instead."""
    site = tmp / "site"
    site.mkdir()
    for name in ("install.sh", "install.ps1"):
        shutil.copy(ROOT / name, site / name)
    class Quiet(SimpleHTTPRequestHandler):
        extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".sh": "text/plain; charset=utf-8",
                          ".ps1": "text/plain; charset=utf-8"}   # as raw.githubusercontent.com: irm then gives text

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", FILES), partial(Quiet, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()


def archives(tmp: Path) -> tuple[Path, Path]:
    """This checkout the way GitHub's archives have it: everything under one top-level folder."""
    tgz, zp = tmp / "source.tar.gz", tmp / "source.zip"
    with tarfile.open(tgz, "w:gz") as t:
        for part in ("scripts", "examples"):
            t.add(ROOT / part, arcname=f"claude-proxy-e2e/{part}")
    with zipfile.ZipFile(zp, "w") as z:
        for f in (ROOT / "scripts" / "windows").iterdir():
            z.write(f, f"claude-proxy-e2e/scripts/windows/{f.name}")
    return tgz, zp


class Gateway:
    def __init__(self, tmp: Path, docker: bool):
        self.tmp, self.docker = tmp, docker
        self.run_dir = tmp / "run"
        self.run_dir.mkdir()
        self.log = tmp / "gateway.log"
        self.procs: list[subprocess.Popen] = []
        self.upstream = os.environ.get("E2E_UPSTREAM", "").rstrip("/")
        self.env = {**os.environ, "E2E_CREDENTIAL_KEY": Fernet.generate_key().decode(), "E2E_RUN_DIR": str(self.run_dir),
                    "E2E_UPSTREAM_TOKEN": os.environ.get("E2E_UPSTREAM_TOKEN", "")}
        if self.upstream:
            self.env["E2E_UPSTREAM"] = self.upstream
        # The gateway's own subscription login is never used here: its usage poll and token refresh go nowhere.
        (self.run_dir / "config.toml").write_text(f'''
[listener]
public_url = "{PROXY_URL}"
dashboard_url = "{DASH_URL}"

[credential]
usage_url = "http://127.0.0.1:9/usage"
token_url = "http://127.0.0.1:9/token"

[signup]
installer_url = "http://127.0.0.1:{FILES}/install.sh"
installer_ps1_url = "http://127.0.0.1:{FILES}/install.ps1"
''')

    def start(self) -> str:
        """Starts the gateway (and the stand-in Anthropic unless there's a real upstream); returns e2e's key."""
        if self.docker:
            run = partial(subprocess.run, env=self.env, check=True)
            run([*COMPOSE, "build", "gateway"])
            if not self.upstream:
                run([*COMPOSE, "up", "-d", "anthropic"])
            key = subprocess.run([*COMPOSE, "run", "--rm", "--no-deps", "-T", "gateway", "python", "/e2e/seed.py"],
                                 env=self.env, check=True, capture_output=True, text=True).stdout.strip().splitlines()[-1]
            run([*COMPOSE, "up", "-d", "--no-deps", "gateway"])
        else:
            env = {**self.env, "CLAUDE_PROXY_CONFIG": str(self.run_dir / "config.toml"),
                   "CLAUDE_PROXY_DB": str(self.run_dir / "claude_proxy.db"),
                   "CLAUDE_PROXY_CREDENTIAL_KEY": self.env["E2E_CREDENTIAL_KEY"],
                   "CLAUDE_PROXY_PORT": str(PROXY), "CLAUDE_PROXY_DASHBOARD_PORT": str(DASH),
                   "CLAUDE_PROXY_UPSTREAM": self.upstream or f"http://127.0.0.1:{FAKE}"}
            log = open(self.log, "w")
            if not self.upstream:
                self.procs.append(subprocess.Popen([sys.executable, str(E2E / "fake_anthropic.py"), str(FAKE)],
                                                   stdout=log, stderr=subprocess.STDOUT))
            key = subprocess.run([sys.executable, str(E2E / "seed.py")], env=env, check=True, capture_output=True,
                                 text=True).stdout.strip().splitlines()[-1]
            self.procs.append(subprocess.Popen([sys.executable, "-c", "from claude_proxy.cli import main; main()", "serve"],
                                               env=env, stdout=log, stderr=subprocess.STDOUT))
        wait_for("the gateway's /health", lambda: get(f"{PROXY_URL}/health")[0] == 200, 90)
        wait_for("the dashboard", lambda: get(f"{DASH_URL}/api/auth-config")[0] == 200, 30)
        check(key.startswith("sk-proxy-"), f"seed.py printed no key: {key[:12]!r}")
        return key

    def logs(self) -> str:
        if self.docker:
            return subprocess.run([*COMPOSE, "logs", "--no-color", "--tail", "200"], env=self.env,
                                  capture_output=True, text=True).stdout
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def stop(self) -> None:
        for p in self.procs:
            p.terminate()
        if self.docker:
            subprocess.run([*COMPOSE, "down", "-v", "--remove-orphans"], env=self.env, capture_output=True)


class Computer:
    """A new user account on this machine: its own home folder, nothing installed, and a browser stand-in that
    records the link claude-gateway asks it to open."""

    def __init__(self, tmp: Path, tgz: Path, zp: Path):
        self.home = tmp / "home"
        (self.home / "tmp").mkdir(parents=True)
        self.bin = self.home / ".local" / "bin"
        self.link_file = tmp / "opened-link.txt"
        claude = shutil.which("claude")
        check(claude, "Claude Code (claude) is not on PATH")
        path = os.pathsep.join([str(self.bin), str(Path(claude).parent), os.environ["PATH"]])
        # Nothing from a Claude Code session this may run inside of (CLAUDECODE, CLAUDE_CODE_*), and no other gateway.
        env = {k: v for k, v in os.environ.items()
               if k != "SSH_CONNECTION" and not k.startswith(("ANTHROPIC_", "CLAUDE_CODE", "CLAUDECODE", "CLAUDE_GATEWAY"))}
        env.update({"HOME": str(self.home), "USERPROFILE": str(self.home), "TMPDIR": str(self.home / "tmp"),
                    "TEMP": str(self.home / "tmp"), "TMP": str(self.home / "tmp"), "PATH": path,
                    "CLAUDE_GATEWAY_TARBALL": tgz.as_uri(), "CLAUDE_GATEWAY_ZIP": str(zp),
                    "CLAUDE_GATEWAY_BIN": str(self.bin),   # Windows: leave this machine's own PATH setting alone
                    "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        if WIN:
            opener = tmp / "opener.cmd"
            opener.write_text(f'@echo %~1> "{self.link_file}"\r\n')
        else:
            opener = tmp / "opener"
            opener.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" > "{self.link_file}"\n')
            opener.chmod(0o755)
        env["CLAUDE_GATEWAY_OPEN"] = str(opener)
        self.env = env

    def one_liner(self, log: Path) -> subprocess.Popen:
        if WIN:
            argv = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                    f"irm {DASH_URL}/install.ps1 | iex"]
        else:
            argv = ["sh", "-c", f"curl -fsSL {DASH_URL}/install | sh"]
        return subprocess.Popen(argv, env=self.env, stdout=open(log, "w"), stderr=subprocess.STDOUT)

    def run(self, argv, timeout=120, stdin="") -> subprocess.CompletedProcess:
        r = subprocess.run(argv, env=self.env, input=stdin, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        r.out = r.stdout + r.stderr
        return r

    def command(self, name: str, *args, **kw) -> subprocess.CompletedProcess:
        """gclaude or claude-gateway, run the way a person types it in their terminal."""
        if WIN:
            return self.run(["cmd.exe", "/d", "/c", name, *args], **kw)
        return self.run([str(self.bin / name), *args], **kw)

    @property
    def gclaude_dir(self) -> Path:
        return self.home / ".config" / "claude-gateway" / "claude"


def authorize_in_the_browser(link: str, code: str, key: str, label: str, shots: Path) -> None:
    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(link)
            # Signed out: the sign-in card. This gateway has no Clerk, so its key form is the way in.
            page.click("#login-switch")
            page.fill("#form-key input[name=key]", key)
            page.click("#form-key button[type=submit]")
            code_box = page.locator(".authorize .user-code")
            expect(code_box).to_have_text(code, timeout=15000)
            expect(page.locator(".authorize")).to_contain_text(label)
            page.screenshot(path=str(shots / "1-authorize.png"))
            page.click("#az-yes")
            expect(page.locator(".authorize h2")).to_have_text("Authorized", timeout=15000)
            page.screenshot(path=str(shots / "2-authorized.png"))
        except Exception:
            page.screenshot(path=str(shots / "failed.png"))
            raise
        finally:
            browser.close()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):   # the status line's diamond, on a Windows console's code page too
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--docker", action="store_true", help="run the gateway as the Docker image the deploy ships")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="gw-e2e-"))
    shots = Path(os.environ.get("E2E_ARTIFACTS") or tmp / "artifacts")
    shots.mkdir(parents=True, exist_ok=True)
    gw = Gateway(tmp, args.docker)
    one_liner_log = shots / "one-liner.log"
    real = bool(gw.upstream)
    try:
        step(f"Gateway: {'Docker image' if args.docker else 'claude-proxy serve'}; Claude answers from "
             f"{gw.upstream if real else 'the stand-in Anthropic'}")
        serve_installers(tmp)
        tgz, zp = archives(tmp)
        key = gw.start()

        step("A new computer runs the dashboard's one-liner")
        pc = Computer(tmp, tgz, zp)
        proc = pc.one_liner(one_liner_log)
        link = wait_for("the authorize link", lambda: pc.link_file.exists() and pc.link_file.read_text().strip(),
                        180, proc, one_liner_log)
        out = one_liner_log.read_text(errors="replace")
        m = re.search(r"shows the code ([A-Z]{4}-[A-Z]{4})", out)
        check(m, f"no code in the terminal:\n{out}")
        check(link == f"{DASH_URL}/dashboard#authorize/{m[1]}", f"unexpected link {link!r}")
        print(f"link {link}, code {m[1]}")

        step("A browser signs in and clicks Authorize")
        label = os.environ.get("COMPUTERNAME", "") if WIN else __import__("socket").gethostname().split(".")[0]
        authorize_in_the_browser(link, m[1], key, label, shots)

        step("The one-liner finishes setting up gclaude")
        try:
            rc = proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise Failed("the one-liner didn't finish within 120 s after Authorize")
        out = one_liner_log.read_text(errors="replace")
        print(out)
        check(rc == 0 or WIN, f"the one-liner exited {rc}")   # irm | iex: the child's exit code isn't passed on
        check("Authorized as e2e" in out and "gclaude now runs Claude Code" in out, "the one-liner did not finish")
        client = json.loads((pc.home / ".config" / "claude-gateway" / "client.json").read_text(encoding="utf-8"))
        check(client["url"] == PROXY_URL and client["key"].startswith("sk-proxy-") and client["key"] != key,
              "client.json lacks this computer's own key")

        step("gclaude asks Claude for pong")
        r = pc.command("gclaude", "-p", "Reply with exactly the word pong, lowercase, and nothing else.",
                       "--model", "haiku", timeout=240)
        print(r.out.strip()[-2000:])
        check(r.returncode == 0 and "pong" in r.stdout.lower(), f"gclaude -p exited {r.returncode} without pong")
        if not real:
            status, body = get(f"http://127.0.0.1:{FAKE}/_requests")
            seen = json.loads(body)
            check(any(s["path"] == "/v1/messages" for s in seen), "Claude Code's request never reached Anthropic")
            check(all(s["authorization"] == "Bearer e2e-access-token" for s in seen),
                  "the gateway passed on something other than its own credential")

        step("The status line reads the dashboard; `claude-gateway status` knows gclaude")
        settings = json.loads((pc.gclaude_dir / "settings.json").read_text(encoding="utf-8"))
        line_env = {**pc.env, **settings["env"], "CLAUDE_CONFIG_DIR": str(pc.gclaude_dir)}
        shell = ["cmd.exe", "/d", "/s", "/c", f'"{settings["statusLine"]["command"]}"'] if WIN \
            else ["sh", "-c", settings["statusLine"]["command"]]
        r = subprocess.run(" ".join(shell) if WIN else shell, env=line_env, input="{}", capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        print(r.stdout.strip())
        check(r.returncode == 0 and "e2e" in r.stdout and "unavailable" not in r.stdout, "the status line is wrong")
        r = pc.command("claude-gateway", "status")
        print(r.out.strip())
        # Windows' status also shows the dashboard's line for this computer's key.
        check(r.returncode == 0 and "gclaude: installed" in r.out and (not WIN or "status: e2e" in r.out),
              "claude-gateway status is wrong")

        step("claude-gateway off removes gclaude")
        r = pc.command("claude-gateway", "off")
        print(r.out.strip())
        check(r.returncode == 0 and not (pc.bin / ("gclaude.cmd" if WIN else "gclaude")).exists(), "gclaude is still there")
        step("PASSED")
        return 0
    except (Failed, subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception) as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        if one_liner_log.exists():
            print("\n--- one-liner ---\n" + one_liner_log.read_text(errors="replace"), file=sys.stderr)
        logs = gw.logs()
        (shots / "gateway.log").write_text(logs, encoding="utf-8")
        print("\n--- gateway (last lines) ---\n" + logs[-6000:], file=sys.stderr)
        return 1
    finally:
        gw.stop()


if __name__ == "__main__":
    sys.exit(main())
