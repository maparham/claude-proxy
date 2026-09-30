"""install.sh, run against a tarball of this checkout instead of GitHub."""
import os
import shutil
import subprocess
import tarfile

import pytest

from tests.test_client_scripts import ROOT, Stub

INSTALL = ROOT / "install.sh"


@pytest.fixture
def tarball(tmp_path):
    """Like GitHub's archive: everything under one top-level folder."""
    path = tmp_path / "claude-proxy-master.tar.gz"
    with tarfile.open(path, "w:gz") as t:
        for part in ("scripts", "examples"):
            t.add(ROOT / part, arcname=f"claude-proxy-master/{part}")
    return path


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    (h / "tmp").mkdir()
    return h


def fake_claude(home, body='#!/bin/sh\necho "2.1.0 (Claude Code)"\n', mode=0o755):
    """A claude of our own ahead of the machine's: one that runs by default."""
    fake = home / "claudebin"
    fake.mkdir(exist_ok=True)
    (fake / "claude").write_text(body)
    (fake / "claude").chmod(mode)
    return fake


def install(home, tarball, *args, path=None):
    if path is None:
        path = f"{home / 'claudebin'}:{os.environ['PATH']}" if (home / "claudebin").exists() else \
            f"{fake_claude(home)}:{os.environ['PATH']}"
    env = {"PATH": path, "HOME": str(home), "TMPDIR": str(home / "tmp"),
           "CLAUDE_GATEWAY_TARBALL": f"file://{tarball}"}
    return subprocess.run(["sh", str(INSTALL), *args], capture_output=True, text=True, env=env, timeout=60)


def test_install_puts_claude_gateway_on_the_path_with_its_files(home, tarball):
    r = install(home, tarball)
    assert r.returncode == 0, r.stderr
    share = home / ".local" / "share" / "claude-gateway"
    link = home / ".local" / "bin" / "claude-gateway"
    assert os.readlink(link) == str(share / "scripts" / "claude-gateway")
    for f in ("scripts/claude-gateway", "scripts/statusline.sh", "scripts/gclaude-sync.py", "scripts/i18n.json", "examples/opencode/muse.md"):
        assert (share / f).read_bytes() == (ROOT / f).read_bytes()
    assert os.access(share / "scripts" / "claude-gateway", os.X_OK)
    assert "Add " in r.stdout                                          # ~/.local/bin isn't on this PATH
    help_out = subprocess.run([str(link), "--help"], capture_output=True, text=True).stdout
    assert "claude-gateway on --gclaude" in help_out


def test_install_again_replaces_the_old_copy(home, tarball):
    assert install(home, tarball).returncode == 0
    stale = home / ".local" / "share" / "claude-gateway" / "scripts" / "old-file"
    stale.write_text("from an earlier version")
    assert install(home, tarball).returncode == 0
    assert not stale.exists()


def test_install_runs_claude_gateway_with_the_arguments_after_it(home, tarball):
    stub = Stub()
    try:
        r = install(home, tarball, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full")
    finally:
        stub.server.shutdown()
    assert r.returncode == 0, r.stderr
    assert "gclaude now runs Claude Code through the gateway" in r.stdout
    assert (home / ".local" / "bin" / "gclaude").exists()
    assert (home / ".config" / "claude-gateway" / "gclaude-sync.py").read_bytes() == \
        (ROOT / "scripts" / "gclaude-sync.py").read_bytes()


def test_install_leaves_a_claude_gateway_that_is_not_a_link_alone(home, tarball):
    link = home / ".local" / "bin" / "claude-gateway"
    link.parent.mkdir(parents=True)
    link.write_text("#!/bin/sh\necho mine\n")
    r = install(home, tarball)
    assert r.returncode == 1 and "not a link" in r.stderr
    assert link.read_text() == "#!/bin/sh\necho mine\n"
    assert not (home / ".local" / "share" / "claude-gateway").exists()


def test_a_failed_download_keeps_the_installed_copy(home, tarball, tmp_path):
    assert install(home, tarball).returncode == 0
    r = install(home, tmp_path / "missing.tar.gz")
    assert r.returncode != 0
    assert (home / ".local" / "share" / "claude-gateway" / "scripts" / "claude-gateway").exists()


# ---------- Claude Code must run first ----------

def no_claude_path(tmp_path):
    """The system tools the installer needs, and no claude."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for t in ("sh", "curl", "tar", "python3", "gzip", "mktemp", "rm", "mkdir", "cp", "chmod", "mv", "ln", "head", "sed",
              "cat", "dirname", "basename", "readlink", "uname", "env", "bash"):
        found = shutil.which(t)
        if found:
            (tools / t).symlink_to(found)
    return str(tools)


def test_install_stops_when_claude_is_missing(home, tarball, tmp_path):
    r = install(home, tarball, path=no_claude_path(tmp_path))
    assert r.returncode == 1 and "Claude Code (claude) is needed first" in r.stderr
    assert not (home / ".local" / "bin" / "claude-gateway").exists()


def test_install_stops_when_claude_does_not_run(home, tarball):
    # npm's placeholder when the native binary never arrived
    fake_claude(home, '#!/bin/sh\necho "Error: claude native binary not installed." >&2\nexit 1\n')
    r = install(home, tarball)
    assert r.returncode == 1 and "doesn't run" in r.stderr and "native binary not installed" in r.stderr
    assert "npm install -g @anthropic-ai/claude-code" in r.stderr
    assert not (home / ".local" / "bin" / "claude-gateway").exists()


def test_install_stops_when_claude_cannot_be_executed(home, tarball, tmp_path):
    fake = fake_claude(home, "not a program\n", mode=0o644)
    r = install(home, tarball, path=f"{fake}:{no_claude_path(tmp_path)}")   # no working claude later on PATH
    assert r.returncode == 1 and "doesn't run" in r.stderr and str(home / "claudebin" / "claude") in r.stderr
    assert not (home / ".local" / "bin" / "claude-gateway").exists()


def test_install_for_opencode_only_needs_no_claude(home, tarball, tmp_path):
    r = install(home, tarball, "on", "--opencode", "--url", "http://127.0.0.1:9", path=no_claude_path(tmp_path))
    assert "Claude Code" not in r.stderr
    assert (home / ".local" / "bin" / "claude-gateway").is_symlink()
