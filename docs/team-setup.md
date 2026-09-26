# Using the team's Claude gateway

The gateway at `https://claude.rahkar.pro` shares one Claude subscription across the team. Each person
has their own key, limits and usage.

## Which command to use

| Command | What it is | For |
|---|---|---|
| `gclaude` | Claude Code through the gateway, with your gateway key | Claude, on the team's subscription |
| `claude` | Claude Code on your own login, if you have one | your own Claude account, untouched by the gateway |
| `opencode` | the open-source OpenCode client, with a separate OpenCode key | Muse and other non-Claude models |

`gclaude` and `claude` can run at the same time in the same terminal. `gclaude` uses the same `CLAUDE.md`,
agents, commands, skills, plugins and project memories as your `claude`. Its settings, history and sessions are
separate.

Claude models only work in Claude Code (`gclaude`): Anthropic accepts the subscription from Claude Code only, so
the gateway refuses Claude requests from OpenCode.

## Setting up

1. Ask the admin for your gateway key (`sk-proxy-…`), and for an OpenCode key (`sk-proxy-r-…`) if you want Muse.
2. Install and set up `gclaude` in one line. You need [Claude Code](https://code.claude.com), `curl` and `python3`.

   ```sh
   curl -fsSL https://raw.githubusercontent.com/maparham/claude-proxy/master/install.sh | sh -s -- on --url https://claude.rahkar.pro --key sk-proxy-...
   ```

   If it says to add `~/.local/bin` to your PATH, do that (for zsh: `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`)
   and open a new terminal.
3. Run `gclaude` in a project. The first time, Claude Code asks whether you trust the folder.

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
- **Dashboard**: `https://claude-dash.rahkar.pro/dashboard`, signed in with your gateway key, shows your own
  requests, sessions and usage.
- **Terminal**: `claude-gateway status`.

## Updating and removing

```sh
curl -fsSL https://raw.githubusercontent.com/maparham/claude-proxy/master/install.sh | sh -s -- on   # update
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
