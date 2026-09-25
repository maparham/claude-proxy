#!/bin/sh
# Claude Code statusline for gateway users: your limits and the shared account's quota.
#
# ~/.claude/settings.json:
#   "statusLine": {"type": "command", "command": "/path/to/statusline.sh", "refreshInterval": 30}
# Environment (e.g. in the same settings.json "env" block):
#   ANTHROPIC_AUTH_TOKEN        your gateway key (already set for the gateway); in own-login mode, where
#                               it is unset, the key is read from ANTHROPIC_CUSTOM_HEADERS (x-gateway-key)
#   CLAUDE_GATEWAY_DASHBOARD    dashboard base URL, e.g. http://gateway.lan:8081
cat >/dev/null   # Claude Code sends session JSON on stdin; this line does not need it.
: "${CLAUDE_GATEWAY_DASHBOARD:?set CLAUDE_GATEWAY_DASHBOARD}"
cache="${TMPDIR:-/tmp}/claude-gateway-status.$(id -u)"
# Cyan with a leading diamond, so it stands apart from Claude Code's own items; a percentage or a
# used/limit pair (`$61/$100`, `4.2M/5.0M`) turns yellow at 80% and red at 100%. The cache keeps the plain line.
show() {
  printf '%s\n' "$1" | awk '
  function num(t) { gsub(/[$,%]/, "", t); return t ~ /M$/ ? t * 1e6 : t ~ /K$/ ? t * 1e3 : t + 0 }
  {
    out = ""; rest = $0
    while (match(rest, /[$]?[0-9][0-9,.]*[KM]?\/[$]?[0-9][0-9,.]*[KM]?%?|[0-9]+(\.[0-9]+)?%/)) {
      tok = substr(rest, RSTART, RLENGTH)
      if (split(tok, ab, "/") == 2) v = num(ab[2]) ? 100 * num(ab[1]) / num(ab[2]) : 0
      else v = num(tok)
      c = v >= 100 ? "\033[31m" : v >= 80 ? "\033[33m" : ""
      out = out substr(rest, 1, RSTART - 1) (c ? c tok "\033[36m" : tok)
      rest = substr(rest, RSTART + RLENGTH)
    }
    printf "\033[36m\342\227\206 %s%s\033[0m\n", out, rest
  }'
}
if [ -f "$cache" ] && [ $(( $(date +%s) - $(stat -c %Y "$cache" 2>/dev/null || stat -f %m "$cache") )) -lt 30 ]; then
  show "$(cat "$cache")"; exit 0
fi
key=${ANTHROPIC_AUTH_TOKEN:-$(printf '%s\n' "${ANTHROPIC_CUSTOM_HEADERS:-}" | sed -n 's/^[Xx]-[Gg]ateway-[Kk]ey: *//p' | head -n 1)}
if line=$(curl -fsS --max-time 3 -H "Authorization: Bearer ${key}" \
          "${CLAUDE_GATEWAY_DASHBOARD%/}/api/me/status?format=text" 2>/dev/null); then
  printf '%s\n' "$line" > "$cache"
  show "$line"
else
  show "gateway status unavailable"
fi
