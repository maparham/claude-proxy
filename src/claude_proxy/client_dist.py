"""The client the dashboard's /install serves itself: install.sh, install.ps1 and the archives they unpack.

The repository is private, so raw.githubusercontent.com and codeload.github.com answer 404 to anyone without
access; the gateway hands out the same files instead. They come from CLAUDE_PROXY_CLIENT_DIR (the Docker image
copies them to /app/client), else from the checkout this package runs from. scripts/VERSION is filled in as
GitHub's archives did (.gitattributes): from CLAUDE_PROXY_CLIENT_VERSION (`git describe`, set at image build),
else `git describe` in a checkout.
"""
from __future__ import annotations

import functools
import io
import os
import subprocess
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

PREFIX = "claude-proxy"   # the archives' top directory, as GitHub's: the installers strip or skip it
FILES = ("scripts/claude-gateway", "scripts/statusline.sh", "scripts/gclaude-sync.py")   # + VERSION, windows/, opencode/


@dataclass(frozen=True)
class Dist:
    install_sh: bytes
    install_ps1: bytes
    tarball: bytes
    zip: bytes


def client_dir() -> Path | None:
    env = os.environ.get("CLAUDE_PROXY_CLIENT_DIR")
    d = Path(env) if env else Path(__file__).resolve().parents[2]
    return d if (d / "install.sh").is_file() and (d / "scripts" / "claude-gateway").is_file() else None


def _version(d: Path) -> str:
    v = os.environ.get("CLAUDE_PROXY_CLIENT_VERSION", "").strip()
    if v.startswith("v"):
        return v
    try:
        v = subprocess.run(["git", "-C", str(d), "describe", "--tags", "--match", "v[0-9]*"],
                           capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        v = ""
    return v if v.startswith("v") else ""


def _members(d: Path) -> list[tuple[str, bytes, int]]:
    """(path in the archive, content, mode) for what the installers copy out."""
    paths = [d / f for f in FILES]
    paths += sorted(p for p in (d / "scripts" / "windows").rglob("*") if p.is_file())
    paths += sorted(p for p in (d / "examples" / "opencode").rglob("*") if p.is_file())
    out = [(p.relative_to(d).as_posix(), p.read_bytes(), 0o755 if os.access(p, os.X_OK) else 0o644) for p in paths]
    out.append(("scripts/VERSION", (_version(d) + "\n").encode(), 0o644))
    return out


@functools.cache
def dist() -> Dist | None:
    d = client_dir()
    if d is None:
        return None
    members, now = _members(d), int(time.time())
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w:gz") as t:
        for name, data, mode in members:
            info = tarfile.TarInfo(f"{PREFIX}/{name}")
            info.size, info.mode, info.mtime = len(data), mode, now
            t.addfile(info, io.BytesIO(data))
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data, mode in members:
            info = zipfile.ZipInfo(f"{PREFIX}/{name}", time.localtime(now)[:6])
            info.external_attr, info.compress_type = (0o100000 | mode) << 16, zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return Dist((d / "install.sh").read_bytes(), (d / "install.ps1").read_bytes(), tbuf.getvalue(), zbuf.getvalue())
