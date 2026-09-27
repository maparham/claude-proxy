#!/bin/sh
# Claude Code statusline for gateway users: your limits and the shared account's quota.
#
# ~/.claude/settings.json:
#   "statusLine": {"type": "command", "command": "/path/to/statusline.sh", "refreshInterval": 30}
# With --then CMD it shows the gateway line and then CMD's line (your own statusline, given the same stdin):
#   "command": "/path/to/statusline.sh --then '/path/to/my-statusline.sh'"
# CMD runs with CLAUDE_GATEWAY_STATUS_INNER=1, and this script prints nothing as a statusline in there, so a CMD
# that shows the gateway line itself doesn't show it twice.
# With --warn it is a UserPromptSubmit hook instead. When a figure on the line is at 80% or more it prints
# {"systemMessage": "Gateway: <line>"}, which Claude Code shows; again after 15 minutes, or at once when a
# figure reaches 100%. Nothing else ever goes to stdout in that mode: a hook's plain output joins the prompt.
#   "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/path/to/statusline.sh --warn"}]}]}
# The same hook answers gclaude's /usage command (commands/usage.md there; Claude Code's own /usage can't see the
# gateway): it blocks that prompt, so no model call is made, and gives a fresh line with the dashboard link as the reason.
# Only where that usage.md is claude-gateway's, so another /usage command (plain claude's, or your own) runs as usual.
# With --account it prints who the key belongs to, which key it is and the dashboard link, for gclaude's /account
# (commands/account.md there runs it, and the model repeats the line: a hook's reply would read as an error).
# Only when a limit is reached or the gateway is down, so that model call would fail, --warn answers /account instead.
# It also answers gclaude's /logout (commands/logout.md there, in place of Claude Code's own): it revokes the key on
# the gateway when it is this computer's own (the first key may be in use elsewhere, so it stays valid), and removes
# it from gclaude's settings.json and from client.json (beside this script, or CLAUDE_GATEWAY_CLIENT).
# Environment (e.g. in the same settings.json "env" block):
#   ANTHROPIC_AUTH_TOKEN        your gateway key (already set for the gateway); in own-login mode, where
#                               it is unset, the key is read from ANTHROPIC_CUSTOM_HEADERS (x-gateway-key)
#   CLAUDE_GATEWAY_DASHBOARD    dashboard base URL, e.g. http://gateway.lan:8081
warn= usage= account= account_prompt= logout= then=
case "${1:-}" in --warn) warn=1 ;; --account) account=1 ;; --then) then=${2:-} ;; esac
[ -z "$warn$account" ] && [ -n "${CLAUDE_GATEWAY_STATUS_INNER:-}" ] && exit 0
input=
[ -n "$account" ] || input=$(cat)   # Claude Code sends session or prompt JSON on stdin; only --warn looks at it, for /usage
ours() {   # name: the prompt is /name and gclaude's commands/name.md is claude-gateway's
  [ -n "$warn" ] && [ -n "${CLAUDE_CONFIG_DIR:-}" ] &&
    grep -qF '# Installed by claude-gateway on --gclaude.' "$CLAUDE_CONFIG_DIR/commands/$1.md" 2>/dev/null &&
    printf '%s' "$input" | grep -Eq '"prompt"[[:space:]]*:[[:space:]]*"/'"$1"'([[:space:]][^"]*)?"'
}
ours usage && usage=1
ours account && account_prompt=1
ours logout && logout=1
json_str() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
block() { printf '{"decision": "block", "reason": "%s"}\n' "$(json_str "$1")"; exit 0; }
stop() { printf '{"continue": false, "stopReason": "%s"}\n' "$(json_str "$1")"; exit 0; }   # ends the prompt with no model call, like block
gateway_key() { printf '%s' "${ANTHROPIC_AUTH_TOKEN:-$(printf '%s\n' "${ANTHROPIC_CUSTOM_HEADERS:-}" | sed -n 's/^[Xx]-[Gg]ateway-[Kk]ey: *//p' | head -n 1)}"; }
own_line() { [ -n "$then" ] && printf '%s' "$input" | CLAUDE_GATEWAY_STATUS_INNER=1 sh -c "$then" 2>/dev/null; }
if [ -n "$logout" ]; then   # gclaude's /logout: revoke this computer's key on the gateway, then drop every copy of it here
  key=$(gateway_key) reply=
  if [ -n "$key" ] && [ -n "${CLAUDE_GATEWAY_DASHBOARD:-}" ]; then
    reply=$(printf 'Authorization: Bearer %s\n' "$key" | curl -sS --max-time 5 -X POST -H @- -w '\n%{http_code}' \
      "${CLAUDE_GATEWAY_DASHBOARD%/}/api/me/logout" 2>/dev/null | tr -d " ")
  fi
  python3 - "$CLAUDE_CONFIG_DIR/settings.json" "${CLAUDE_GATEWAY_CLIENT:-$(dirname "$0")/client.json}" "$key" <<'EOF' ||
