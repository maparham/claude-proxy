# Using the team's Claude gateway

The gateway at `https://claude.rahkar.pro` shares one Claude subscription across the team. Each person
has their own account, limits and usage.

## Which command to use

| Command | What it is | For |
|---|---|---|
| `gclaude` | Claude Code through the gateway, with your gateway key | Claude, on the team's subscription |
| `claude` | Claude Code on your own login, if you have one | your own Claude account, untouched by the gateway |
| `opencode` | the open-source OpenCode client, with a separate OpenCode key | Muse and other non-Claude models |

`gclaude` and `claude` can run at the same time in the same terminal. `gclaude` uses the same `CLAUDE.md`,
agents, commands, skills, plugins and project memories as your `claude`, and its `/resume` lists your `claude`
sessions too: one you resume in `gclaude` goes on in both. Its settings and the sessions you start in it are
separate.

Claude models only work in Claude Code (`gclaude`): Anthropic accepts the subscription from Claude Code only, so
the gateway refuses Claude requests from OpenCode.

## Setting up

1. Sign up at [claude-dash.rahkar.pro](https://claude-dash.rahkar.pro/dashboard) with Google, GitHub or your
   email. A new account starts with a one-time $5 credit; ask the admin when you need more.
2. Run this in your terminal. You need [Claude Code](https://code.claude.com), `curl` and `python3`.

   ```sh
   curl -fsSL https://claude-dash.rahkar.pro/install | sh
   ```

   It opens the dashboard at a code: check that it matches the one in your terminal and click **Authorize**. The
   terminal then sets up `gclaude` by itself. Over SSH it prints the link to open on any device instead. Each
   computer gets a key of its own, listed under **Your computers** on the dashboard, where you can remove it.

   If it says to add `~/.local/bin` to your PATH, do that (for zsh: `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`)
   and open a new terminal.

   **On Windows**, run this in PowerShell instead (no Python, Git or admin rights needed):

   ```powershell
   irm https://claude-dash.rahkar.pro/install.ps1 | iex
   ```

   Then open a new terminal so `gclaude` is on your PATH. On Windows there is gclaude only: no global mode or
   OpenCode yet, and gclaude doesn't share your `~/.claude` skills and agents yet.
3. Run `gclaude` in a project. The first time, Claude Code asks whether you trust the folder.

If the admin gave you a key (`sk-proxy-…`) instead, add it: `… | sh -s -- on --url https://claude.rahkar.pro --key sk-proxy-...`
with the installer below. Ask the admin for an OpenCode key (`sk-proxy-r-…`) if you want Muse.

For OpenCode (after step 2):

```sh
claude-gateway on --opencode --routes-key sk-proxy-r-...
opencode        # then /models → Claude gateway → Muse Spark 1.3, or ask for the muse subagent
```

If your machine has no Claude login of its own, you can instead point plain `claude` at the gateway with
`claude-gateway on --global --url https://claude.rahkar.pro --key sk-proxy-...`, and back with `claude-gateway off`.

## Seeing your limits

- **Status line** in `gclaude`: your limits, e.g. `◆ maya · daily $61/$100 61% (resets in 3.2 h)`: used, limit, percentage, and when the
  count goes back to zero. A daily window opens with your first request and lasts 24 hours. A figure turns yellow at 80% and
  red at 100%. If you have your own status line in `claude`, `gclaude` shows it after your limits.
- **Warning**: from 80% of a limit, Claude Code shows `Gateway: …` above its reply (again every 15 minutes, and at
  once at 100%). It never blocks you; the gateway refuses requests only once a limit is reached.
- **`/usage`** in `gclaude` shows the same line with a link to the dashboard, without using a request.
- **`/account`** in `gclaude` shows your account and key with a link to the dashboard (a small Haiku request;
  when you're out of credit it's answered without one).
- **`/logout`** in `gclaude` signs this computer out: it revokes this computer's key and removes it. Sign in again
  with `claude-gateway on --login`.
- **`gclaude update`** updates the gateway client and gclaude's setup, then Claude Code itself.
- **Dashboard**: `https://claude-dash.rahkar.pro/dashboard` shows your own requests, sessions, usage and computers.
- **Terminal**: `claude-gateway status`.

## Updating and removing

```sh
curl -fsSL https://claude-dash.rahkar.pro/install | sh   # update (it keeps this computer's key); Windows: irm …/install.ps1 | iex
claude-gateway on --login         # connect this computer again, e.g. after removing it in the dashboard
claude-gateway off                # remove gclaude (keeps its history)
claude-gateway off --opencode     # remove the gateway from OpenCode
```

Nothing is changed in `~/.claude` apart from memories and plugins you add from `gclaude` (they are shared with
`claude` on purpose). Installing or removing a plugin in `gclaude` does it for `claude` too.

## For the admin

On the gateway host, from `deploy/lightsail`, run commands as `docker compose exec gateway claude-proxy …`:

```sh
claude-proxy user add maya                         # prints maya's gateway key once
claude-proxy user routes-key maya                  # her OpenCode key (third-party models only); --remove deletes it
claude-proxy limit set maya cost_daily 100         # limits: see the README
claude-proxy user list
claude-proxy user rotate maya                      # new gateway key; the old one stops working
claude-proxy user revoke maya                      # both keys stop working
```

The dashboard's **Users & limits** page does the same. Send each person their key privately; the README has the
full list of limits and settings.

People who sign up themselves appear there with a `credit` limit (the one-time $5). **Upgrade** replaces it with a
daily allowance; raising or removing the credit in **Limits** works too.
