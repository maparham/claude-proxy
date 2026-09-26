#!/usr/bin/env bash
# One-time setup (rerun to update the deploy script or rotate the key) for the GitHub Actions deploy.
# Run from the repo root:  deploy/lightsail/install-deploy-key.sh ec2-user@<server-ip>
#
# Installs claude-gateway-deploy and the compose file on the box, adds a fresh deploy key to
# authorized_keys that can run only that script, and stores DEPLOY_SSH_KEY, DEPLOY_KNOWN_HOSTS and
# DEPLOY_HOST as repository secrets. The private key goes straight from ssh-keygen to `gh secret set`
# and is not kept on this machine. The box's host key is taken from this machine's known_hosts, which
# already trusts it.
set -euo pipefail
HOST=${1:?usage: $0 user@host}
REMOTE=claude-gateway/deploy/lightsail
MARK=claude-gateway-deploy@github-actions

KNOWN=$(ssh-keygen -F "${HOST#*@}" | grep -v '^#') || { echo "${HOST#*@} is not in known_hosts; ssh to it once first"; exit 1; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT; chmod 700 "$TMP"
ssh-keygen -q -t ed25519 -N '' -C "$MARK" -f "$TMP/key"

echo "1/3 installing the deploy script and compose file"
scp -q deploy/lightsail/claude-gateway-deploy deploy/lightsail/docker-compose.yml "$HOST:$REMOTE/"
ssh "$HOST" "sudo install -o root -g root -m 755 $REMOTE/claude-gateway-deploy /usr/local/bin/claude-gateway-deploy"

echo "2/3 replacing the deploy key in authorized_keys"
ssh "$HOST" "set -e; f=~/.ssh/authorized_keys; cp -p \$f \$f.bak-deploy; grep -v ' $MARK\$' \$f.bak-deploy > \$f || true
  echo 'command=\"/usr/local/bin/claude-gateway-deploy\",restrict $(cat "$TMP/key.pub")' >> \$f"

echo "3/3 setting repository secrets"
gh secret set DEPLOY_SSH_KEY <"$TMP/key"
gh secret set DEPLOY_KNOWN_HOSTS --body "$KNOWN"
gh secret set DEPLOY_HOST --body "$HOST"
echo "done. The next green push to master deploys; 'Run workflow' on the deploy workflow redeploys."
