# A Windows VM for hands-on testing

CI covers the Windows client, but only a person at a real Windows desktop sees how gclaude renders in Windows
Terminal, PowerShell and cmd (fonts, glyphs, colors). Running Windows locally doesn't fit a Mac with little free
RAM, and `pwsh` on macOS renders through the Mac's terminal, so this is a Lightsail VM reached over RDP.

## The script

`scripts/windows-vm.sh up` creates the VM below (or reuses it), opens RDP to this computer's address only, waits
for the Administrator password, copies it to the clipboard and opens the connection in Windows App with the address
and user filled in: paste the password at the prompt. The first sign-in takes a minute or two to reach the desktop.
`connect` reconnects (and follows a changed address), `status` shows the VM, `down` deletes it.

The sections below are the same steps by hand.

## Create

Windows Server 2025 ships Windows Terminal, and its license is included in the price. The `small_win_3_0` plan
(2 GB, about $22 a month, charged by the hour) is enough. The commands use the AWS CLI's own account and region.

```sh
IP=$(curl -s https://api.ipify.org)
aws lightsail create-instances --instance-names gclaude-win --availability-zone us-east-2a \
  --blueprint-id windows_server_2025 --bundle-id small_win_3_0 --tags key=purpose,value=gclaude-testing
until [ "$(aws lightsail get-instance-state --instance-name gclaude-win --query state.name --output text)" = running ]; do sleep 10; done
# RDP from this computer's address only
aws lightsail put-instance-public-ports --instance-name gclaude-win \
  --port-infos fromPort=3389,toPort=3389,protocol=tcp,cidrs=$IP/32
aws lightsail get-instance --instance-name gclaude-win --query instance.publicIpAddress --output text
# The Administrator password shows up a few minutes after the instance runs; until then this prints nothing
aws lightsail get-instance-access-details --instance-name gclaude-win --protocol rdp \
  --query "accessDetails.[username,password]" --output text
```

Each new instance has a new address and password.

## Connect

Microsoft's **Windows App** (Mac App Store): **+** > **Add PC**, the instance's address, user `Administrator` and
the password above. Error `0x204` means the address is out of date or this computer's address changed: rerun
`put-instance-public-ports` with the new `$IP`.

On the VM, Claude Code comes first (`irm https://claude.ai/install.ps1 | iex`, then a new terminal), then the
dashboard's `irm https://<dashboard>/install.ps1 | iex`.

## Delete

Lightsail charges a stopped instance too, so delete it after a session:

```sh
aws lightsail delete-instance --instance-name gclaude-win
```
