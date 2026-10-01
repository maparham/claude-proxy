#!/bin/sh
# Install or update claude-gateway, the client side of claude-proxy, for this user:
#
#   curl -fsSL https://raw.githubusercontent.com/maparham/claude-proxy/master/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/maparham/claude-proxy/master/install.sh | sh -s -- on --url https://claude.example.com --key sk-proxy-...   # and set up gclaude
#   curl -fsSL https://claude-dash.example.com/install | sh    # a gateway's own: this, then `on` authorizing in the browser
#
# It puts claude-gateway and the files it installs from (statusline.sh, gclaude-sync.py, examples/opencode) in
# ~/.local/share/claude-gateway, replacing an earlier copy, and links ~/.local/bin/claude-gateway to it. Anything
# after `--` then runs as `claude-gateway ...`. Needs curl, tar and python3, and a Claude Code that runs (not for
# `on --opencode`).
# CLAUDE_GATEWAY_REPO (owner/name) and CLAUDE_GATEWAY_REF (branch or tag) pick another source;
# CLAUDE_GATEWAY_TARBALL gives the archive's URL directly.
set -eu

[ -n "${HOME:-}" ] || { echo "install.sh: HOME is not set" >&2; exit 1; }
repo=${CLAUDE_GATEWAY_REPO:-maparham/claude-proxy}
ref=${CLAUDE_GATEWAY_REF:-master}
tarball=${CLAUDE_GATEWAY_TARBALL:-https://codeload.github.com/$repo/tar.gz/$ref}
share=${XDG_DATA_HOME:-$HOME/.local/share}/claude-gateway
bin=$HOME/.local/bin
link=$bin/claude-gateway

for tool in curl tar python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "install.sh: $tool is needed" >&2; exit 1; }
done
# Claude Code must run before anything is set up for it: an npm install whose native binary never arrived leaves a
# `claude` on PATH that only fails (or can't be executed at all), and gclaude would then fail on every start.
case " $* " in
  *" --opencode "*) ;;
  *)
    claude=$(command -v claude 2>/dev/null) ||
      claude=$(IFS=:; for d in $PATH; do [ -e "$d/claude" ] && { echo "$d/claude"; break; }; done; true)
    if [ -z "$claude" ]; then
      installer=${CLAUDE_GATEWAY_CLAUDE_INSTALLER:-https://claude.ai/install.sh}
      echo "Installing Claude Code..."
      script=$(mktemp) log=$(mktemp)
      # Downloaded first, so a failed download isn't run as an empty script that succeeds. Its output is kept for a
      # failure only: on success its PATH advice is already taken care of below.
      if curl -fsSL "$installer" -o "$script" && command -v bash >/dev/null 2>&1 && bash "$script" >"$log" 2>&1; then
        claude=$(command -v claude 2>/dev/null) || claude=$HOME/.local/bin/claude   # where its installer puts it
      fi
      if [ -z "$claude" ] || [ ! -e "$claude" ]; then
        echo "install.sh: Claude Code (claude) could not be installed:" >&2
        tail -n 10 "$log" | sed 's/^/  /' >&2
        echo "Install it (https://claude.ai/install.sh), then run this again." >&2
        rm -f "$script" "$log"; exit 1
      fi
      rm -f "$script" "$log"
      installed=1
    fi
    if ! out=$("$claude" --version 2>&1); then
      echo "install.sh: Claude Code at $claude doesn't run:" >&2
      printf '%s\n' "$out" | head -n 3 | sed 's/^/  /' >&2
      echo "Reinstall it (installed with npm: npm install -g @anthropic-ai/claude-code), then run this again." >&2
      exit 1
    fi
    [ -z "${installed:-}" ] || echo "Installed Claude Code ${out%% *}." ;;
esac
if [ -e "$link" ] && [ ! -L "$link" ]; then
  echo "install.sh: $link already exists and is not a link; left as it is." >&2; exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$tarball" | tar -xzf - -C "$tmp" --strip-components=1
[ -f "$tmp/scripts/claude-gateway" ] || { echo "install.sh: $tarball has no scripts/claude-gateway" >&2; exit 1; }

# Build the new copy beside the old one, then swap, so a failed download never leaves half an install.
rm -rf "$share.new"
mkdir -p "$share.new/scripts" "$share.new/examples" "$bin"
cp "$tmp/scripts/claude-gateway" "$tmp/scripts/statusline.sh" "$tmp/scripts/gclaude-sync.py" "$tmp/scripts/VERSION" "$share.new/scripts/"
cp -R "$tmp/examples/opencode" "$share.new/examples/"
chmod 755 "$share.new/scripts/claude-gateway" "$share.new/scripts/statusline.sh" "$share.new/scripts/gclaude-sync.py"
rm -rf "$share"
mv "$share.new" "$share"
ln -sfn "$share/scripts/claude-gateway" "$link"

case ":$PATH:" in *":$bin:"*) ;; *) echo "Add ~/.local/bin to your PATH to run claude-gateway and gclaude." ;; esac
if [ $# -gt 0 ]; then
  exec "$link" "$@"
fi
echo "Installed claude-gateway. Next: run 'claude-gateway on' (and 'on --opencode' if you use it) to refresh what it set up."
