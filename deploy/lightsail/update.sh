#!/usr/bin/env bash
# Ship this checkout's source to the box and restart the gateway. The box's data volume (grant, users,
# history), config.toml and gateway.env are left as they are.
# Run from the repo root:  deploy/lightsail/update.sh ec2-user@<server-ip>
set -euo pipefail
HOST=${1:?usage: $0 user@host}
REMOTE=claude-gateway/deploy/lightsail

rsync -az --delete --exclude .git --exclude .venv --exclude '__pycache__' --exclude '*.db*' --exclude '*.env' --exclude .deploy.lock \
  --exclude reports --exclude research_notes --exclude docs --exclude tests ./ "$HOST:claude-gateway/"
ssh "$HOST" bash -s <<REMOTE_SCRIPT
set -euo pipefail
cd $REMOTE
exec 9>.deploy.lock; flock -n 9 || { echo "a CI deploy is running; try again shortly"; exit 1; }
TAG=manual-\$(date -u +%Y%m%dT%H%M%SZ)   # never reuse a CI per-commit tag for a local build
sudo docker build -q -t "claude-proxy:\$TAG" ../.. >/dev/null
printf 'GATEWAY_TAG=%s\n' "\$TAG" > .env
sudo docker compose up -d --no-build
up=
for i in \$(seq 30); do
  if curl -fsS --max-time 5 http://127.0.0.1:18480/health >/dev/null 2>&1; then up=1; break; fi
  sleep 1
done
[ -n "\$up" ] || { echo "gateway did not answer on http://127.0.0.1:18480/health within 30 s" >&2; exit 1; }
curl -fsS http://127.0.0.1:18480/health && echo
sudo docker compose exec -T gateway claude-proxy status | grep -v "HTTP Request"
REMOTE_SCRIPT
