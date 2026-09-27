#!/usr/bin/env python3
"""Share this machine's own Claude Code setup with gclaude; run by the gclaude command at each start.

    gclaude-sync.py sync   <gclaude dir> <own dir>     # link, then report what was left alone (stderr)
    gclaude-sync.py unsync <gclaude dir> <own dir>     # undo, for `claude-gateway off --gclaude`

<own dir> is ~/.claude. What is shared:
- commands: each entry of <own>/commands is linked into gclaude's own commands/ folder (which also holds gclaude's
  /usage), and links whose target is gone are dropped.
- plugins: <gclaude>/plugins is a link to <own>/plugins, so plugins are installed once for both: installing,
  updating or removing one in gclaude does it for plain `claude` too, and `off --gclaude` does not undo that.
  The enabledPlugins and extraKnownMarketplaces entries of <own>/settings.json follow into gclaude's settings.json:
  each sync copies only what plain `claude` changed since the last one (kept in .gclaude-sync.json), so a plugin
  turned on or off in gclaude stays that way until plain `claude` changes the same one.
- MCP servers: the user-scope mcpServers of ~/.claude.json follow into gclaude's .claude.json the same way, so a
  server added, changed or removed in plain `claude` is in gclaude at its next start, and one gclaude changed or
  added itself stays. Project and local-scope servers are not shared (.mcp.json is read by both anyway).
- memory: projects/<project>/memory is a link to <own>/projects/<project>/memory for every project that has
  memories there, and for the current project once plain `claude` has been used in it. A gclaude memory folder that
  already holds memories is left alone.
- sessions: each of plain `claude`'s sessions (projects/<project>/<id>.jsonl) is a hard link in gclaude's projects
  folder, so gclaude's /resume lists it: the picker skips symbolic links, and Claude Code appends to the file in
  place, so a session resumed in gclaude goes on in <own> too. Its <id> folder (subagents, tool results) is a
  symbolic link, and so is its file-history/<id> folder of rewind checkpoints, so /rewind can restore the files it
  changed. Sessions started in gclaude stay gclaude's, and prompt history (history.jsonl) stays separate.
This script itself changes nothing in <own>, except that it creates the current project's memory folder there to
link to. Memories, plugin changes and the turns of a resumed session made in gclaude land in <own>: that is
the point of sharing them.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

PLUGIN_KEYS = ("enabledPlugins", "extraKnownMarketplaces")
MCP_KEYS = ("mcpServers",)
SNAPSHOT = ".gclaude-sync.json"   # in the gclaude dir: <own>'s plugin and MCP entries as of the last sync


def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def points_to(link, target):
    return os.path.islink(link) and os.readlink(link) == target


def inside(path, own):
    """Whether path, through any links, is in <own>: never remove or link anything there."""
    real, own = os.path.realpath(path), os.path.realpath(own)
    return real == own or real.startswith(own + os.sep)


def each(items, step, notes):
    for item in items:   # one entry failing never stops the rest
        try:
            step(item)
        except OSError as e:
            notes.append(f"gclaude-sync: {e}")


# ---------- commands ----------

def sync_commands(gdir, own, notes):
    src, dst = os.path.join(own, "commands"), os.path.join(gdir, "commands")
    if not os.path.isdir(dst) or os.path.islink(dst) or inside(dst, own):   # only gclaude's own real folder
        return

    def add(name):
        if not os.path.lexists(os.path.join(dst, name)):
            os.symlink(os.path.join(src, name), os.path.join(dst, name))

    def prune(name):
        link = os.path.join(dst, name)
        if os.path.islink(link) and not os.path.exists(link) and os.path.dirname(os.readlink(link)) == src:
            os.remove(link)

    each(os.listdir(src) if os.path.isdir(src) else [], add, notes)
    each(os.listdir(dst), prune, notes)


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
    if not os.path.isdir(path) or os.path.islink(path) or not set(os.listdir(path)) <= {"known_marketplaces.json",
                                                                                            "marketplaces"}:
        return False
    known = load(os.path.join(path, "known_marketplaces.json"))
    listed = os.listdir(os.path.join(path, "marketplaces")) if os.path.isdir(os.path.join(path, "marketplaces")) else []
    return set(known or {}) <= {"claude-plugins-official"} and set(listed) <= {"claude-plugins-official"}


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
    follow(own_settings, settings, settings_path, PLUGIN_KEYS, gdir)


def unsync_plugins(gdir, own):
    src, dst = os.path.join(own, "plugins"), os.path.join(gdir, "plugins")
    if points_to(dst, src):
        os.remove(dst)
    unfollow(os.path.join(gdir, "settings.json"), PLUGIN_KEYS, gdir)


# ---------- MCP servers ----------

def mcp_paths(gdir, own):
    """Plain claude keeps its user-scope MCP servers in ~/.claude.json, next to <own>; gclaude in <gclaude>/.claude.json."""
    return os.path.join(os.path.dirname(os.path.abspath(own)), ".claude.json"), os.path.join(gdir, ".claude.json")


def sync_mcp(gdir, own, notes):
    own_path, path = mcp_paths(gdir, own)
    own_data = load(own_path)
    data = load(path) if os.path.exists(path) else {}   # gclaude's first start makes the rest of it
    if own_data is None or data is None:
        return
    follow(own_data, data, path, MCP_KEYS, gdir)


def unsync_mcp(gdir, own):
    unfollow(mcp_paths(gdir, own)[1], MCP_KEYS, gdir)


# ---------- entries that follow plain claude (plugins, MCP servers) ----------

def follow(own_data, data, path, keys, gdir):
    """Copy into data (saved at path) what plain claude changed in each keys dict of own_data since the last sync."""
    snap_path = os.path.join(gdir, SNAPSHOT)
    snap = load(snap_path) or {}
    changed = False
    for key in keys:
        theirs = own_data.get(key) if isinstance(own_data.get(key), dict) else {}
        last = snap.get(key) if isinstance(snap.get(key), dict) else {}
        mine = dict(data[key]) if isinstance(data.get(key), dict) else {}
        for name, value in theirs.items():
            if name not in last or last[name] != value:   # new or changed in plain claude since the last sync
                mine[name] = value
        for name, value in last.items():
            if name not in theirs and mine.get(name) == value:   # dropped in plain claude, untouched in gclaude
                del mine[name]
        if mine != (data.get(key) or {}):
            changed = True
            if mine:
                data[key] = mine
            else:
                data.pop(key, None)
        snap[key] = theirs
    if changed:
        write_json(path, data)
    if snap != load(snap_path):
        write_json(snap_path, snap)


def unfollow(path, keys, gdir):
    """Drop from the file at path the keys entries a sync copied and gclaude left as they were."""
    snap_path = os.path.join(gdir, SNAPSHOT)
    snap, data = load(snap_path) or {}, load(path)
    if data is not None:
        changed = False
        for key in keys:
            mine, synced = data.get(key), snap.get(key)
            if isinstance(mine, dict) and isinstance(synced, dict):
                kept = {k: v for k, v in mine.items() if not (k in synced and synced[k] == v)}   # gclaude's own stay
                if kept != mine:
                    changed = True
                    if kept:
                        data[key] = kept
                    else:
                        del data[key]
        if changed:
            write_json(path, data)
    rest = {k: v for k, v in snap.items() if k not in keys}   # what the other unsync steps still need
    if rest:
        write_json(snap_path, rest)
    elif os.path.exists(snap_path):
        os.remove(snap_path)


# ---------- memory ----------

def project_name(cwd):
    """Claude Code's folder name for the project at cwd: its main git checkout (so worktrees share it), else cwd.
    (Claude Code also shortens names over 200 characters; such a project just isn't prepared ahead.)"""
    root = cwd
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}   # the repository at cwd, not an inherited one

    def git(*args):
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5,
                              env=env).stdout.strip()
    try:
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
        if os.path.basename(common) == ".git":
            root = os.path.dirname(common)
        elif common:   # a submodule (.git/modules/<name>) or similar: its own checkout
            root = git("rev-parse", "--show-toplevel") or cwd
    except (OSError, subprocess.SubprocessError):
        pass
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(root))


