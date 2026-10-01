#!/usr/bin/env bash
# A throwaway Lightsail Windows VM for hands-on gclaude testing (see docs/windows-test-vm.md).
#   scripts/windows-vm.sh up       create the VM if needed, open RDP to this computer, connect in Windows App
#   scripts/windows-vm.sh connect  connect again (refreshes the RDP firewall rule for this computer's address)
#   scripts/windows-vm.sh status   the VM's state and address, or that there is none
#   scripts/windows-vm.sh down     delete the VM (Lightsail charges a stopped one too)
# The Administrator password goes to the clipboard: Windows App on macOS can't take one from an .rdp file.
set -euo pipefail
NAME=${GCLAUDE_WIN_NAME:-gclaude-win}
REGION=${GCLAUDE_WIN_REGION:-us-east-2}
AZ=${REGION}a
RDP_FILE=${TMPDIR:-/tmp}/$NAME.rdp
ls_() { aws lightsail --region "$REGION" "$@"; }

# "none" only when Lightsail says there is no such VM; any other failure (expired login, throttling) stops the script
state() {
  local out
  if out=$(ls_ get-instance-state --instance-name "$NAME" --query state.name --output text 2>&1); then echo "$out"
  elif grep -q NotFoundException <<<"$out"; then echo none
  else echo "$out" >&2; return 1; fi
}

create() {
  echo "creating $NAME in $AZ (Windows Server 2025, small_win_3_0)"
  ls_ create-instances --instance-names "$NAME" --availability-zone "$AZ" \
    --blueprint-id windows_server_2025 --bundle-id small_win_3_0 --tags key=purpose,value=gclaude-testing >/dev/null
}

connect() {
  local s ip addr pass
  while s=$(state) || exit 1; [ "$s" != running ]; do
    case $s in
      none)             echo "$NAME doesn't exist; run: $0 up" >&2; exit 1 ;;
      stopped)          echo "starting $NAME"; ls_ start-instance --instance-name "$NAME" >/dev/null ;;
      pending|stopping) echo "waiting for $NAME to run ($s)" ;;
      *)                echo "$NAME is $s; can't connect" >&2; exit 1 ;;
    esac
    sleep 10
  done
  ip=$(curl -fsS https://api.ipify.org)
  ls_ put-instance-public-ports --instance-name "$NAME" \
    --port-infos "fromPort=3389,toPort=3389,protocol=tcp,cidrs=$ip/32" >/dev/null
  addr=$(ls_ get-instance --instance-name "$NAME" --query instance.publicIpAddress --output text)
  # The password appears a few minutes after the first boot
  until pass=$(ls_ get-instance-access-details --instance-name "$NAME" --protocol rdp \
                 --query accessDetails.password --output text 2>/dev/null) && [ -n "$pass" ] && [ "$pass" != None ]; do
    echo "waiting for Windows to set the Administrator password"; sleep 20
  done
  cat > "$RDP_FILE" <<RDP
full address:s:$addr
username:s:Administrator
screen mode id:i:1
dynamic resolution:i:1
audiomode:i:2
redirectclipboard:i:1
RDP
  printf %s "$pass" | pbcopy
  echo "$NAME at $addr (RDP open to $ip only); Administrator password copied to the clipboard"
  open -a "Windows App" "$RDP_FILE"
}

case ${1:-up} in up|connect|status|down) ;; *) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;; esac
s=$(state)
case ${1:-up} in
  up)      if [ "$s" = none ]; then create; fi; connect ;;
  connect) connect ;;
  status)  echo "$NAME: $s"
           [ "$s" = none ] || ls_ get-instance --instance-name "$NAME" --query instance.publicIpAddress --output text ;;
  down)    if [ "$s" = none ]; then echo "$NAME doesn't exist"
           else ls_ delete-instance --instance-name "$NAME" >/dev/null; rm -f "$RDP_FILE"
             # Lightsail bills until the VM is gone, so don't report success before it is
             for _ in $(seq 30); do s=$(state) || exit 1; [ "$s" = none ] && break; echo "waiting for $NAME to go ($s)"; sleep 10; done
             [ "$s" = none ] || { echo "$NAME still exists ($s) after 5 minutes: check the Lightsail console, it is still billed" >&2; exit 1; }
             echo "deleted $NAME"; fi ;;
esac