import json, os, sys
settings, client, key = sys.argv[1:]
def edit(path, change):
    try:
        data = json.load(open(path))
    except FileNotFoundError:
        return
    if change(data):
        tmp = path + ".logout"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
edit(settings, lambda s: isinstance(s.get("env"), dict) and s["env"].pop("ANTHROPIC_AUTH_TOKEN", None) is not None)
edit(client, lambda c: bool(key) and c.get("key") == key and c.pop("key") is not None)
if os.path.exists(settings + ".bak-claude-gateway"):   # claude-gateway's copy from before its last edit holds the key too
    os.remove(settings + ".bak-claude-gateway")
EOF
    stop "Sign-out failed: the key could not be removed from $CLAUDE_CONFIG_DIR/settings.json. claude-gateway off --gclaude removes it."
  again="Exit gclaude now (/exit); to sign in again: claude-gateway on --login"
  case "$reply" in
    *'"revoked":true'*200) stop "Signed out: this computer's key is revoked on the gateway and removed from gclaude. $again" ;;
    *'"revoked":false'*200) stop "Signed out: the key is removed from gclaude. It is your first key, so it still works wherever else it is set up. $again" ;;
    *401|*403) stop "Signed out: the key is removed from gclaude (the gateway no longer accepted it). $again" ;;
    *) stop "Signed out here: the key is removed from gclaude, but the gateway could not be reached to revoke it; remove this computer in the dashboard${CLAUDE_GATEWAY_DASHBOARD:+ (${CLAUDE_GATEWAY_DASHBOARD%/}/dashboard)}. $again" ;;
  esac