def link_memory(gdir, own, name, notes):
    src = os.path.join(own, "projects", name, "memory")
    dst = os.path.join(gdir, "projects", name, "memory")
    if points_to(dst, src):
        return
    if inside(os.path.dirname(dst), own):   # gclaude's projects/ linked into <own>: it is already shared
        return
    if os.path.isdir(dst) and not os.path.islink(dst) and not os.listdir(dst):
        os.rmdir(dst)   # Claude Code makes it empty on the first session there
    if os.path.lexists(dst):
        notes.append(f"{dst} already holds gclaude's own memories, so it is not shared with plain claude.")
        return
    os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
    os.symlink(src, dst)


def sync_memory(gdir, own, cwd, notes):
    projects, mine = os.path.join(own, "projects"), os.path.join(gdir, "projects")
    if os.path.isdir(mine) and not inside(mine, own):
        def prune(name):   # links whose project is gone from <own>
            dst = os.path.join(mine, name, "memory")
            if points_to(dst, os.path.join(projects, name, "memory")) and not os.path.exists(dst):
                os.remove(dst)
        each(os.listdir(mine), prune, notes)
    if not os.path.isdir(projects):
        return
    current = project_name(cwd)

    def share(name):
        memory = os.path.join(projects, name, "memory")
        if name == current and os.path.isdir(os.path.join(projects, name)):
            os.makedirs(memory, mode=0o700, exist_ok=True)   # the one change in <own>: a folder to link to
        if os.path.isdir(memory) and not os.path.islink(memory):
            link_memory(gdir, own, name, notes)
    each(sorted(os.listdir(projects)), share, notes)


# ---------- sessions ----------

SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def sync_sessions(gdir, own, notes):
    projects, mine = os.path.join(own, "projects"), os.path.join(gdir, "projects")
    if not os.path.isdir(projects) or inside(mine, own):
        return
    if os.path.isdir(mine):
        def prune(name):   # folder links whose session is gone from <own>
            folder = os.path.join(mine, name)
            for entry in os.listdir(folder) if os.path.isdir(folder) and not os.path.islink(folder) else []:
                dst = os.path.join(folder, entry)
                if SESSION_ID.fullmatch(entry) and points_to(dst, os.path.join(projects, name, entry)) \
                        and not os.path.exists(dst):
                    os.remove(dst)
        each(os.listdir(mine), prune, notes)
    if not os.path.isdir(gdir):
        return
    if os.stat(projects).st_dev != os.stat(gdir).st_dev:   # a hard link can't cross disks
        notes.append(f"{projects} is on another disk than {gdir}, so your sessions are not shared with gclaude.")
        return

    def share(name):
        theirs, folder = os.path.join(projects, name), os.path.join(mine, name)
        if not os.path.isdir(theirs) or os.path.islink(theirs) or inside(folder, own):
            return

        def link(entry):
            stem, ext = os.path.splitext(entry)
            src, dst = os.path.join(theirs, entry), os.path.join(folder, entry)
            if not SESSION_ID.fullmatch(stem) or os.path.islink(src) or os.path.lexists(dst):
                return   # a session gclaude has already, linked or its own
            if ext == ".jsonl" and os.path.isfile(src):
                make = os.link
            elif ext == "" and os.path.isdir(src):
                make = os.symlink
            else:
                return
            os.makedirs(folder, mode=0o700, exist_ok=True)
            make(src, dst)
        each(sorted(os.listdir(theirs)), link, notes)
    each(sorted(os.listdir(projects)), share, notes)


def sync_file_history(gdir, own, notes):
    theirs, mine = os.path.join(own, "file-history"), os.path.join(gdir, "file-history")
    if not os.path.isdir(theirs) or not os.path.isdir(gdir) or inside(mine, own):
        return
    if os.path.isdir(mine):
        def prune(entry):   # links whose session is gone from <own>
            dst = os.path.join(mine, entry)
            if points_to(dst, os.path.join(theirs, entry)) and not os.path.exists(dst):
                os.remove(dst)
        each(os.listdir(mine), prune, notes)

    def link(entry):
        src, dst = os.path.join(theirs, entry), os.path.join(mine, entry)
        if SESSION_ID.fullmatch(entry) and os.path.isdir(src) and not os.path.islink(src) \
                and not os.path.lexists(dst):   # else a session gclaude has already, linked or its own
            os.makedirs(mine, exist_ok=True)
            os.symlink(src, dst)
    each(sorted(os.listdir(theirs)), link, notes)


def unsync_file_history(gdir, own):
    theirs, mine = os.path.join(own, "file-history"), os.path.join(gdir, "file-history")
    if os.path.isdir(mine) and not inside(mine, own):
        for entry in os.listdir(mine):
            if points_to(os.path.join(mine, entry), os.path.join(theirs, entry)):
                os.remove(os.path.join(mine, entry))


def unsync_projects(gdir, own):
    """Remove the memory and session links sync made; gclaude's own sessions and memories stay."""
    projects = os.path.join(gdir, "projects")
    if not os.path.isdir(projects) or inside(projects, own):
        return
    for name in os.listdir(projects):
        folder, theirs = os.path.join(projects, name), os.path.join(own, "projects", name)
        if not os.path.isdir(folder) or os.path.islink(folder):
            continue
        removed = False
        for entry in os.listdir(folder):
            dst, src = os.path.join(folder, entry), os.path.join(theirs, entry)
            hard_link = entry.endswith(".jsonl") and not os.path.islink(dst) and os.path.isfile(src) \
                and os.path.samefile(dst, src)
            if points_to(dst, src) or hard_link:
                os.remove(dst)
                removed = True
        if removed and not os.listdir(folder):
            os.rmdir(folder)   # made by sync just to hold the links


def main():
    mode, gdir, own = sys.argv[1:4]
    if mode == "sync":
        steps = (lambda: sync_commands(gdir, own, notes), lambda: sync_plugins(gdir, own, notes),
                 lambda: sync_mcp(gdir, own, notes), lambda: sync_memory(gdir, own, os.getcwd(), notes),
                 lambda: sync_sessions(gdir, own, notes), lambda: sync_file_history(gdir, own, notes))
    elif mode == "unsync":
        steps = (lambda: unsync_commands(gdir, own), lambda: unsync_plugins(gdir, own),
                 lambda: unsync_mcp(gdir, own), lambda: unsync_projects(gdir, own),
                 lambda: unsync_file_history(gdir, own))
    else:
        sys.exit(__doc__)
    notes = []
    for step in steps:
        try:
            step()
        except Exception as e:   # one step failing never stops the others, gclaude, or `off --gclaude`
            notes.append(f"gclaude-sync: {e}")
    for note in notes:
        print(note, file=sys.stderr)


if __name__ == "__main__":
    main()
