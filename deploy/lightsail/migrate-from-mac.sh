#!/usr/bin/env bash
# Move the running gateway (its subscription grant, users, limits and history) from this Mac to the box.
# Run from the repo root:  deploy/lightsail/migrate-from-mac.sh ec2-user@<server-ip>
# Only one process may ever refresh the grant, so the Mac gateway is stopped first and its data
# directory is retired at the end.
set -euo pipefail
HOST=${1:?usage: $0 user@host}
SRC=${CLAUDE_GATEWAY_HOME:-$HOME/.claude-gateway}
REMOTE=claude-gateway/deploy/lightsail
[ -f "$SRC/claude_proxy.db" ] && [ -f "$SRC/gateway.key" ] || { echo "no gateway data in $SRC"; exit 1; }

echo "1/6 stopping the local gateway"
pkill -f "claude-proxy serve" || true
sleep 2
if pgrep -f "claude-proxy serve" >/dev/null; then echo "local gateway still running; stop it first"; exit 1; fi

echo "2/6 consistent copy of the database"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT; chmod 700 "$TMP"
sqlite3 "$SRC/claude_proxy.db" ".backup $TMP/migrate.db"
[ "$(sqlite3 "$TMP/migrate.db" 'pragma integrity_check')" = ok ]

echo "3/6 syncing source and copying data to $HOST"
rsync -az --delete --exclude .git --exclude .venv --exclude '__pycache__' --exclude '*.db*' --exclude '*.env' \
  --exclude reports --exclude research_notes --exclude docs --exclude tests ./ "$HOST:claude-gateway/"
scp -q "$TMP/migrate.db" "$HOST:$REMOTE/migrate.db"
{ printf 'CLAUDE_PROXY_CREDENTIAL_KEY='; cat "$SRC/gateway.key"; if [ -n "${META_API_KEY:-}" ]; then printf 'META_API_KEY=%s\n' "$META_API_KEY"; fi; } \
  | ssh "$HOST" "umask 077; cat > $REMOTE/gateway.env"

echo "4/6 building and starting the container"
ssh "$HOST" bash -s <<REMOTE_SCRIPT
set -euo pipefail
cd $REMOTE
chmod 600 migrate.db gateway.env
sudo docker rm -f cp-trial >/dev/null 2>&1 || true
sudo docker volume rm cp-trial >/dev/null 2>&1 || true
sudo docker compose build -q
UID_GW=\$(sudo docker run --rm claude-proxy:latest id -u gateway)
sudo docker volume create claude-gateway_data >/dev/null
sudo docker run --rm --user 0 -v claude-gateway_data:/data -v \$PWD:/src:ro claude-proxy:latest sh -c \
  "cp /src/config.toml /data/config.toml && cp /src/migrate.db /data/claude_proxy.db && rm -f /data/claude_proxy.db-wal /data/claude_proxy.db-shm && chown -R \$UID_GW /data && chmod 600 /data/claude_proxy.db"
rm -f migrate.db
sudo docker compose up -d
sleep 6
curl -fsS http://127.0.0.1:18480/health && echo
sudo docker compose exec -T gateway claude-proxy status | grep -v "HTTP Request"
REMOTE_SCRIPT

echo "5/6 retiring the local copy (a second refresher would break the box's login)"
mv "$SRC" "$SRC.migrated-$(date +%Y%m%d-%H%M%S)"

echo "6/6 done. Admin commands now run on the box:"
echo "   ssh $HOST 'cd $REMOTE && sudo docker compose exec gateway claude-proxy status'"
