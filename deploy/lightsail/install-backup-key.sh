#!/usr/bin/env bash
# One-time setup (rerun to update the backup script or rotate the SSH key) for the daily off-box backup,
# which runs in the private maparham/claude-proxy-backups repo.
# Run from the repo root:  deploy/lightsail/install-backup-key.sh ec2-user@<server-ip>
# Needs age-keygen on this machine (brew install age) the first time.
#
# Installs age and claude-gateway-backup on the box, adds a fresh backup key to authorized_keys that can
# run only that script, and stores BACKUP_SSH_KEY, BACKUP_KNOWN_HOSTS and BACKUP_HOST as secrets of the
# backups repo. The private SSH key goes straight from ssh-keygen to `gh secret set` and is not kept here.
#
# The first run (or one with --new-age-key) also makes the age key pair. The private key goes to the
# backups repo as AGE_KEY and is printed once, for your password manager; only the public key goes to
# the box. A new age key can't decrypt older copies, so don't rotate it without keeping the old one.
set -euo pipefail
HOST=${1:?usage: $0 user@host [--new-age-key]}
NEW_AGE_KEY=${2:-}
REPO=maparham/claude-proxy-backups
MARK=claude-gateway-backup@github-actions
RECIPIENTS=/usr/local/etc/claude-gateway-backup.age.pub
AGE_URL=https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz
AGE_SHA256=cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10

KNOWN=$(ssh-keygen -F "${HOST#*@}" | grep -v '^#') || { echo "${HOST#*@} is not in known_hosts; ssh to it once first"; exit 1; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT; chmod 700 "$TMP"
ssh-keygen -q -t ed25519 -N '' -C "$MARK" -f "$TMP/key"

echo "1/4 installing age and the backup script"
scp -q deploy/lightsail/claude-gateway-backup "$HOST:claude-gateway/deploy/lightsail/"
ssh "$HOST" "set -e; sudo install -o root -g root -m 755 claude-gateway/deploy/lightsail/claude-gateway-backup /usr/local/bin/
  if ! /usr/local/bin/age --version 2>/dev/null | grep -qx v1.3.2; then
    t=\$(mktemp -d); curl -fsSL -o \$t/age.tgz $AGE_URL
    echo '$AGE_SHA256  '\$t/age.tgz | sha256sum -c --quiet
    tar -xzf \$t/age.tgz -C \$t; sudo install -o root -g root -m 755 \$t/age/age /usr/local/bin/age; rm -rf \$t
  fi"

echo "2/4 age key pair"
if [[ $NEW_AGE_KEY == --new-age-key ]] || ! ssh "$HOST" "test -s $RECIPIENTS"; then
  age-keygen -o "$TMP/age.key" 2>/dev/null
  gh secret set AGE_KEY -R "$REPO" <"$TMP/age.key"   # first, so a run that fails here leaves the box's key alone
  age-keygen -y "$TMP/age.key" | ssh "$HOST" "sudo install -d -m 755 ${RECIPIENTS%/*} && sudo tee $RECIPIENTS >/dev/null"
  echo "   new age key. Save this private key in your password manager; it is shown only now:"
  grep '^AGE-SECRET-KEY-' "$TMP/age.key"
else
  echo "   keeping the box's age public key"
fi

echo "3/4 replacing the backup key in authorized_keys"
ssh "$HOST" "set -e; f=~/.ssh/authorized_keys; cp -p \$f \$f.bak-backup; grep -v ' $MARK\$' \$f.bak-backup > \$f || true
  echo 'command=\"/usr/local/bin/claude-gateway-backup\",restrict $(cat "$TMP/key.pub")' >> \$f"

echo "4/4 setting secrets in $REPO"
gh secret set BACKUP_SSH_KEY -R "$REPO" <"$TMP/key"
gh secret set BACKUP_KNOWN_HOSTS -R "$REPO" --body "$KNOWN"
gh secret set BACKUP_HOST -R "$REPO" --body "$HOST"
echo "done. 'Run workflow' on the backup workflow in $REPO takes a copy now."
