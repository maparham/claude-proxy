#!/usr/bin/env bash
# Add the gateway's two hostnames to the box's existing cloudflared tunnel and restart it.
# Run from the repo root:  deploy/lightsail/apply-tunnel.sh ec2-user@<server-ip> rahkar.pro
# The tunnel's other hostnames drop for a few seconds during the restart.
set -euo pipefail
HOST=${1:?usage: $0 user@host domain}
DOMAIN=${2:?usage: $0 user@host domain}
ssh "$HOST" bash -s <<REMOTE_SCRIPT
set -euo pipefail
CFG=/etc/cloudflared/config.yml
if sudo grep -q "claude.$DOMAIN" \$CFG; then echo "already configured"; else
  sudo cp -p \$CFG \$CFG.bak-before-claude
  sudo python3 - <<PY
p = "\$CFG"
s = open(p).read()
old = "  - service: http_status:404"
assert s.count(old) == 1, "catch-all rule not found"
new = ("  - hostname: claude.$DOMAIN\n    service: http://127.0.0.1:18480\n"
       "  - hostname: claude-dash.$DOMAIN\n    service: http://127.0.0.1:18481\n" + old)
open(p, "w").write(s.replace(old, new))
PY
fi
sudo cloudflared tunnel --config \$CFG ingress validate | tail -1
UNIT=\$(systemctl list-units --type=service --all | grep -o "cloudflared[^ ]*\.service" | head -1)
sudo systemctl restart "\$UNIT"
sleep 5
systemctl is-active "\$UNIT"
REMOTE_SCRIPT
echo "Tunnel updated. DNS: proxied CNAMEs claude and claude-dash -> <tunnel-id>.cfargotunnel.com"
