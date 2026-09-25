#!/bin/sh
# Claude Code statusline for gateway users: your limits and the shared account's quota.
#
# ~/.claude/settings.json:
#   "statusLine": {"type": "command", "command": "/path/to/statusline.sh", "refreshInterval": 30}
# With --warn it is a UserPromptSubmit hook instead. When a figure on the line is at 80% or more it prints
# {"systemMessage": "Gateway: <line>"}, which Claude Code shows; again after 15 minutes, or at once when a
# figure reaches 100%. Nothing else ever goes to stdout in that mode: a hook's plain output joins the prompt.
#   "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/path/to/statusline.sh --warn"}]}]}
# Environment (e.g. in the same settings.json "env" block):
#   ANTHROPIC_AUTH_TOKEN        your gateway key (already set for the gateway); in own-login mode, where
#                               it is unset, the key is read from ANTHROPIC_CUSTOM_HEADERS (x-gateway-key)
#   CLAUDE_GATEWAY_DASHBOARD    dashboard base URL, e.g. http://gateway.lan:8081
warn=
[ "${1:-}" = --warn ] && warn=1
cat >/dev/null   # Claude Code sends session or prompt JSON on stdin; this script does not need it.
if [ -z "${CLAUDE_GATEWAY_DASHBOARD:-}" ]; then
  [ -n "$warn" ] && exit 0
  echo "statusline.sh: set CLAUDE_GATEWAY_DASHBOARD" >&2
  exit 1
fi
cache="${TMPDIR:-/tmp}/claude-gateway-status.$(id -u)"
# A figure is a percentage or a used/limit pair (`$61/$100`, `4.2M/5.0M`); pct() gives it as a percentage.
FIGURES='
function num(t) { gsub(/[$,%]/, "", t); return t ~ /M$/ ? t * 1e6 : t ~ /K$/ ? t * 1e3 : t + 0 }
function pct(tok,  ab) { if (split(tok, ab, "/") == 2) return num(ab[2]) ? 100 * num(ab[1]) / num(ab[2]) : 0; return num(tok) }
BEGIN { RE = "[$]?[0-9][0-9,.]*[KM]?/[$]?[0-9][0-9,.]*[KM]?%?|[0-9]+([.][0-9]+)?%" }
'
# Cyan with a leading diamond, so it stands apart from Claude Code's own items; a figure turns yellow at 80%
# and red at 100%. The cache keeps the plain line.
show() {
  printf '%s\n' "$1" | awk "$FIGURES"'
  {
    out = ""; rest = $0
    while (match(rest, RE)) {
      tok = substr(rest, RSTART, RLENGTH); v = pct(tok)
      c = v >= 100 ? "\033[31m" : v >= 80 ? "\033[33m" : ""
      out = out substr(rest, 1, RSTART - 1) (c ? c tok "\033[36m" : tok)
      rest = substr(rest, RSTART + RLENGTH)
    }
    printf "\033[36m\342\227\206 %s%s\033[0m\n", out, rest
  }'
}
peak() {   # the highest figure on the line, as a whole percentage (0 when there is none)
  printf '%s\n' "$1" | awk "$FIGURES"'
  {
    m = 0; rest = $0
    while (match(rest, RE)) { v = pct(substr(rest, RSTART, RLENGTH)); if (v > m) m = v; rest = substr(rest, RSTART + RLENGTH) }
    printf "%d\n", m
  }'
}
age() { echo $(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1") )); }

if [ -f "$cache" ] && [ "$(age "$cache")" -lt 30 ]; then
  line=$(cat "$cache")
else
  key=${ANTHROPIC_AUTH_TOKEN:-$(printf '%s\n' "${ANTHROPIC_CUSTOM_HEADERS:-}" | sed -n 's/^[Xx]-[Gg]ateway-[Kk]ey: *//p' | head -n 1)}
  if line=$(curl -fsS --max-time 3 -H "Authorization: Bearer ${key}" \
            "${CLAUDE_GATEWAY_DASHBOARD%/}/api/me/status?format=text" 2>/dev/null); then
    printf '%s\n' "$line" > "$cache"
  else
    line=
  fi
fi

if [ -z "$warn" ]; then
  show "${line:-gateway status unavailable}"
  exit 0
fi

# --warn: band 1 from 80%, band 2 from 100%. The state file holds the band last warned about; its age is the time since.
[ -n "$line" ] || exit 0
p=$(peak "$line")
band=0
[ "${p:-0}" -ge 80 ] 2>/dev/null && band=1
[ "${p:-0}" -ge 100 ] 2>/dev/null && band=2
state="$cache.warned"
if [ "$band" = 0 ]; then
  rm -f "$state"
  exit 0
fi
last=$(cat "$state" 2>/dev/null)
case "$last" in 1|2) ;; *) last=0 ;; esac
if [ "$band" -le "$last" ] && [ "$(age "$state")" -lt 900 ]; then
  exit 0
fi
printf '%s\n' "$band" > "$state"
msg=$(printf 'Gateway: %s' "$line" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')
printf '{"systemMessage": "%s"}\n' "$msg"
exit 0
