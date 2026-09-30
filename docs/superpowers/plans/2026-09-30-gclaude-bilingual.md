# gclaude in English and Persian: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A gclaude user picks English or Persian at setup (or with `--lang`), switches later with `/language`,
and gets Claude's replies plus gclaude's own text in that language.

**Architecture:** `client.json`'s `lang` is the saved choice. `claude-gateway` writes Claude Code's built-in
`language` setting and two env vars (`CLAUDE_GATEWAY_LANG`, `CLAUDE_GATEWAY_CMD`) into gclaude's settings.json.
All text lives in one catalog, `scripts/i18n.json`, read by a small `t` helper in each script. The existing
UserPromptSubmit hook (`statusline.sh --warn`, `statusline.ps1 --warn`) answers `/language` by running
`claude-gateway lang en|fa`.

**Tech Stack:** POSIX sh and bash, embedded Python 3 (stdlib only), Windows PowerShell 5.1, pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-gclaude-bilingual-design.md`

## Global Constraints

- Languages are exactly `en` and `fa`. A missing or unknown `lang` means `en`.
- Claude Code setting written for Persian: `"language": "persian"` in gclaude's settings.json only. Never touch `~/.claude`.
- Every English string moved into the catalog must stay byte-identical to today's text, so existing tests pass unchanged.
- Placeholders are `{name}` and are filled by plain string replacement (no `str.format`), the same in sh, Python and PowerShell.
- `.ps1` files stay ASCII-only. Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so every Persian string on Windows comes from `i18n.json`, read as UTF-8. Non-ASCII chars go in as `[char]0x....`, as the files already do for `·`.
- `scripts/windows/i18n.json` is a byte-identical copy of `scripts/i18n.json`.
- No test may block on a terminal. The sh question is asked only when stderr is a terminal (`[ -t 2 ]`). Tests pass the answer through `CLAUDE_GATEWAY_TTY` (a file read instead of `/dev/tty`).
- Stay in English: global mode, OpenCode, admin/bug-report errors, `gclaude-sync.py`, the server's figures line, and on Windows `claude-gateway.ps1` output and `gclaude.cmd` messages.
- Match the surrounding style: comments explain why, in the same terse voice. Functions are small, and no new dependencies.
- Work happens in the worktree `/Users/mahmoudparham/projects/claude_proxy-bilingual` on branch `gclaude-bilingual`. Commit only this plan's paths.

## Review Focus

1. **An existing gclaude user runs `gclaude update`.** No `lang` is saved yet. On a terminal they're asked once and the update carries on. Without a terminal it goes on in English. The re-sign-in that gclaude itself starts (`CLAUDE_GATEWAY_FROM_GCLAUDE=1`) never asks. Tests: Task 2 `test_gclaude_is_not_asked_while_it_signs_in_again` and `test_gclaude_without_a_terminal_uses_english_and_asks_next_time`.
2. **The user set their own `language` in gclaude's settings.json (e.g. `japanese`).** `on --lang fa`, `/language` and `off` never overwrite or remove it. Test: Task 2 `test_gclaude_leaves_the_users_own_language_alone`.
3. **`/language` while signed out or offline.** Switching needs no key and no network. Test: Task 3 `test_gclaude_lang_works_signed_out_and_offline`.
4. **Loosely typed answers and arguments.** The setup answer `۲` (Persian digit), `fa`, or `فارسی` means Persian. `/language  FA `, `/language Persian` and `/language فارسی` all switch. `/languages` and `what does /language do?` are left alone. Tests: Task 2 `test_gclaude_takes_a_persian_digit_as_the_answer`, Task 4 `test_language_prompt_understands_each_way_of_naming_persian` and `test_other_prompts_mentioning_language_are_left_alone`.
5. **Persian text in quoting contexts.** Persian text goes into JSON hook output, the sh launcher's quoted strings and YAML front matter, and must survive quoting. Tests: Task 1 `test_launcher_text_is_safe_to_quote_in_sh`, Task 3 `test_gclaude_on_localizes_its_command_descriptions` (parses the YAML value), and Task 4 `test_language_answers_are_valid_json` (a reason with quotes and backslashes).

---

### Task 1: The catalog

**Files:**
- Create: `scripts/i18n.json`
- Create: `scripts/windows/i18n.json` (a copy)
- Create: `tests/test_i18n.py`

**Interfaces:**
- Produces: `scripts/i18n.json`, an object `{key: {"en": str, "fa": str}}` with exactly the keys below. Later tasks look texts up by these key names.

- [ ] **Step 1: Write the failing tests**

`tests/test_i18n.py`:

```python
"""scripts/i18n.json: gclaude's own text in English and Persian (gclaude bilingual design, "Strings")."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "scripts" / "i18n.json"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_every_key_has_english_and_persian():
    for key, texts in catalog().items():
        assert set(texts) == {"en", "fa"}, key
        assert texts["en"].strip() and texts["fa"].strip(), key


def test_persian_uses_the_same_placeholders_as_english():
    for key, texts in catalog().items():
        assert sorted(PLACEHOLDER.findall(texts["fa"])) == sorted(PLACEHOLDER.findall(texts["en"])), key


def test_the_persian_is_persian():
    """Catches an English text copied into fa and never translated."""
    for key, texts in catalog().items():
        assert re.search("[؀-ۿ]", texts["fa"]), key


def test_the_windows_copy_is_the_same():
    assert (ROOT / "scripts" / "windows" / "i18n.json").read_bytes() == CATALOG.read_bytes()


def test_launcher_text_is_safe_to_quote_in_sh():
    """The launcher puts these in single quotes and splices "$why" in: none may carry sh's special characters."""
    for key, texts in catalog().items():
        if key.startswith("launch."):
            for text in texts.values():
                assert not set(text) & set('"$`\\\n'), key
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd /Users/mahmoudparham/projects/claude_proxy-bilingual && uv run pytest tests/test_i18n.py -v`
Expected: FAIL with `FileNotFoundError` for `scripts/i18n.json`.

- [ ] **Step 3: Write the catalog**

`scripts/i18n.json` (UTF-8, no BOM, 2-space indent, trailing newline). `‎` is a left-to-right mark: it keeps a `/command` that follows Persian text reading left to right.

```json
{
  "auth.open": {"en": "To connect this computer, open {link}", "fa": "برای اتصال این رایانه، این پیوند را باز کنید: {link}"},
  "auth.code": {"en": "and check that it shows the code {code}.", "fa": "و بررسی کنید که کد {code} را نشان دهد."},
  "auth.opened": {"en": "It is open in your browser.", "fa": "پیوند در مرورگر شما باز شد."},
  "auth.waiting": {"en": "Waiting for you to authorize it there...", "fa": "در انتظار تأیید شما در آنجا..."},
  "auth.authorized": {"en": "Authorized as {user}.", "fa": "به‌عنوان {user} تأیید شد."},
  "auth.cancelled": {"en": "Cancelled in the browser; nothing was changed.", "fa": "در مرورگر لغو شد؛ چیزی تغییر نکرد."},
  "auth.expired": {"en": "The code expired. Run the command again for a new one.", "fa": "کد منقضی شد. برای کد تازه، فرمان را دوباره اجرا کنید."},

  "on.ready": {"en": "gclaude now runs Claude Code through the gateway at {url}; plain 'claude' is unchanged.", "fa": "اکنون gclaude، ‏Claude Code را از طریق درگاه {url} اجرا می‌کند؛ ‏claude معمولی تغییری نکرده است."},
  "on.next": {"en": "Next: run 'gclaude' instead of 'claude'.", "fa": "گام بعد: به‌جای claude، فرمان gclaude را اجرا کنید."},
  "on.next_path": {"en": "Next: add {bin} to your PATH, then run 'gclaude' instead of 'claude'.", "fa": "گام بعد: {bin} را به PATH اضافه کنید، سپس به‌جای claude، فرمان gclaude را اجرا کنید."},
  "off.removed": {"en": "gclaude is removed. Its history and sessions are still in {dir}; delete that folder to drop them.", "fa": "gclaude حذف شد. تاریخچه و نشست‌هایش هنوز در {dir} هستند؛ برای پاک کردنشان آن پوشه را حذف کنید."},
  "off.foreign": {"en": "gclaude's gateway settings are removed. {path} was not installed by claude-gateway, so it was left as it is.", "fa": "تنظیمات درگاه gclaude حذف شد. {path} را claude-gateway نصب نکرده بود، پس دست‌نخورده ماند."},
  "off.not_set_up": {"en": "gclaude was not set up; nothing to undo.", "fa": "gclaude راه‌اندازی نشده بود؛ چیزی برای برگرداندن نیست."},

  "status.not_set_up": {"en": "gclaude: not set up", "fa": "gclaude: راه‌اندازی نشده"},
  "status.reachable": {"en": "gateway: {url} (reachable)", "fa": "درگاه: {url} (در دسترس)"},
  "status.unreachable": {"en": "gateway: {url} (NOT reachable)", "fa": "درگاه: {url} (در دسترس نیست)"},
  "status.signed_out": {"en": "account: signed out; run gclaude to sign in", "fa": "حساب: خارج شده‌اید؛ برای ورود gclaude را اجرا کنید"},
  "status.account": {"en": "account: {account}", "fa": "حساب: {account}"},
  "status.key_removed": {"en": "account: this computer's key no longer works; run gclaude to sign in again", "fa": "حساب: کلید این رایانه دیگر کار نمی‌کند؛ برای ورود دوباره gclaude را اجرا کنید"},
  "status.account_unavailable": {"en": "account: unavailable (dashboard: {dashboard})", "fa": "حساب: در دسترس نیست (داشبورد: {dashboard})"},
  "status.usage": {"en": "usage: {usage}", "fa": "مصرف: {usage}"},
  "status.dashboard": {"en": "dashboard: {url}", "fa": "داشبورد: {url}"},

  "launch.no_claude": {"en": "gclaude: Claude Code (claude) is not installed or not on PATH", "fa": "gclaude: ‏Claude Code (claude) نصب نیست یا در PATH نیست"},
  "launch.update_failed": {"en": "gclaude: the gateway update failed, so Claude Code was not updated", "fa": "gclaude: به‌روزرسانی درگاه ناموفق بود، پس Claude Code به‌روز نشد"},
  "launch.signed_out": {"en": "signed out (/logout_gclaude)", "fa": "خارج شده‌اید (‎/logout_gclaude)"},
  "launch.key_removed": {"en": "this computer's key no longer works (removed in the dashboard?)", "fa": "کلید این رایانه دیگر کار نمی‌کند (در داشبورد حذف شده؟)"},
  "launch.signing_in": {"en": "gclaude: {why}; signing this computer in again.", "fa": "gclaude: {why}؛ ورود دوباره‌ی این رایانه..."},
  "launch.not_signed_in": {"en": "gclaude: not signed in, so Claude Code was not started", "fa": "gclaude: وارد نشده‌اید، پس Claude Code اجرا نشد"},

  "cmd.usage": {"en": "Your gateway limits and usage", "fa": "محدودیت‌ها و مصرف شما در درگاه"},
  "cmd.account": {"en": "Your gateway account and a link to the dashboard", "fa": "حساب شما در درگاه و پیوند داشبورد"},
  "cmd.logout_gclaude": {"en": "Sign this computer out of the gateway", "fa": "خروج این رایانه از درگاه"},
  "cmd.language": {"en": "Switch gclaude between English and Persian (/language en, /language fa)", "fa": "تغییر زبان gclaude بین انگلیسی و فارسی (‎/language en، ‎/language fa)"},

  "line.unavailable": {"en": "gateway status unavailable", "fa": "وضعیت درگاه در دسترس نیست"},
  "warn.line": {"en": "Gateway: {figures}", "fa": "درگاه: {figures}"},
  "usage.line": {"en": "Gateway: {figures} · details: {dashboard}", "fa": "درگاه: {figures} · جزئیات: {dashboard}"},
  "usage.unavailable": {"en": "Gateway status unavailable; see {dashboard}", "fa": "وضعیت درگاه در دسترس نیست؛ ببینید: {dashboard}"},
  "usage.no_dashboard": {"en": "Gateway status unavailable: CLAUDE_GATEWAY_DASHBOARD is not set (run gclaude update).", "fa": "وضعیت درگاه در دسترس نیست: CLAUDE_GATEWAY_DASHBOARD تنظیم نشده است (gclaude update را اجرا کنید)."},
  "account.line": {"en": "Account: {account} · dashboard: {dashboard}", "fa": "حساب: {account} · داشبورد: {dashboard}"},
  "account.unavailable": {"en": "Account details unavailable; see {dashboard}", "fa": "جزئیات حساب در دسترس نیست؛ ببینید: {dashboard}"},
  "account.no_dashboard": {"en": "Account details unavailable: CLAUDE_GATEWAY_DASHBOARD is not set (run gclaude update).", "fa": "جزئیات حساب در دسترس نیست: CLAUDE_GATEWAY_DASHBOARD تنظیم نشده است (gclaude update را اجرا کنید)."},
  "account.limit": {"en": "{account} · limit reached: {figures}", "fa": "{account} · به سقف رسیده: {figures}"},

  "logout.failed": {"en": "Sign-out failed: the key could not be removed from {settings}. gclaude uninstall removes it.", "fa": "خروج ناموفق بود: کلید از {settings} حذف نشد. ‏gclaude uninstall آن را حذف می‌کند."},
  "logout.again": {"en": "gclaude is closing; run gclaude again to sign in.", "fa": "gclaude بسته می‌شود؛ برای ورود دوباره gclaude را اجرا کنید."},
  "logout.again_windows": {"en": "Exit gclaude now (/exit); run gclaude again to sign in.", "fa": "اکنون از gclaude خارج شوید (‎/exit)؛ برای ورود دوباره gclaude را اجرا کنید."},
  "logout.revoked": {"en": "Signed out: this computer's key is revoked on the gateway and removed from gclaude. {again}", "fa": "خارج شدید: کلید این رایانه در درگاه باطل و از gclaude حذف شد. {again}"},
  "logout.first_key": {"en": "Signed out: the key is removed from gclaude. It is your first key, so it still works wherever else it is set up. {again}", "fa": "خارج شدید: کلید از gclaude حذف شد. این نخستین کلید شماست، پس هر جای دیگری که تنظیم شده همچنان کار می‌کند. {again}"},
  "logout.refused": {"en": "Signed out: the key is removed from gclaude (the gateway no longer accepted it). {again}", "fa": "خارج شدید: کلید از gclaude حذف شد (درگاه دیگر آن را نمی‌پذیرفت). {again}"},
  "logout.offline": {"en": "Signed out here: the key is removed from gclaude, but the gateway could not be reached to revoke it; remove this computer in the dashboard{where}. {again}", "fa": "اینجا خارج شدید: کلید از gclaude حذف شد، اما برای باطل کردنش به درگاه دسترسی نبود؛ این رایانه را در داشبورد حذف کنید{where}. {again}"},
  "logout.http": {"en": "Signed out here: the key is removed from gclaude, but the gateway refused to revoke it (HTTP {code}); remove this computer in the dashboard{where}. {again}", "fa": "اینجا خارج شدید: کلید از gclaude حذف شد، اما درگاه از باطل کردنش سر باز زد (HTTP {code})؛ این رایانه را در داشبورد حذف کنید{where}. {again}"},
  "logout.odd": {"en": "Signed out: the key is removed from gclaude, and the gateway accepted the sign-out (HTTP {code}) but gave an unexpected reply; check in the dashboard that this computer is gone{where}. {again}", "fa": "خارج شدید: کلید از gclaude حذف شد و درگاه خروج را پذیرفت (HTTP {code}) اما پاسخ غیرمنتظره‌ای داد؛ در داشبورد بررسی کنید که این رایانه حذف شده باشد{where}. {again}"},

  "language.current": {"en": "gclaude's language is English. /language fa switches it to Persian (فارسی).", "fa": "زبان gclaude فارسی است. ‎/language en آن را به انگلیسی برمی‌گرداند."},
  "language.switched": {"en": "gclaude now uses English: Claude's replies from your next message, and gclaude's own text.", "fa": "زبان gclaude اکنون فارسی است: پاسخ‌های Claude از پیام بعدی شما، و متن‌های خود gclaude."},
  "language.usage": {"en": "Use /language en or /language fa.", "fa": "از ‎/language en یا ‎/language fa استفاده کنید."},
  "language.failed": {"en": "The language was not changed: {error}. Run gclaude update in a terminal, then try again.", "fa": "زبان تغییر نکرد: {error}. در ترمینال gclaude update را اجرا کنید و دوباره امتحان کنید."}
}
```

`test_the_persian_is_persian` needs at least one Persian letter in every `fa` text. The `launch.*` Persian texts must avoid `"`, `$`, `` ` `` and `\` (the `‎` escape is JSON only; the loaded string holds the character itself). Then copy the file: `cp scripts/i18n.json scripts/windows/i18n.json`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_i18n.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/i18n.json scripts/windows/i18n.json tests/test_i18n.py
git commit -m "gclaude's text in English and Persian: the catalog"
```

---

### Task 2: The language choice (`--lang`, the setup question, settings)

**Files:**
- Modify: `scripts/claude-gateway` (header comment, vars near line 60, new functions after `save_client`, `edit_settings` ~line 185, `gclaude_on` ~line 648, `gclaude_files` `off` branch ~line 636, the `on)` argument loop ~line 720)
- Modify: `install.sh:40-41` (copy `i18n.json`)
- Modify: `tests/test_install.py:56` (the installed file list)
- Test: `tests/test_client_scripts.py` (new section after the gclaude tests)

**Interfaces:**
- Consumes: `scripts/i18n.json` (Task 1)
- Produces:
  - `choose_lang [en|fa]`: prints `en`/`fa`, or nothing when it couldn't ask.
  - `save_lang en|fa`
  - `gclaude_language`: writes the settings from client.json's `lang`.
  - Shell var `CG_LANG`: the language chosen this run, or empty.
  - client.json: `lang` at the top level and `gclaude.added_language`.
  - gclaude settings.json: `env.CLAUDE_GATEWAY_LANG` (`en`|`fa`), `env.CLAUDE_GATEWAY_CMD` (absolute path of this claude-gateway), and `language` (`persian`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_client_scripts.py`:

```python
# ---------- gclaude in English or Persian (gclaude bilingual design) ----------

CATALOG = json.loads((ROOT / "scripts" / "i18n.json").read_text(encoding="utf-8"))


def cg_tty(home, answer, *args, **env):
    """claude-gateway with a terminal on stderr, as under `curl ... | sh`; its question's answer comes from the file
    CLAUDE_GATEWAY_TTY names. Returns (result, what went to the terminal)."""
    import pty
    tty = home / "tty-answer"
    tty.write_text(answer, encoding="utf-8")
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    master, slave = pty.openpty()
    try:
        r = subprocess.run(["bash", str(GATEWAY), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=slave,
                           text=True, timeout=60, env={"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(tmp),
                                                       "CLAUDE_GATEWAY_TTY": str(tty), **env})
    finally:
        os.close(slave)
    os.set_blocking(master, False)
    shown = b""
    try:
        while chunk := os.read(master, 65536):
            shown += chunk
    except OSError:   # BlockingIOError once drained; EIO on Linux once the other end is closed
        pass
    os.close(master)
    return r, shown.decode("utf-8", "replace")


def client_json(home):
    return json.loads((home / ".config" / "claude-gateway" / "client.json").read_text())


def test_gclaude_asks_for_its_language_on_a_terminal_and_keeps_it(stub, home):
    r, shown = cg_tty(home, "2\n", "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 0, shown
    assert "Language / زبان" in shown
    _, gsettings, _ = gc_paths(home)
    s = json.loads(gsettings.read_text())
    assert client_json(home)["lang"] == "fa"
    assert s["language"] == "persian" and s["env"]["CLAUDE_GATEWAY_LANG"] == "fa"
    r, shown = cg_tty(home, "1\n", "on")   # later runs keep the choice and never ask
    assert r.returncode == 0 and "Language / زبان" not in shown
    assert client_json(home)["lang"] == "fa"


@pytest.mark.parametrize("answer, lang", [("۲\n", "fa"), ("fa\n", "fa"), ("فارسی\n", "fa"), ("\n", "en"), ("x\n", "en")])
def test_gclaude_takes_a_persian_digit_as_the_answer(stub, home, answer, lang):
    r, shown = cg_tty(home, answer, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 0, shown
    assert client_json(home)["lang"] == lang


def test_gclaude_lang_flag_skips_the_question(stub, home):
    r, shown = cg_tty(home, "1\n", "on", "--gclaude", "--lang", "fa", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 0 and "Language / زبان" not in shown
    assert client_json(home)["lang"] == "fa"


def test_gclaude_without_a_terminal_uses_english_and_asks_next_time(stub, home):
    r = cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full")
    assert r.returncode == 0, r.stderr
    assert "/language fa" in r.stderr
    assert "lang" not in client_json(home)                     # not saved: the next `on` on a terminal asks
    _, gsettings, _ = gc_paths(home)
    s = json.loads(gsettings.read_text())
    assert "language" not in s and s["env"]["CLAUDE_GATEWAY_LANG"] == "en"
    r, shown = cg_tty(home, "2\n", "on")
    assert "Language / زبان" in shown and client_json(home)["lang"] == "fa"


def test_gclaude_is_not_asked_while_it_signs_in_again(stub, home):
    r, shown = cg_tty(home, "2\n", "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full",
                      CLAUDE_GATEWAY_FROM_GCLAUDE="1")
    assert r.returncode == 0 and "Language / زبان" not in shown
    assert "lang" not in client_json(home)


def test_gclaude_settings_name_this_claude_gateway_for_the_language_hook(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, gsettings, _ = gc_paths(home)
    cmd = json.loads(gsettings.read_text())["env"]["CLAUDE_GATEWAY_CMD"]
    assert os.path.isabs(cmd) and os.path.realpath(cmd) == str(GATEWAY.resolve())


@pytest.mark.parametrize("args", [["--gclaude", "--lang", "de"], ["--global", "--lang", "fa"],
                                  ["--opencode", "--lang", "fa", "--routes-key", "sk-proxy-r-k"]])
def test_lang_is_en_or_fa_and_goes_with_gclaude(stub, home, args):
    r = cg(home, "on", *args, "--url", stub.url, *(["--key", "sk-proxy-full"] if "--opencode" not in args else []))
    assert r.returncode == 1 and "--lang" in r.stderr


def test_gclaude_leaves_the_users_own_language_alone(stub, home):
    gdir, gsettings, _ = gc_paths(home)
    gdir.mkdir(parents=True)
    gsettings.write_text(json.dumps({"language": "japanese"}))
    assert cg(home, "on", "--gclaude", "--lang", "fa", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert json.loads(gsettings.read_text())["language"] == "japanese"
    assert cg(home, "off", "--gclaude").returncode == 0
    assert json.loads(gsettings.read_text())["language"] == "japanese"


def test_gclaude_off_removes_the_language_it_set(stub, home):
    assert cg(home, "on", "--gclaude", "--lang", "fa", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    assert cg(home, "off", "--gclaude").returncode == 0
    _, gsettings, _ = gc_paths(home)
    s = json.loads(gsettings.read_text())
    assert "language" not in s and "CLAUDE_GATEWAY_LANG" not in s.get("env", {}) and "CLAUDE_GATEWAY_CMD" not in s.get("env", {})
    assert "lang" not in client_json(home)
```

