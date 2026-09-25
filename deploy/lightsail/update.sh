#!/usr/bin/env bash
# Ship this checkout's source to the box and restart the gateway. The box's data volume (grant, users,
# history), config.toml and gateway.env are left as they are.
# Run from the repo root:  deploy/lightsail/update.sh ec2-user@3.139.146.5
set -euo pipefail
HOST=${1:?usage: $0 user@host}
REMOTE=claude-gateway/deploy/lightsail

rsync -az --delete --exclude .git --exclude .venv --exclude '__pycache__' --exclude '*.db*' --exclude '*.env' \
  --exclude reports --exclude research_notes --exclude docs --exclude tests ./ "$HOST:claude-gateway/"
ssh "$HOST" bash -s <<REMOTE_SCRIPT
set -euo pipefail
cd $REMOTE
printf 'GATEWAY_TAG=manual-%s\n' "\$(date -u +%Y%m%dT%H%M%SZ)" > .env   # keep CI's per-commit tags honest
sudo docker compose build -q
sudo docker compose up -d
for i in \$(seq 30); do curl -fsS http://127.0.0.1:18480/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:18480/health && echo
sudo docker compose exec -T gateway claude-proxy status | grep -v "HTTP Request"
REMOTE_SCRIPT
