#!/usr/bin/env python3
"""Share this machine's own Claude Code setup with gclaude; run by the gclaude command at each start.

    gclaude-sync.py sync   <gclaude dir> <own dir>     # link, then report what was left alone (stderr)
    gclaude-sync.py unsync <gclaude dir> <own dir>     # undo, for `claude-gateway off --gclaude`

<own dir> is ~/.claude. What is shared:
- commands: each entry of <own>/commands is linked into gclaude's own commands/ folder (which also holds gclaude's
  /usage), and links whose target is gone are dropped.
- plugins: <gclaude>/plugins is a link to <own>/plugins, so plugins are installed once for both, and the
  enabledPlugins and extraKnownMarketplaces entries of <own>/settings.json are copied into gclaude's settings.json.
  Those win over gclaude's own entries of the same name; entries only gclaude has are kept.
- memory: projects/<project>/memory is a link to <own>/projects/<project>/memory for every project that has
  memories there, and for the current project once plain `claude` has been used in it. Session history stays
  separate. A gclaude memory folder that already holds memories is left alone.
Nothing in <own> is changed, except that the current project's memory folder is created there to link to.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

PLUGIN_KEYS = ("enabledPlugins", "extraKnownMarketplaces")


def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600)
    os.replace(tmp, path)


def points_to(link, target):
    return os.path.islink(link) and os.readlink(link) == target


# ---------- commands ----------

def sync_commands(gdir, own):
    src, dst = os.path.join(own, "commands"), os.path.join(gdir, "commands")
    if not os.path.isdir(dst) or os.path.islink(dst):   # only gclaude's own real folder
        return
    if os.path.isdir(src):
        for name in os.listdir(src):
            link = os.path.join(dst, name)
            if not os.path.lexists(link):
                os.symlink(os.path.join(src, name), link)
    for name in os.listdir(dst):
        link = os.path.join(dst, name)
        if os.path.islink(link) and not os.path.exists(link) and os.path.dirname(os.readlink(link)) == src:
            os.remove(link)


def unsync_commands(gdir, own):
    src, dst = os.path.join(own, "commands"), os.path.join(gdir, "commands")
    if os.path.isdir(dst) and not os.path.islink(dst):
        for name in os.listdir(dst):
            link = os.path.join(dst, name)
            if os.path.islink(link) and os.path.dirname(os.readlink(link)) == src:
                os.remove(link)


# ---------- plugins ----------

def untouched_plugins_dir(path):
    """The folder Claude Code makes on its first start: the official marketplace list, nothing installed."""
    return os.path.isdir(path) and not os.path.islink(path) and \
        set(os.listdir(path)) <= {"known_marketplaces.json", "marketplaces"}


def sync_plugins(gdir, own, notes):
    src, dst = os.path.join(own, "plugins"), os.path.join(gdir, "plugins")
    if os.path.isdir(src) and not points_to(dst, src):
        if untouched_plugins_dir(dst):
            shutil.rmtree(dst)
        if os.path.lexists(dst):
            notes.append(f"{dst} is gclaude's own, so your plugins are not shared with gclaude.")
        else:
            os.symlink(src, dst)
    own_settings = load(os.path.join(own, "settings.json"))
    settings_path = os.path.join(gdir, "settings.json")
    settings = load(settings_path)
    if own_settings is None or settings is None:
        return
    changed = False
    for key in PLUGIN_KEYS:
        theirs = own_settings.get(key)
        if isinstance(theirs, dict) and theirs:
            mine = settings.get(key) if isinstance(settings.get(key), dict) else {}
            merged = {**mine, **theirs}
            if settings.get(key) != merged:
                settings[key] = merged
                changed = True
    if changed:
        write_json(settings_path, settings)


def unsync_plugins(gdir, own):
    src, dst = os.path.join(own, "plugins"), os.path.join(gdir, "plugins")
    if points_to(dst, src):
        os.remove(dst)
    own_settings = load(os.path.join(own, "settings.json")) or {}
    settings_path = os.path.join(gdir, "settings.json")
    settings = load(settings_path)
    if settings is None:
        return
    changed = False
    for key in PLUGIN_KEYS:
        mine, theirs = settings.get(key), own_settings.get(key)
        if isinstance(mine, dict) and isinstance(theirs, dict):
            kept = {k: v for k, v in mine.items() if not (k in theirs and theirs[k] == v)}   # gclaude's own stay
            if kept != mine:
                changed = True
                if kept:
                    settings[key] = kept
                else:
                    del settings[key]
    if changed:
        write_json(settings_path, settings)


# ---------- memory ----------

def project_name(cwd):
    """Claude Code's folder name for the project at cwd: its main git checkout (so worktrees share it), else cwd."""
    root = cwd
    try:
        common = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        if common:
            root = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    except (OSError, subprocess.SubprocessError):
        pass
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(root))


def link_memory(gdir, own, name, notes):
    src = os.path.join(own, "projects", name, "memory")
    dst = os.path.join(gdir, "projects", name, "memory")
    if points_to(dst, src):
        return
    if os.path.isdir(dst) and not os.path.islink(dst) and not os.listdir(dst):
        os.rmdir(dst)   # Claude Code makes it empty on the first session there
    if os.path.lexists(dst):
        notes.append(f"{dst} already holds gclaude's own memories, so it is not shared with plain claude.")
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.symlink(src, dst)


def sync_memory(gdir, own, cwd, notes):
    projects = os.path.join(own, "projects")
    if not os.path.isdir(projects):
        return
    current = project_name(cwd)
    for name in sorted(os.listdir(projects)):
        memory = os.path.join(projects, name, "memory")
        if name == current and os.path.isdir(os.path.join(projects, name)):
            os.makedirs(memory, exist_ok=True)   # the one change in <own>: a folder to link to
        if os.path.isdir(memory) and not os.path.islink(memory):
            link_memory(gdir, own, name, notes)


def unsync_memory(gdir, own):
    projects = os.path.join(gdir, "projects")
    if not os.path.isdir(projects):
        return
    for name in os.listdir(projects):
        dst = os.path.join(projects, name, "memory")
        if points_to(dst, os.path.join(own, "projects", name, "memory")):
            os.remove(dst)


def main():
    mode, gdir, own = sys.argv[1:4]
    if mode == "sync":
        notes = []
        for step in (lambda: sync_commands(gdir, own), lambda: sync_plugins(gdir, own, notes),
                     lambda: sync_memory(gdir, own, os.getcwd(), notes)):
            try:
                step()
            except OSError as e:   # one step failing never stops the others, or gclaude
                notes.append(f"gclaude-sync: {e}")
        for note in notes:
            print(note, file=sys.stderr)
    elif mode == "unsync":
        unsync_commands(gdir, own)
        unsync_plugins(gdir, own)
        unsync_memory(gdir, own)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