In `tests/test_install.py:56`, add `"scripts/i18n.json"` to the tuple of files checked in the share dir:

```python
    for f in ("scripts/claude-gateway", "scripts/statusline.sh", "scripts/gclaude-sync.py", "scripts/i18n.json", "examples/opencode/muse.md"):
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_client_scripts.py -k "language or lang or asks or asked" tests/test_install.py -v`
Expected: FAIL (`--lang` is an unknown option, so `usage 1` exits 1; no `lang` in client.json; `i18n.json` is not installed).

- [ ] **Step 3: Implement**

`install.sh` lines 40-41: copy and chmod the catalog with the other scripts:

```sh
cp "$tmp/scripts/claude-gateway" "$tmp/scripts/statusline.sh" "$tmp/scripts/gclaude-sync.py" "$tmp/scripts/i18n.json" "$share.new/scripts/"
```

(chmod stays as it is: `i18n.json` is data, not a script.) Also add `, i18n.json` to the file list in the header comment on line 7: `statusline.sh, gclaude-sync.py, i18n.json, examples/opencode`.

`scripts/claude-gateway`, after `GCLAUDE_SYNC=...` (line 63):

```bash
CG_LANG=   # the language `on` chose this run (en or fa), or empty: client.json's lang, else English
```

After `save_client` (after line 79):

