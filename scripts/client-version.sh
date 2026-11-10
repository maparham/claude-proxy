#!/bin/sh
# gclaude's version, in `git describe`'s form (vMAJOR.MINOR[-N-gHASH]), counting only the commits that change what
# the dashboard's /install serves and gclaude installs: a gateway-only commit leaves it as it was, so gclaude does not
# update itself for it. N is those commits since the newest vMAJOR.MINOR tag, HASH the newest of them. Prints nothing
# outside a git checkout with such a tag. The deploy bakes it into the image (CLIENT_VERSION); client_dist.py and
# claude-gateway's version() run it in a checkout. CLIENT_PATHS must match client_dist.py and the Dockerfile.
CLIENT_PATHS="install.sh install.ps1 scripts/claude-gateway scripts/statusline.sh scripts/gclaude-sync.py scripts/windows examples/opencode"
cd "$(dirname "$0")/.." 2>/dev/null || exit 0
tag=$(git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null) || exit 0
# shellcheck disable=SC2086   # CLIENT_PATHS is a list
n=$(git rev-list --count "$tag..HEAD" -- $CLIENT_PATHS 2>/dev/null) || exit 0
if [ "$n" = 0 ]; then echo "$tag"; else echo "$tag-$n-g$(git log -1 --format=%h HEAD -- $CLIENT_PATHS)"; fi
