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

  on stderr, reading the answer from `/dev/tty` in sh (stdin is the piped installer; `1`, `2`, `۲`, `en`, `fa` and
  `فارسی` are understood), `Read-Host` in PowerShell (an ASCII prompt there: `1) English  2) Farsi (Persian)`). It
  asks only when stderr is a terminal (sh) or stdin is not redirected (PowerShell). Otherwise (CI, tests, no
  terminal) it uses English without saving it and says `/language fa` switches, so the question comes again at the
  next `on` that has a terminal, `gclaude update` included.
- Once saved, `gclaude update` and a bare `on` keep the choice and never ask; nor does the sign-in that gclaude
  itself starts (`CLAUDE_GATEWAY_FROM_GCLAUDE`).
- `off --gclaude` removes `lang` with the rest of what `on` added.

## Claude's replies

`on --gclaude` writes `"language": "persian"` into gclaude's own `settings.json` for `fa`, and removes it for `en`.
Claude Code then adds "Always respond in persian…" to its system prompt; code and identifiers stay as they are.
The same settings.json `env` gets `CLAUDE_GATEWAY_LANG` (`en` or `fa`, for the statusline and hook) and
`CLAUDE_GATEWAY_CMD` (this claude-gateway, which /language runs). It's recorded in client.json's `gclaude` record like the other settings `on` adds (`added_language`), so `off
--gclaude` removes it, and a `language` the user set there themselves is left alone (recorded as not ours, never
overwritten).

## Strings

`scripts/i18n.json` is the catalog: `{"<key>": {"en": "…", "fa": "…"}}`, with `{name}` placeholders filled by the
caller. Both installers copy it into the share dir beside statusline.sh (`scripts/windows/` gets its own copy via
the Windows installer's source folder; a test checks the two are identical).

Readers:

- the Python embedded in `claude-gateway` and `gclaude-sync.py`: `json.load`, `t(key, **args)`;
- `claude-gateway` and `statusline.sh`: a `t key [name=value…]` shell helper (one `python3` call). It is called
  only for text actually shown, so a statusline with figures to show, or a hook with nothing to say, makes no
  extra call;
- `statusline.ps1`: `ConvertFrom-Json`, a `T` function.

Placeholders are filled by plain replacement of `{name}`, the same in every reader.

A key with no `fa` text falls back to `en`. Which language to use: `client.json`'s `lang` in claude-gateway;
`CLAUDE_GATEWAY_LANG` from gclaude's settings.json `env` in the statusline and hook.

The `description` of each gclaude command (usage, account, logout_gclaude, language) is written in the chosen
language, so the `/` menu shows it in Persian. Their bodies, instructions to the model for when the hook is
missing, stay English: the model answers in the `language` setting anyway. Their marker comment stays the same, so
statusline.sh's `ours` check still recognises them.

In Persian on macOS/Linux: the setup question, the browser sign-in messages, `on`/`off`/`gclaude status` output
for gclaude, and the gclaude launcher's messages. On Windows: the statusline, hook answers and command descriptions
(all shown by Claude Code, which reads UTF-8).

What stays English: global mode and OpenCode, errors meant for an admin or a bug report, `gclaude-sync.py` notes,
the gateway's own figures line (`alice · daily 10/100 req`, written by the server), and on Windows the
`claude-gateway.ps1` output and the `gclaude.cmd` launcher's messages (the console there uses a legacy code page,
and switching it would garble Claude Code's own output in the same console).

## /language

`commands/language.md`, installed and removed like the other gclaude commands, with a stub body that says the hook
did not run. The `--warn` UserPromptSubmit hook answers it and blocks the prompt, so no model call is made:

- `/language` alone: the current language and how to switch (`/language en`, `/language fa`), in the current language;
- `/language fa` (also `persian`, `فارسی`) or `/language en` (also `english`): updates `lang` in client.json, `language`
  and `env.CLAUDE_GATEWAY_LANG` in gclaude's settings.json, rewrites the command files, and answers in the new
  language;
- anything else: the usage line again.

Switching runs `claude-gateway lang en|fa` (`CLAUDE_GATEWAY_CMD`): it saves `lang`, sets the two settings, and
rewrites the command files and the launcher. It needs no network and no key, so it also works signed out. If it
fails, the reply gives its last line of error.

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