```bash
save_lang() {   # en|fa: gclaude's language, kept in client.json (readable by this user alone, as it holds the key)
  mkdir -p "$(dirname "$CLIENT")"
  ( umask 077; python3 - "$CLIENT" "$1" <<'EOF'
import json, os, sys
path, lang = sys.argv[1:]
c = json.load(open(path)) if os.path.exists(path) else {}
c["lang"] = lang
json.dump(c, open(path, "w"), indent=2)
EOF
  )
}

choose_lang() {   # [en|fa]: the language given, else the saved one, else asked once on the terminal. Prints nothing
  # when it can't ask (no terminal, or gclaude signing in again): English then, unsaved, so the next `on` asks.
  if [ -n "$1" ]; then echo "$1"; return; fi
  case "$(field lang)" in en|fa) field lang; return ;; esac
  [ -z "${CLAUDE_GATEWAY_FROM_GCLAUDE:-}" ] || return 0
  local answer=
  # stdin may be the installer itself (curl ... | sh), so the answer comes from the terminal.
  if [ -t 2 ] && { exec 3<"${CLAUDE_GATEWAY_TTY:-/dev/tty}"; } 2>/dev/null; then
    printf 'Language / زبان:  1) English  2) فارسی  [1] ' >&2
    IFS= read -r answer <&3 || true
    exec 3<&-
    case "$(printf '%s' "$answer" | tr -d '[:space:]')" in 2|۲|fa|FA|فارسی) echo fa ;; *) echo en ;; esac
  else
    echo "No terminal to ask on, so gclaude uses English; /language fa switches it to Persian (فارسی)." >&2
  fi
}

gclaude_language() {   # client.json's lang into gclaude's settings.json: Claude's replies, and what the hook needs
  python3 - "$GCLAUDE_DIR/settings.json" "$CLIENT" "$(src_dir)/$(basename "${BASH_SOURCE[0]}")" <<'EOF'
import json, os, sys, tempfile
settings, client, self_path = sys.argv[1:]
c = json.load(open(client))
rec = c["gclaude"] = c["gclaude"] if isinstance(c.get("gclaude"), dict) else {}
lang = c.get("lang") if c.get("lang") in ("en", "fa") else "en"
s = json.load(open(settings))
env = s.setdefault("env", {})
env["CLAUDE_GATEWAY_LANG"] = lang        # statusline.sh and its hook speak it
env["CLAUDE_GATEWAY_CMD"] = self_path    # /language runs `claude-gateway lang`
if rec.pop("added_language", False) and s.get("language") == "persian":
    del s["language"]
if lang == "fa" and "language" not in s:   # a language the user set in gclaude's settings stays theirs
    s["language"] = "persian"            # Claude Code's own setting: "Always respond in persian..."
    rec["added_language"] = True
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(settings))
with os.fdopen(fd, "w") as f:
    json.dump(s, f, indent=2)
    f.write("\n")
os.chmod(tmp, os.stat(settings).st_mode & 0o777)
os.replace(tmp, settings)
json.dump(c, open(client, "w"), indent=2)
EOF
}
```

`edit_settings`: in the Python, extend the popped env keys (line ~184) and add the language removal right after the `added_disable_connectors` line. `on` pops them before `gclaude_language` writes them again, and `off` removes them for good:

```python
for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_GATEWAY_DASHBOARD", "CLAUDE_GATEWAY_LANG", "CLAUDE_GATEWAY_CMD"):
    env.pop(k, None)
```

```python
if st.pop("added_language", False) and s.get("language") == "persian":   # gclaude_language's; a user's own stays
    s.pop("language")
```

`gclaude_on`: save the choice before anything is written, and set the language after `edit_settings`:

```bash
gclaude_on() {
  gclaude_free
  [ -n "$(field key)" ] || { echo "No gateway configured yet: claude-gateway on --gclaude --url <url> --key <key>" >&2; exit 1; }
  preflight key-only
  [ -z "$CG_LANG" ] || save_lang "$CG_LANG"
  install_statusline
  ...
  edit_settings on "$GCLAUDE_DIR/settings.json" gclaude
  gclaude_language
  chmod 600 ...
```

`gclaude_files` `off` branch (line ~636): `c.pop("gclaude", None)` becomes

```python
    c.pop("gclaude", None)
    c.pop("lang", None)   # `on` saved it for gclaude
```

`install_statusline` (line ~155): the statusline reads the catalog beside itself, so copy it too:

```bash
install_statusline() {   # copy statusline.sh, and the i18n.json it reads, from beside this script next to client.json
  local src
  src=$(src_dir)/statusline.sh
  if [ -f "$src" ]; then cp "$src" "$STATUSLINE" && chmod 755 "$STATUSLINE"; fi
  if [ -f "$(src_dir)/i18n.json" ]; then cp "$(src_dir)/i18n.json" "$(dirname "$STATUSLINE")/i18n.json"; fi
}
```

The `on)` argument loop: add `lang=""` to the local var list, a case arm, and the checks.

```bash
    url="" key="" dash="" mode="" opencode="" rkey="" gclaude="" global="" login="" lang=""
    ...
        --lang) lang=$2; shift 2 ;;
    ...
    case "$lang" in ""|en|fa) ;; *) echo "--lang is en (English) or fa (Persian)." >&2; exit 1 ;; esac
    if [ -n "$opencode" ]; then
      [ -z "$key$dash$mode$gclaude$global$login$lang" ] || { echo "--opencode takes only --url and --routes-key; ..." >&2; exit 1; }
```

The `--opencode` message stays as it is. The test checks that stderr mentions `--lang`, so add ` (--lang is for gclaude)` to the end of that message's text.

After the "gclaude is the default" block (after line ~120):

```bash
    [ -z "$lang" ] || [ -n "$gclaude" ] || { echo "--lang goes with gclaude: gclaude's language (global mode is English)." >&2; exit 1; }
    [ -z "$gclaude" ] || CG_LANG=$(choose_lang "$lang")   # before signing in, so that speaks it too
```

Header comment: add under the gclaude lines (line ~19):

```
#   claude-gateway on --lang en|fa   # gclaude's language: Claude's replies and gclaude's own text (asked the first time)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_client_scripts.py tests/test_install.py tests/test_device_cli.py -q`
Expected: all pass. The existing tests don't pass `--lang` and have no terminal, so they get English and nothing saved, as before.

- [ ] **Step 5: Commit**

```bash
git add scripts/claude-gateway install.sh tests/test_client_scripts.py tests/test_install.py
git commit -m "gclaude asks for its language (English or Persian) and sets Claude Code's language setting"
```

---

### Task 3: `claude-gateway lang`, /language's command file, and localized command descriptions and launcher

**Files:**
- Modify: `scripts/claude-gateway` (new `t` helper after `field`, `gclaude_files` ~line 485-630, new `lang)` case in the command switch, header comment)
- Test: `tests/test_client_scripts.py`

**Interfaces:**
- Consumes: `save_lang`, `gclaude_language`, `CG_LANG` (Task 2); catalog keys `cmd.*`, `launch.*` (Task 1)
- Produces:
  - `t key [name=value...]` in claude-gateway: prints the text in `${CG_LANG:-client.json's lang}`.
  - `claude-gateway lang en|fa`: exit 0 on success; exit 1 with a one-line stderr reason otherwise.
  - gclaude's `commands/language.md`.

- [ ] **Step 1: Write the failing tests**

Change the expected command list in `test_gclaude_on_sets_up_its_own_dir_and_leaves_claude_code_alone` (line 820):

```python
    assert sorted(p.name for p in (gdir / "commands").iterdir()) == ["account.md", "language.md", "logout_gclaude.md", "usage.md"]   # gclaude's own only
```

Append:

```python
def description(path):
    line = next(l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("description: "))
    return json.loads(line.removeprefix("description: "))   # a JSON string is a YAML one too


def test_gclaude_on_localizes_its_command_descriptions(stub, home):
    assert cg(home, "on", "--gclaude", "--lang", "fa", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, _, _ = gc_paths(home)
    for name in ("usage", "account", "logout_gclaude", "language"):
        md = gdir / "commands" / f"{name}.md"
        assert description(md) == CATALOG[f"cmd.{name}"]["fa"]
        assert "<!-- # Installed by claude-gateway on --gclaude. -->" in md.read_text(encoding="utf-8")


def test_gclaude_lang_switches_everything(stub, home):
    assert cg(home, "on", "--gclaude", "--lang", "en", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    gdir, gsettings, launcher = gc_paths(home)
    r = cg(home, "lang", "fa")
    assert r.returncode == 0, r.stderr
    s = json.loads(gsettings.read_text())
    assert (client_json(home)["lang"], s["language"], s["env"]["CLAUDE_GATEWAY_LANG"]) == ("fa", "persian", "fa")
    assert description(gdir / "commands" / "usage.md") == CATALOG["cmd.usage"]["fa"]
    assert CATALOG["launch.no_claude"]["fa"] in launcher.read_text(encoding="utf-8")
    assert cg(home, "lang", "en").returncode == 0
    s = json.loads(gsettings.read_text())
    assert "language" not in s and s["env"]["CLAUDE_GATEWAY_LANG"] == "en"
    assert description(gdir / "commands" / "usage.md") == "Your gateway limits and usage"
    assert CATALOG["launch.no_claude"]["en"] in launcher.read_text(encoding="utf-8")


def test_gclaude_lang_works_signed_out_and_offline(stub, home):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, gsettings, _ = gc_paths(home)
    s = json.loads(gsettings.read_text())
    del s["env"]["ANTHROPIC_AUTH_TOKEN"]                    # as /logout_gclaude leaves it
    gsettings.write_text(json.dumps(s))
    c = client_json(home)
    del c["key"]
    (home / ".config" / "claude-gateway" / "client.json").write_text(json.dumps(c))
    stub.server.shutdown()                                 # and no gateway either
    r = cg(home, "lang", "fa")
    assert r.returncode == 0, r.stderr
    assert json.loads(gsettings.read_text())["language"] == "persian"


def test_gclaude_launcher_speaks_persian(stub, home):
    assert cg(home, "on", "--gclaude", "--lang", "fa", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    _, _, launcher = gc_paths(home)
    r = run([str(launcher)], {"PATH": "/usr/bin:/bin", "HOME": str(home)})   # no claude on this PATH
    assert r.returncode == 127 and CATALOG["launch.no_claude"]["fa"] in r.stderr


@pytest.mark.parametrize("args, why", [(["de"], "en|fa"), ([], "en|fa"), (["fa", "x"], "en|fa")])
def test_gclaude_lang_takes_en_or_fa(stub, home, args, why):
    assert cg(home, "on", "--gclaude", "--url", stub.url, "--key", "sk-proxy-full").returncode == 0
    r = cg(home, "lang", *args)
    assert r.returncode == 1 and why in r.stderr


def test_gclaude_lang_needs_gclaude(home):
    r = cg(home, "lang", "fa")
    assert r.returncode == 1 and "not set up" in r.stderr
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_client_scripts.py -k "localizes or lang_ or launcher_speaks or sets_up_its_own_dir" -v`
Expected: FAIL (no `language.md`, the descriptions are plain English, `lang` is an unknown command).

