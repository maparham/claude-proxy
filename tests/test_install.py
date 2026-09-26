"""install.sh, run against a tarball of this checkout instead of GitHub."""
import os
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


def install(home, tarball, *args):
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(home / "tmp"),
           "CLAUDE_GATEWAY_TARBALL": f"file://{tarball}"}
    return subprocess.run(["sh", str(INSTALL), *args], capture_output=True, text=True, env=env, timeout=60)


def test_install_puts_claude_gateway_on_the_path_with_its_files(home, tarball):
    r = install(home, tarball)
    assert r.returncode == 0, r.stderr
    share = home / ".local" / "share" / "claude-gateway"
    link = home / ".local" / "bin" / "claude-gateway"
    assert os.readlink(link) == str(share / "scripts" / "claude-gateway")
    for f in ("scripts/claude-gateway", "scripts/statusline.sh", "scripts/gclaude-sync.py", "examples/opencode/muse.md"):
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