fi
if [ -z "${CLAUDE_GATEWAY_DASHBOARD:-}" ]; then
  [ -n "$account" ] && { echo "Account details unavailable: CLAUDE_GATEWAY_DASHBOARD is not set (rerun claude-gateway on --gclaude)."; exit 0; }
  [ -n "$usage" ] && block "Gateway status unavailable: CLAUDE_GATEWAY_DASHBOARD is not set (rerun claude-gateway on --gclaude)."
  [ -n "$warn" ] && exit 0
  [ -n "$then" ] && { own_line; exit 0; }
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
# figures LINE [colour]: each used/limit pair (with its " req"/" tok" unit) followed by its percentage, rounded
# down so 100% means reached: `daily $305/$500 61%`. With colour: cyan with a leading diamond, so it stands apart
# from Claude Code's own items, and a figure turns yellow at 80% and red at 100%. The cache keeps the server's line.
figures() {
  printf '%s\n' "$1" | awk -v colour="${2:-}" "$FIGURES"'
  {
    out = ""; rest = $0
    while (match(rest, RE)) {
      pre = substr(rest, 1, RSTART - 1); tok = substr(rest, RSTART, RLENGTH); v = pct(tok)
      rest = substr(rest, RSTART + RLENGTH)
      if (split(tok, ab, "/") == 2) {
        if (rest ~ /^ (req|tok)/) { tok = tok substr(rest, 1, 4); rest = substr(rest, 5) }
        if (num(ab[2])) tok = tok " " int(v + 1e-9) "%"   # 4.2M/5.0M is 84%, not 83.99999...
      }
      c = colour == "" ? "" : v >= 100 ? "\033[31m" : v >= 80 ? "\033[33m" : ""
      out = out pre (c ? c tok "\033[36m" : tok)
    }
    printf (colour == "" ? "%s%s\n" : "\033[36m\342\227\206 %s%s\033[0m\n"), out, rest
  }'
}
show() { figures "$1" colour; }
peak() {   # the highest figure on the line, as a whole percentage (0 when there is none)
  printf '%s\n' "$1" | awk "$FIGURES"'
  {
    m = 0; rest = $0
    while (match(rest, RE)) { v = pct(substr(rest, RSTART, RLENGTH)); if (v > m) m = v; rest = substr(rest, RSTART + RLENGTH) }
    printf "%d\n", m
  }'
}
age() { echo $(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1") )); }

status() {   # format: /api/me/status as text, with this key
  key=$(gateway_key)
  # The key goes to curl on stdin: in its arguments, `ps` would show it to every local user.
  printf 'Authorization: Bearer %s\n' "$key" | curl -fsS --max-time 3 -H @- \
    "${CLAUDE_GATEWAY_DASHBOARD%/}/api/me/status?format=$1" 2>/dev/null
}

account_line() {
  if line=$(status account) && [ -n "$line" ]; then
    echo "Account: $line · dashboard: ${CLAUDE_GATEWAY_DASHBOARD%/}/dashboard"
  else
    echo "Account details unavailable; see ${CLAUDE_GATEWAY_DASHBOARD%/}/dashboard"
  fi
}
if [ -n "$account" ]; then
  account_line
  exit 0
fi

if [ -z "$usage$account_prompt" ] && [ -f "$cache" ] && [ "$(age "$cache")" -lt 30 ]; then
  line=$(cat "$cache")
elif line=$(status text); then
  printf '%s\n' "$line" > "$cache"
else
  line=
fi

if [ -z "$warn" ]; then
  own=$(own_line)
  printf '%s%s\n' "$(show "${line:-gateway status unavailable}")" "${own:+ | $own}"
  exit 0
fi

if [ -n "$usage" ]; then
  [ -n "$line" ] || block "Gateway status unavailable; see ${CLAUDE_GATEWAY_DASHBOARD%/}/dashboard"
  block "Gateway: $(figures "$line") · details: ${CLAUDE_GATEWAY_DASHBOARD%/}/dashboard"
fi

if [ -n "$account_prompt" ]; then   # the model answers /account, unless it can't be reached
  [ -n "$line" ] || block "$(account_line)"
  [ "$(peak "$line")" -ge 100 ] 2>/dev/null && block "$(account_line) · limit reached: $(figures "$line")"
fi

# --warn: band 1 from 80%, band 2 from 100%. The state file holds the band last warned about; its age is the time since.
[ -n "$line" ] || exit 0
p=$(peak "$line")
band=0
[ "${p:-0}" -ge 80 ] 2>/dev/null && band=1
[ "${p:-0}" -ge 100 ] 2>/dev/null && band=2
state="$cache.warned"
if [ "$band" = 0 ]; then
  exit 0
fi
last=$(cat "$state" 2>/dev/null)
case "$last" in 1|2) ;; *) last=0 ;; esac
if [ "$band" -le "$last" ] && [ "$(age "$state")" -lt 900 ]; then
  exit 0
fi
printf '%s\n' "$band" > "$state"
printf '{"systemMessage": "%s"}\n' "$(json_str "Gateway: $(figures "$line")")"
exit 0