- [ ] **Step 3: Implement**

`t` helper after `field` (line ~140):

```bash
t() {   # key [name=value...]: gclaude's text in its language (this run's choice, else client.json's lang) from i18n.json
  python3 - "$(src_dir)/i18n.json" "${CG_LANG:-$(field lang en)}" "$@" <<'EOF'
import json, sys
path, lang, key, *pairs = sys.argv[1:]
texts = json.load(open(path, encoding="utf-8"))[key]
text = texts.get(lang) or texts["en"]
for p in pairs:
    k, _, v = p.partition("=")
    text = text.replace("{" + k + "}", v)
print(text)
EOF
}
```

`gclaude_files`: pass the catalog and language in the environment (the argv list stays as it is):

```bash
  CG_I18N="$(src_dir)/i18n.json" CG_LANG="${CG_LANG:-$(field lang en)}" python3 - "$1" "$CLIENT" ... <<'EOF'
import atexit, json, os, shlex, sys, tempfile
mode, client, gdir, launcher, mark, shared, own_state, sync, statusline, self_path = sys.argv[1:]
CATALOG = json.load(open(os.environ["CG_I18N"], encoding="utf-8"))
def t(key, **a):   # as claude-gateway's own t
    text = CATALOG[key].get(os.environ.get("CG_LANG") or "en") or CATALOG[key]["en"]
    for k, v in a.items():
        text = text.replace("{" + k + "}", v)
    return text
desc = lambda name: json.dumps(t("cmd." + name), ensure_ascii=False)   # a JSON string is valid YAML, Persian or not
```

In `COMMANDS`, change each `description: <English>` line to `description: {desc("usage")}`, `{desc("account")}` and `{desc("logout_gclaude")}`. The bodies stay as they are. Add the fourth command after `logout_gclaude`:

```python
""", "language": f"""---
description: {desc("language")}
argument-hint: en | fa
disable-model-invocation: true
---
<!-- {mark} -->
The claude-gateway hook that answers /language did not run, so the language was not changed. Tell the user, in one
sentence, that running `gclaude update` in a terminal reinstalls the hook. Use no tools.
"""}
```

Extend the comment above `COMMANDS`:

```python
# /language switches gclaude between English and Persian: the hook answers it by running `claude-gateway lang`.
```

Launcher lines. Each literal message becomes the catalog's text, shell-quoted:

```python
signing_pre, signing_post = t("launch.signing_in", why="\0").split("\0")
...
                f"    script=$(curl -fsSL {shlex.quote(dash + '/install')}) && printf '%s\\n' \"$script\" | sh ||\n"
                f"      {{ echo {shlex.quote(t('launch.update_failed'))} >&2; exit 1; }}\n"
...
                f"command -v claude >/dev/null 2>&1 || {{ echo {shlex.quote(t('launch.no_claude'))} >&2; exit 127; }}\n"
...
                f"  why={shlex.quote(t('launch.signed_out'))}\n"
...
                f"  why={shlex.quote(t('launch.key_removed'))}\n"
...
                f"  echo {shlex.quote(signing_pre)}\"$why\"{shlex.quote(signing_post)} >&2\n"
                '  CLAUDE_GATEWAY_FROM_GCLAUDE=1 "$gw" on --gclaude --login ||'
                f" {{ echo {shlex.quote(t('launch.not_signed_in'))} >&2; exit 1; }}\n"
```

The English launcher output stays byte-identical: `shlex.quote("gclaude: ")` + `"$why"` + `shlex.quote("; signing ...")` prints the same text as before.

New command in the main `case "$cmd"` switch, before `off)`:

```bash
  lang)   # gclaude's /language runs this (statusline.sh --warn). No network and no key, so it works signed out too.
    case "${1:-}" in en|fa) ;; *) echo "Usage: claude-gateway lang en|fa" >&2; exit 1 ;; esac
    [ $# -eq 1 ] || { echo "Usage: claude-gateway lang en|fa" >&2; exit 1; }
    gclaude_ours || { echo "gclaude is not set up, so it has no language to change." >&2; exit 1; }
    CG_LANG=$1
    save_lang "$1"
    gclaude_language
    gclaude_files on
    ;;
```

Header comment, beside `--lang`:

```
#   claude-gateway lang en|fa        # switch gclaude's language; its /language runs this
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_client_scripts.py tests/test_device_cli.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/claude-gateway tests/test_client_scripts.py
git commit -m "claude-gateway lang, gclaude's /language command, and its commands and launcher in its language"
```

---

### Task 4: statusline.sh speaks the language and answers /language

**Files:**
- Modify: `scripts/statusline.sh` (header comment, vars line 29, `ours` calls ~line 41, new `t` after `json_str`, each user-facing message, new `/language` block after the logout block)
- Test: `tests/test_client_scripts.py`

**Interfaces:**
- Consumes: env `CLAUDE_GATEWAY_LANG`, `CLAUDE_GATEWAY_CMD` (Task 2); `claude-gateway lang` (Task 3); catalog (Task 1)
- Produces: the hook's answers to `/language`, `/language <x>`.

- [ ] **Step 1: Write the failing tests**

Append:

