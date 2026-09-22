#!/bin/sh
# Claude Code statusline for gateway users: your limits and the shared account's quota.
#
# ~/.claude/settings.json:
#   "statusLine": {"type": "command", "command": "/path/to/statusline.sh", "refreshInterval": 30}
# Environment (e.g. in the same settings.json "env" block):
#   ANTHROPIC_AUTH_TOKEN        your gateway key (already set for the gateway)
#   CLAUDE_GATEWAY_DASHBOARD    dashboard base URL, e.g. http://gateway.lan:8081
cat >/dev/null   # Claude Code sends session JSON on stdin; this line does not need it.
: "${CLAUDE_GATEWAY_DASHBOARD:?set CLAUDE_GATEWAY_DASHBOARD}"
cache="${TMPDIR:-/tmp}/claude-gateway-status.$(id -u)"
if [ -f "$cache" ] && [ $(( $(date +%s) - $(stat -c %Y "$cache" 2>/dev/null || stat -f %m "$cache") )) -lt 30 ]; then
  cat "$cache"; exit 0
fi
if line=$(curl -fsS --max-time 3 -H "Authorization: Bearer ${ANTHROPIC_AUTH_TOKEN}" \
          "${CLAUDE_GATEWAY_DASHBOARD%/}/api/me/status?format=text" 2>/dev/null); then
  printf '%s\n' "$line" | tee "$cache"
else
  echo "gateway status unavailable"
fi
