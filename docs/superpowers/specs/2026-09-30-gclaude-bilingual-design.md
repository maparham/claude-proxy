# gclaude in English and Persian

## Goal

A gclaude user picks English or Persian (فارسی) when gclaude is set up, and can switch later with `/language`
inside gclaude. The choice covers:

- Claude's replies, through Claude Code's built-in `language` setting;
- the text gclaude itself shows: the setup prompt and messages, the statusline and limit warning, and the
  /usage, /account, /logout_gclaude and /language commands.

Claude Code's own interface stays English: gclaude can't translate it. Plain `claude` and `~/.claude` are never
touched.

This is sub-project 1 of 2. Sub-project 2 is the dashboard website in Persian (front-end translations, RTL layout,
a Persian font) and gets its own spec.

## Saved choice

`client.json` holds `"lang": "en"` or `"lang": "fa"`, the single source of truth. A missing or unknown value means `en`.

- `claude-gateway on --gclaude --lang en|fa` (and `-Lang` in claude-gateway.ps1) sets it. `install.sh … | sh -s --
  on --lang fa` passes it through, as other `on` flags are.
- Without `--lang`, and only when `client.json` has no `lang` yet, `on` asks on the terminal:

      Language / زبان:  1) English  2) فارسی  [1]

  from `/dev/tty` in sh (stdin is the piped installer), `[Console]::ReadLine` in PowerShell. With no terminal (CI,
  `/dev/tty` unopenable, `[Console]::IsInputRedirected` with no console), it picks English and says so.
- `gclaude update`, a bare `on`, and `on --login` keep the saved choice and never ask.

## Claude's replies

`on --gclaude` writes `"language": "persian"` into gclaude's own `settings.json` for `fa`, and removes it for `en`.
Claude Code then adds "Always respond in persian…" to its system prompt; code and identifiers stay as they are.
It's recorded in client.json's `gclaude` record like the other settings `on` adds (`added_language`), so `off
--gclaude` removes it, and a `language` the user set there themselves is left alone (recorded as not ours, never
overwritten).

## Strings

`scripts/i18n.json` is the catalog: `{"<key>": {"en": "…", "fa": "…"}}`, with `{name}` placeholders filled by the
caller. Both installers copy it into the share dir beside statusline.sh (`scripts/windows/` gets its own copy via
the Windows installer's source folder; a test checks the two are identical).

Readers:

- the Python embedded in `claude-gateway` and `gclaude-sync.py`: `json.load`, `t(key, **args)`;
- `statusline.sh`: a `t key [name=value…]` helper. When lang is `en` it uses the English text already in the script
  and makes no extra call, so the 30-second statusline stays as cheap as now. When `fa`, one `python3` call per run
  reads every key the run needs;
- `claude-gateway.ps1`, `statusline.ps1`: `ConvertFrom-Json`, a `T` function.

A key with no `fa` text falls back to `en`. Which language to use: `client.json`'s `lang`, read where client.json is
already read; statusline and the hook read `CLAUDE_GATEWAY_LANG` from gclaude's settings.json `env` block, which
`on` and `/language` keep equal to client.json's `lang`.

The gclaude command files (commands/usage.md, account.md, logout_gclaude.md, language.md) are written in the chosen
language, so the `/` menu's descriptions show in Persian too. Their marker comment stays the same, so
statusline.sh's `ours` check still recognises them.

What stays English: global mode, admin-facing `claude-gateway` output (status details, errors meant for bug reports),
`gclaude-sync.py` notes, and the `gclaude.cmd` launcher's few messages on Windows (cmd.exe reads a .cmd file in the
console code page, not UTF-8).

## /language

`commands/language.md`, installed and removed like the other gclaude commands, with a stub body that says the hook
did not run. The `--warn` UserPromptSubmit hook answers it and blocks the prompt, so no model call is made:

- `/language` alone: the current language and how to switch (`/language en`, `/language fa`), in the current language;
- `/language fa` (also `persian`, `فارسی`) or `/language en` (also `english`): updates `lang` in client.json, `language`
  and `env.CLAUDE_GATEWAY_LANG` in gclaude's settings.json, rewrites the command files, and answers in the new
  language;
- anything else: the usage line again.

Claude Code watches settings.json; the implementation checks whether the `language` setting takes effect in the
running session. If it needs a restart, the `/language` reply says so.

## Persian on a terminal

Persian is right-to-left and many terminals render mixed-direction text poorly. The statusline keeps numbers,
percentages and URLs as separate segments separated by ` · `, never inside a Persian phrase. Checked by hand in
Terminal.app, iTerm2 and Windows Terminal before merging.

## Tests

- `i18n.json`: every key has non-empty `en` and `fa`; the placeholders in `fa` match those in `en`; the Windows copy
  is identical.
- `on --lang fa` / `--lang en`: client.json's `lang`, settings.json's `language` and `env.CLAUDE_GATEWAY_LANG`, the
  command files' language; a user's own `language` is left alone; `off` removes only what `on` added.
- The prompt: answered `2` on a pty gives `fa`; no terminal gives `en`; an existing `lang` is not asked again.
- `/language`: each form above, through the hook, for sh and PowerShell.
- The statusline in `fa` prints the Persian labels; in `en` it's byte-for-byte what it prints today.