```python
# ---------- statusline.sh in Persian, and gclaude's /language ----------

def fake_gateway(tmp_path, fails=False):
    """A stand-in for claude-gateway that records its arguments."""
    log = tmp_path / "gw.log"
    path = tmp_path / "fake-claude-gateway"
    path.write_text(f'#!/bin/sh\necho "$@" >> {log}\n' + ('echo "Usage: \\"boom\\" \\\\ here" >&2\nexit 1\n' if fails else ""))
    path.chmod(0o755)
    return path, log


def language_prompt(env, prompt, gw, text="<!-- # Installed by claude-gateway on --gclaude. -->\n"):
    env = gclaude_config(env)
    (Path(env["CLAUDE_CONFIG_DIR"]) / "commands" / "language.md").write_text(text)
    return run(["sh", str(STATUSLINE), "--warn"], {**env, "CLAUDE_GATEWAY_CMD": str(gw)}, stdin=json.dumps({"prompt": prompt}))


def test_statusline_speaks_persian_when_gclaude_does(stub, warn_env):
    fa = {**warn_env, "CLAUDE_GATEWAY_LANG": "fa"}
    stub.status_line = None
    assert CATALOG["line.unavailable"]["fa"] in run(["sh", str(STATUSLINE)], fa, stdin="{}").stdout
    stub.status_line = "alice · daily 10/100 req"
    out = json.loads(usage_prompt(fa).stdout)
    assert out["reason"] == f"درگاه: alice · daily 10/100 req 10% · جزئیات: {stub.url}/dashboard"


def test_language_prompt_alone_says_the_current_language(stub, warn_env, tmp_path):
    gw, log = fake_gateway(tmp_path)
    out = json.loads(language_prompt(warn_env, "/language", gw).stdout)
    assert out == {"decision": "block", "reason": CATALOG["language.current"]["en"]}
    out = json.loads(language_prompt({**warn_env, "CLAUDE_GATEWAY_LANG": "fa"}, "/language", gw).stdout)
    assert out["reason"] == CATALOG["language.current"]["fa"]
    assert not log.exists()


@pytest.mark.parametrize("prompt, lang", [("/language fa", "fa"), ("/language  FA ", "fa"), ("/language Persian", "fa"),
                                          ("/language فارسی", "fa"), ("/language en", "en"), ("/language English", "en")])
def test_language_prompt_understands_each_way_of_naming_persian(stub, warn_env, tmp_path, prompt, lang):
    gw, log = fake_gateway(tmp_path)
    out = json.loads(language_prompt(warn_env, prompt, gw).stdout)
    assert log.read_text() == f"lang {lang}\n"
    assert out == {"decision": "block", "reason": CATALOG["language.switched"][lang]}   # in the new language


def test_language_prompt_with_another_language_shows_how(stub, warn_env, tmp_path):
    gw, log = fake_gateway(tmp_path)
    out = json.loads(language_prompt(warn_env, "/language de", gw).stdout)
    assert out["reason"] == CATALOG["language.usage"]["en"] and not log.exists()


def test_language_answers_are_valid_json(stub, warn_env, tmp_path):
    gw, _ = fake_gateway(tmp_path, fails=True)
    out = json.loads(language_prompt(warn_env, "/language fa", gw).stdout)
    assert out["reason"] == CATALOG["language.failed"]["en"].replace("{error}", 'Usage: "boom" \\ here')


@pytest.mark.parametrize("prompt, text", [("/languages", None), ("what does /language do?", None),
                                          ("/language fa", "my own language command\n")])
def test_other_prompts_mentioning_language_are_left_alone(stub, warn_env, tmp_path, prompt, text):
    stub.status_line = "alice · daily 10/100 req"
    gw, log = fake_gateway(tmp_path)
    r = language_prompt(warn_env, prompt, gw, **({"text": text} if text else {}))
    assert (r.returncode, r.stdout) == (0, "") and not log.exists()
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_client_scripts.py -k "persian or language_" -v`
Expected: FAIL (English text, and `/language` isn't answered).

- [ ] **Step 3: Implement**

Header comment, after the /logout_gclaude paragraph:

```
# It answers gclaude's /language too (commands/language.md there): alone it says which language gclaude speaks;
# with en or fa (English, Persian, فارسی) it runs `claude-gateway lang` (CLAUDE_GATEWAY_CMD) to switch. What this
# script shows is in CLAUDE_GATEWAY_LANG (gclaude's settings.json sets it; English without it), from i18n.json beside it.
```

Line 29: `warn= usage= account= account_prompt= logout= language= then=`. After `ours logout_gclaude && logout=1`: `ours language && language=1`.

After `stop()`:

```sh
t() {   # key [name=value...]: the text for key in CLAUDE_GATEWAY_LANG, from i18n.json beside this script. Only for text
        # actually shown, so a statusline with figures to show makes no call.
  python3 - "$(dirname "$0")/i18n.json" "${CLAUDE_GATEWAY_LANG:-en}" "$@" <<'EOF'
import json, sys
path, lang, key, *pairs = sys.argv[1:]
texts = json.load(open(path, encoding="utf-8"))[key]
text = texts.get(lang) or texts["en"]
for p in pairs:
    k, _, v = p.partition("=")
    text = text.replace("{" + k + "}", v)
print(text)
EOF
}
```

Replace each message with its key. `$dash` below stands for `${CLAUDE_GATEWAY_DASHBOARD%/}/dashboard`, as each line already writes it:

| line (today) | becomes |
|---|---|
| `stop "Sign-out failed: ... $CLAUDE_CONFIG_DIR/settings.json. ..."` | `stop "$(t logout.failed settings="$CLAUDE_CONFIG_DIR/settings.json")"` |
| `again="gclaude is closing; ..."` | `again=$(t logout.again)` |
| the four `stop "Signed out..."` | `stop "$(t logout.revoked again="$again")"`, `logout.first_key`, `logout.refused`, and `stop "$(t logout.offline where="${CLAUDE_GATEWAY_DASHBOARD:+ (${CLAUDE_GATEWAY_DASHBOARD%/}/dashboard)}" again="$again")"` |
| `echo "Account details unavailable: CLAUDE_GATEWAY_DASHBOARD is not set ..."` | `t account.no_dashboard` |
| `block "Gateway status unavailable: CLAUDE_GATEWAY_DASHBOARD is not set ..."` | `block "$(t usage.no_dashboard)"` |
| `echo "Account: $line · dashboard: ..."` in `account_line` | `t account.line account="$line" dashboard="$dash"` |
| `echo "Account details unavailable; see ..."` | `t account.unavailable dashboard="$dash"` |
| `show "${line:-gateway status unavailable}"` | `show "${line:-$(t line.unavailable)}"` |
| `block "Gateway status unavailable; see ..."` | `block "$(t usage.unavailable dashboard="$dash")"` |
| `block "Gateway: $(figures "$line") · details: ..."` | `block "$(t usage.line figures="$(figures "$line")" dashboard="$dash")"` |
| `block "$(account_line) · limit reached: $(figures "$line")"` | `block "$(t account.limit account="$(account_line)" figures="$(figures "$line")")"` |
| `json_str "Gateway: $(figures "$line")"` | `json_str "$(t warn.line figures="$(figures "$line")")"` |

`statusline.sh: set CLAUDE_GATEWAY_DASHBOARD` stays English (it's for an admin).

The `/language` block goes right after the logout block's closing `fi`, before the `CLAUDE_GATEWAY_DASHBOARD` check (switching needs no dashboard):

```sh
if [ -n "$language" ]; then   # gclaude's /language [en|fa]
  arg=$(printf '%s' "$input" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("prompt", "")[len("/language"):].strip().lower())' 2>/dev/null)
  case "$arg" in
    "") block "$(t language.current)" ;;
    en|english|انگلیسی) new=en ;;
    fa|persian|farsi|فارسی) new=fa ;;
    *) block "$(t language.usage)" ;;
  esac
  if err=$("${CLAUDE_GATEWAY_CMD:-claude-gateway}" lang "$new" 2>&1 >/dev/null); then
    block "$(CLAUDE_GATEWAY_LANG=$new t language.switched)"   # in the language just chosen
  fi
  block "$(t language.failed error="$(printf '%s\n' "$err" | tail -n 1)")"
fi
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_client_scripts.py -q`
Expected: all pass. The existing English tests are unchanged, which shows the English texts are byte-identical.

- [ ] **Step 5: Commit**

```bash
git add scripts/statusline.sh tests/test_client_scripts.py
git commit -m "statusline and its hook in gclaude's language; the hook answers /language"
```

---

### Task 5: claude-gateway's gclaude messages in the chosen language

**Files:**
- Modify: `scripts/claude-gateway` (`authorize` ~line 80-137, `gclaude_on` end, `gclaude_off`, `gclaude_status`)
- Test: `tests/test_client_scripts.py`, `tests/test_device_cli.py`

**Interfaces:**
- Consumes: `t`, `CG_LANG` (Tasks 2-3); catalog `auth.*`, `on.*`, `off.*`, `status.*`

- [ ] **Step 1: Write the failing tests**

`tests/test_client_scripts.py`:

```python
def test_gclaude_on_status_and_off_speak_persian(stub, home):
    r = cg(home, "on", "--gclaude", "--lang", "fa", "--url", stub.url, "--key", "sk-proxy-full")
    assert CATALOG["on.ready"]["fa"].replace("{url}", stub.url) in r.stdout
    r = cg(home, "status", "--gclaude")
    assert CATALOG["status.reachable"]["fa"].replace("{url}", stub.url) in r.stdout
    assert CATALOG["status.dashboard"]["fa"].replace("{url}", f"{stub.url}/dashboard") in r.stdout
    gdir, _, _ = gc_paths(home)
    r = cg(home, "off", "--gclaude")
    assert CATALOG["off.removed"]["fa"].replace("{dir}", str(gdir)) in r.stdout
```

`tests/test_device_cli.py`, after `test_on_without_a_key_authorizes_in_the_browser`:

```python
def test_on_authorizes_in_persian_when_gclaude_is_persian(dash, home, tmp_path):
    dash.tokens = [(200, {"key": "sk-proxy-machine", "user": "maya"})]
    path, _ = opener(tmp_path)
    r = cg(home, "on", "--lang", "fa", "--url", dash.url, "--dashboard", dash.url, CLAUDE_GATEWAY_OPEN=str(path))
    assert r.returncode == 0, r.stderr
    assert f"این پیوند را باز کنید: {dash.url}/dashboard#authorize/{CODE}" in r.stderr
    assert "به‌عنوان maya تأیید شد." in r.stderr
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_client_scripts.py -k speak_persian tests/test_device_cli.py -k persian -v`
Expected: FAIL (English output).

- [ ] **Step 3: Implement**

`authorize`: pass the catalog and language, and look each message up:

```bash
authorize() {   # url dashboard: prints the key the dashboard hands this computer once someone authorizes it there
  CG_I18N="$(src_dir)/i18n.json" CG_LANG="${CG_LANG:-$(field lang en)}" python3 - "$@" <<'EOF'
import json, os, shutil, socket, subprocess, sys, time
url, dash = sys.argv[1], sys.argv[2]
say = lambda m: print(m, file=sys.stderr, flush=True)
CATALOG = json.load(open(os.environ["CG_I18N"], encoding="utf-8"))
def t(key, **a):   # as claude-gateway's own t
    text = CATALOG[key].get(os.environ.get("CG_LANG") or "en") or CATALOG[key]["en"]
    for k, v in a.items():
        text = text.replace("{" + k + "}", v)
    return text
```

Then change each message:
- `say(t("auth.open", link=link))`
- `say(t("auth.code", code=s["user_code"]))`
- `say(t("auth.opened"))`
- `say(t("auth.waiting"))`
- `say(t("auth.authorized", user=r.get("user", "?")))`
- in the `sys.exit({...})` map: `"access_denied": t("auth.cancelled"), "expired_token": t("auth.expired")`
- the final `sys.exit(t("auth.expired"))`

The "Can't reach the dashboard" and "can't authorize" errors stay English (they're for an admin).

`gclaude_on` end:

```bash
  t on.ready url="$(field url)"
  ...
    *":$(dirname "$GCLAUDE"):"*) echo "${b}$(t on.next)${r}" ;;
    *) echo "${b}$(t on.next_path bin="$(dirname "$GCLAUDE")")${r}" ;;
```

`gclaude_off`. `gclaude_files off` drops `lang`, so fix the language first:

```bash
gclaude_off() {
  CG_LANG=$(field lang en)   # `off` removes it below; its messages still speak it
  if ! gclaude_ours && [ ! -f "$GCLAUDE_DIR/settings.json" ]; then t off.not_set_up; return; fi
  ...
    t off.removed dir="$GCLAUDE_DIR"
  else
    t off.foreign path="$GCLAUDE"
```

`gclaude_status`: each `echo` becomes its key:
- `t status.not_set_up`
- `t status.reachable url="$url"` / `t status.unreachable url="$url"`
- `t status.signed_out`
- `t status.account account="$body"`
- `t status.key_removed`
- `t status.account_unavailable dashboard="$dash"`
- `t status.usage usage="$body"`
- `t status.dashboard url="$dash/dashboard"`

`gclaude_state` (used by the admin `status`) stays English.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_client_scripts.py tests/test_device_cli.py tests/test_install.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/claude-gateway tests/test_client_scripts.py tests/test_device_cli.py
git commit -m "gclaude's setup, sign-in, status and removal messages in its language"
```

---

### Task 6: Windows parity

**Files:**
- Modify: `scripts/windows/claude-gateway.ps1` (header, `Remove-Ours`, `Command-Text`, `Gclaude-On`, `Gclaude-Off`, argument parsing, command switch)
- Modify: `scripts/windows/statusline.ps1` (header, `T`, messages, `/language`)
- Test: `tests/test_windows_client.py` (`@on_windows`: these run in CI's windows job)

**Interfaces:**
- Consumes: `scripts/windows/i18n.json` (Task 1); the same client.json/settings.json fields as Task 2 (`lang`, `gclaude.added_language`, `language`, `env.CLAUDE_GATEWAY_LANG`, `env.CLAUDE_GATEWAY_CMD`).
- Produces: `claude-gateway lang en|fa` on Windows, and `/language` answered by `statusline.ps1 --warn`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_windows_client.py`:

```python
CATALOG = json.loads((WINDOWS / "i18n.json").read_text(encoding="utf-8"))


@on_windows
def test_on_with_lang_fa_sets_the_language_and_the_command_descriptions(installed, stub):
    win = installed
    r = win.cg("on", "--lang", "fa", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url)
    assert r.returncode == 0, r.out
    s = win.settings()
    assert win.client()["lang"] == "fa" and s["language"] == "persian" and s["env"]["CLAUDE_GATEWAY_LANG"] == "fa"
    assert s["env"]["CLAUDE_GATEWAY_CMD"].endswith("claude-gateway.ps1")
    md = (win.gdir / "commands" / "usage.md").read_text(encoding="utf-8")
    line = next(l for l in md.splitlines() if l.startswith("description: "))
    assert json.loads(line.removeprefix("description: ")) == CATALOG["cmd.usage"]["fa"]   # PS 5.1 writes \uXXXX escapes
    assert (win.gdir / "commands" / "language.md").is_file()
    assert win.cg("off").returncode == 0
    s = win.settings()
    assert "language" not in s and "CLAUDE_GATEWAY_LANG" not in s.get("env", {}) and "lang" not in win.client()


@on_windows
def test_on_asks_for_the_language_when_it_can(installed, stub, tmp_path):
    win = installed
    answer = tmp_path / "answer.txt"
    answer.write_text("2\n", encoding="ascii")
    r = win.cg("on", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url, CLAUDE_GATEWAY_TTY=str(answer))
    assert r.returncode == 0 and "Language:" in r.out, r.out
    assert win.client()["lang"] == "fa"


@on_windows
def test_on_without_a_console_uses_english_unsaved(installed, stub):
    win = installed
    r = win.cg("on", "--url", stub.url, "--key", "sk-proxy-k", "--dashboard", stub.url, stdin="")
    assert r.returncode == 0 and "/language fa" in r.out, r.out
    assert "lang" not in win.client() and "language" not in win.settings()


@on_windows
def test_the_hook_answers_language_and_switches(gclaude, installed, stub):
    win = installed
    r = gclaude(" --warn", '{"prompt": "/language"}')
    assert json.loads(r.stdout)["reason"] == CATALOG["language.current"]["en"], r.out
    r = gclaude(" --warn", '{"prompt": "/language fa"}')
    assert json.loads(r.stdout)["reason"] == CATALOG["language.switched"]["fa"], r.out
    assert win.client()["lang"] == "fa" and win.settings()["language"] == "persian"
    stub.status_line = None
    r = gclaude("", CLAUDE_GATEWAY_LANG="fa")
    assert CATALOG["line.unavailable"]["fa"] in r.stdout, r.out
```

- [ ] **Step 2: Confirm they're collected**

Run: `uv run pytest tests/test_windows_client.py -q`
Expected on macOS: the new tests are skipped, and `test_the_scripts_parse` still passes (it parses with pwsh). They fail on Windows until Step 3 is done, which CI shows in Step 5.

- [ ] **Step 3: Implement `claude-gateway.ps1`**

Keep the file ASCII-only.

Near the other paths (after `$Statusline`):

```powershell
$I18n = Join-Path $PSScriptRoot 'i18n.json'   # gclaude's text in English and Persian; Persian can't sit in this ANSI-read file
```

After `Field`:

```powershell
function T([string]$key, [string]$lang, [hashtable]$a = @{}) {   # the catalog's text for key in lang (English when it has none)
  $texts = ([IO.File]::ReadAllText($I18n, (New-Object Text.UTF8Encoding $false)) | ConvertFrom-Json).$key
  $text = [string](Field $texts $lang); if (-not $text) { $text = [string]$texts.en }
  foreach ($k in $a.Keys) { $text = $text.Replace('{' + $k + '}', [string]$a[$k]) }
  return $text
}
function Lang($c) { $l = [string](Field $c 'lang'); if ($l -in 'en', 'fa') { return $l } return 'en' }

function Choose-Lang([string]$given, $c) {   # the language given, else the saved one, else asked once; '' when it can't ask
  if ($given) { return $given }
  $saved = [string](Field $c 'lang'); if ($saved -in 'en', 'fa') { return $saved }
  if ($env:CLAUDE_GATEWAY_FROM_GCLAUDE) { return '' }
  if ($env:CLAUDE_GATEWAY_TTY) {   # tests: the answer from a file, as on macOS/Linux
    $answer = [IO.File]::ReadAllText($env:CLAUDE_GATEWAY_TTY)
  } elseif (-not [Console]::IsInputRedirected) {
    [Console]::Error.Write('Language:  1) English  2) Farsi (Persian)  [1] ')
    $answer = [Console]::ReadLine()
  } else {
    Say 'No terminal to ask on, so gclaude uses English; /language fa switches it to Persian (Farsi).'
    return ''
  }
  $answer = ([string]$answer).Trim()
  if ($answer -in '2', 'fa', 'farsi', 'persian', [string][char]0x06F2) { return 'fa' }   # 0x06F2: the Persian digit 2
  return 'en'
}

function Set-Language($c, $s) {   # client.json's lang into gclaude's settings: Claude's replies, and what the hook needs
  $rec = Field $c 'gclaude'
  $lang = Lang $c
  $envBlock = Field $s 'env'
  if (-not ($envBlock -is [psobject])) { $envBlock = New-Object psobject; Set-Field $s 'env' $envBlock }
  Set-Field $envBlock 'CLAUDE_GATEWAY_LANG' $lang
  Set-Field $envBlock 'CLAUDE_GATEWAY_CMD' $PSCommandPath   # /language runs `claude-gateway lang`
  if ((Field $rec 'added_language') -and ((Field $s 'language') -eq 'persian')) { Remove-Field $s 'language' }
  Remove-Field $rec 'added_language'
  if ($lang -eq 'fa' -and -not (Has $s 'language')) {   # a language the user set in gclaude's settings stays theirs
    Set-Field $s 'language' 'persian'
    Set-Field $rec 'added_language' $true
  }
}
```

`Remove-Ours`: add `'CLAUDE_GATEWAY_LANG', 'CLAUDE_GATEWAY_CMD'` to the env keys removed. Before the final `foreach ... Remove-Field $rec` line, add:

```powershell
  if ((Field $rec 'added_language') -and ((Field $s 'language') -eq 'persian')) { Remove-Field $s 'language' }
```

Also add `'added_language'` to that final list.

`Command-Text([string]$name, [string]$lang)`: each `description: <English>` line becomes `description: $(ConvertTo-Json -Compress (T "cmd.$name" $lang))`. PS 5.1's `ConvertTo-Json` escapes non-ASCII as `\uXXXX`, which is still valid YAML/JSON (the test parses it with `json.loads`). Add the language command:

```powershell
  if ($name -eq 'language') {   # answered by the --warn hook, which runs `claude-gateway lang`
    return @"
---
description: $(ConvertTo-Json -Compress (T 'cmd.language' $lang))
argument-hint: en | fa
disable-model-invocation: true
---
<!-- $UsageMark -->
The claude-gateway hook that answers /language did not run, so the language was not changed. Tell the user, in one
sentence, that running ``gclaude update`` in a terminal reinstalls the hook. Use no tools.
"@
  }
```

Pull the command-writing loop out of `Gclaude-On` into `Write-Commands($c)`, with the name list `'usage', 'account', 'logout_gclaude', 'language'` and calling `Command-Text $name (Lang $c)`. `Gclaude-On` calls `Write-Commands $c`. In `Gclaude-On`, right after `Remove-Ours $s $rec`, add `Set-Language $c $s`. Also copy the catalog beside the statusline copy:

```powershell
  Copy-Item -LiteralPath $I18n -Destination (Join-Path $ClientDir 'i18n.json') -Force
```

`Gclaude-Off`: add `'language'` to the command names removed, and `Remove-Field $c 'lang'` beside `Remove-Field $c 'gclaude'`.

Argument parsing: handle `lang` before the option loop. Its argument isn't an option:

```powershell
if ($cmd -eq 'lang') {   # gclaude's /language runs this (statusline.ps1 --warn). No network and no key, so it works signed out too.
  if ($rest.Count -ne 1 -or [string]$rest[0] -notin 'en', 'fa') { Fail 'Usage: claude-gateway lang en|fa' }
  if (-not (Launcher-Ours)) { Fail 'gclaude is not set up, so it has no language to change.' }
  $c = Read-Json $Client
  Set-Field $c 'lang' ([string]$rest[0])
  $s = Read-Json $Settings
  if (-not ($s -is [psobject])) { $s = New-Object psobject }
  Set-Language $c $s
  Write-Json $Settings $s -Private
  Write-Json $Client $c -Private
  Write-Commands $c
  exit 0
}
```

`$lang = ''` joins the option vars. Add the case arm `'--lang' { $lang = & $next; if ($lang -notin 'en', 'fa') { Fail '--lang is en (English) or fa (Persian).' } }`. In the `'on'` branch, before `Preflight`:

```powershell
    $chosen = Choose-Lang $lang $c
    if ($chosen) { Set-Field $c 'lang' $chosen }
```

Header: add `#   claude-gateway on --lang en|fa   # gclaude's language (asked the first time)` and `#   claude-gateway lang en|fa        # switch it; gclaude's /language runs this`.

- [ ] **Step 4: Implement `statusline.ps1`**

After `Stop-Prompt`:

```powershell
  function T([string]$key, [hashtable]$a = @{}, [string]$lang = $env:CLAUDE_GATEWAY_LANG) {   # i18n.json beside this script
    $texts = ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'i18n.json'), $utf8) | ConvertFrom-Json).$key
    $text = if ($lang -eq 'fa' -and $texts.fa) { [string]$texts.fa } else { [string]$texts.en }
    foreach ($k in $a.Keys) { $text = $text.Replace('{' + $k + '}', [string]$a[$k]) }
    return $text
  }
```

Replace the messages with keys, using the same mapping as Task 4. On Windows, `logout.again_windows` stands in for `logout.again`, `logout.odd` gets `code`/`where`, and the refused case is `logout.http` with `code`/`where` (the unreachable case is `logout.offline`). The `'gateway status unavailable'` fallback, `Account-Line`, `/usage`, `/account` and the warning's `'Gateway: '` all move to `T`.

`/language`, after the logout block:

```powershell
  if (Ours 'language') {   # gclaude's /language [en|fa]: switching runs `claude-gateway lang`
    $arg = ''
    try { $arg = ([string]($stdin | ConvertFrom-Json).prompt).Substring('/language'.Length).Trim().ToLowerInvariant() } catch { }
    $farsi = -join ([char[]](0x0641, 0x0627, 0x0631, 0x0633, 0x06CC))   # the word in Persian script; this file stays ASCII
    if (-not $arg) { Block (T 'language.current') }
    $new = if ($arg -in 'en', 'english') { 'en' } elseif ($arg -in 'fa', 'persian', 'farsi', $farsi) { 'fa' } else { Block (T 'language.usage') }
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $env:CLAUDE_GATEWAY_CMD lang $new 2>&1
    if ($LASTEXITCODE -eq 0) { Block (T 'language.switched' @{} $new) }
    Block (T 'language.failed' @{ error = ([string](@($out)[-1])).Trim() })
  }
```

(`Block` exits, so the `else` arm ends the script there.)

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/test_windows_client.py tests/test_i18n.py -q` (on macOS this covers the parse checks). Then push the branch and watch the `client-scripts` workflow's windows job: `gh run watch` on the run for this branch.
Expected: the windows job passes, including the four new tests.

- [ ] **Step 6: Commit**

```bash
git add scripts/windows/claude-gateway.ps1 scripts/windows/statusline.ps1 tests/test_windows_client.py
git commit -m "Windows: gclaude's language, /language, and its statusline and hook in Persian"
```

---

### Task 7: Docs, hands-on checks, PR

**Files:**
- Modify: `README.md` (the gclaude section: `--lang`, `/language`)
- Modify: `docs/superpowers/specs/2026-09-30-gclaude-bilingual-design.md`, only if the check below needs a restart

- [ ] **Step 1: README**

In the README's gclaude section, beside the other gclaude commands, add:

```markdown
gclaude speaks English or Persian (فارسی): setup asks which (or pass `--lang en|fa` to `claude-gateway on`), and
`/language fa` or `/language en` inside gclaude switches it. It sets Claude Code's own `language` setting for
gclaude, so Claude answers in that language, and gclaude's status line, commands and messages follow.
```

- [ ] **Step 2: Does the language setting take effect without a restart?**

Run `claude-gateway on --gclaude --lang en` against a real gateway, start `gclaude`, and ask "say hi". Then type `/language fa` and ask "say hi" again.
- If the second reply is in Persian, leave `language.switched` as it is.
- If not, change `language.switched` in both catalog copies to say it applies from the next gclaude session. The en text becomes "gclaude now uses English: gclaude's own text now, and Claude's replies from your next gclaude session.", with fa in step. Also update the spec's /language section.
- Run `uv run pytest tests/test_i18n.py tests/test_client_scripts.py -q` again.

- [ ] **Step 3: RTL check by hand**

With `--lang fa`, check these in Terminal.app and iTerm2, and on Windows Terminal from CI's artifacts or a Windows machine if one is at hand:
- the setup question
- the statusline
- `/usage`, `/account`, `/language`
- `gclaude status`

Numbers, percentages and URLs must read left to right and stay whole. If a URL or figure is split by a Persian phrase, add `‎` before it in that `fa` text, in both catalog copies.

- [ ] **Step 4: The whole suite**

Run: `uv run pytest -q`
Expected: all pass (Windows-only tests skipped locally).

- [ ] **Step 5: Commit, push, PR with auto-merge**

```bash
git add README.md docs/superpowers/specs/2026-09-30-gclaude-bilingual-design.md scripts/i18n.json scripts/windows/i18n.json
git commit -m "README: gclaude in English or Persian"
git push -u origin gclaude-bilingual
gh pr create --base master --title "gclaude in English and Persian" --body "$(cat <<'EOF'
Setup asks for English or Persian (or `--lang en|fa`); `/language` switches later. Sets Claude Code's `language`
setting for gclaude only, and gclaude's own text follows, from one catalog (scripts/i18n.json).

Spec: docs/superpowers/specs/2026-09-30-gclaude-bilingual-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --auto --merge
```
