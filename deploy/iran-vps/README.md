# The Iranian VPS (`gateway-ir`)

An ArvanCloud VPS in Karaj (`95.38.187.63`, Debian 13, 1 vCPU, 2 GB RAM; `ssh gateway-ir` /
`ssh iran-vps.rahkar.pro`, as root). It runs no gateway code: it is the way in for users in Iran, and passes
everything to the gateway on the AWS box (`ec2-user@3.18.205.89`, see `../lightsail/README.md`) through a private
Xray tunnel. From Iran, Cloudflare freezes larger downloads partway; this path does not
(`docs/superpowers/specs/2026-11-14-iran-hosting-design.md`).

| Name (DNS only in Cloudflare) | What Caddy does | Reaches on the AWS box |
|---|---|---|
| `claude-ir.rahkar.pro` | API relay; gclaude's fallback (`[listener] relay_url`) | `127.0.0.1:18480` (API) |
| `rahkar.pro`, `dash.rahkar.pro` | home page and dashboard, for everyone; Clerk at `/__clerk` | `127.0.0.1:18481` (dashboard) |
| `www.rahkar.pro` | redirect to `rahkar.pro` | — |

`claude.rahkar.pro` (the API) and `hooks.rahkar.pro` (Telegram's webhook; Telegram cannot reach Iran) stay on
the Cloudflare tunnel straight to the AWS box.

## How a request travels

```
browser ──https──▶ Caddy (VPS :443, Let's Encrypt)
                   └─▶ Xray dokodemo-door 127.0.0.1:18480 / :18481 (VPS)
                       └─▶ VLESS + Reality, TCP 443 ──▶ Xray (AWS box :443)
                                                         └─▶ 127.0.0.1:18480 / :18481, nothing else
```

- **Reality** makes the tunnel look like a TLS connection to `www.microsoft.com`, which Iran's filtering leaves alone.
- The AWS box's Lightsail firewall opens 443 **only to `95.38.187.63/32`** (SSH is the only other open port).
  A new VPS address must be put there.
- Xray on the AWS box forwards only to `127.0.0.1:18480` and `:18481` and blocks everything else
  (`finalRules` plus routing rules), so the tunnel's key reaches nothing but the gateway.
- Caddy sets `X-Forwarded-For`/`-Proto`; the gateway trusts them from `127.0.0.1` and records users' real IPs.

## Files here

Templates of what runs on the servers. **Secrets are placeholders** and live only on the servers:
`<PRIVATEKEY>`, `<PUBLICKEY>`, `<CLIENT_UUID>`, `<SHORT_ID>`, `<AWS_GATEWAY_IP>`, `<WSTUNNEL_PATH>`.

| File | Goes to |
|---|---|
| `Caddyfile` | VPS `/etc/caddy/Caddyfile` |
| `xray-vps.json` | VPS `/usr/local/etc/xray/config.json` |
| `xray-aws.json` | AWS box `/usr/local/etc/xray/config.json` |
| `systemd/xray-vps.service`, `systemd/xray-aws.service` | `/etc/systemd/system/xray.service` on each |
| `systemd/iranbrowser-*.service` | VPS `/etc/systemd/system/` (test browser, below) |

The Caddyfile also serves things that are not the gateway's: a WireGuard-over-WebSocket endpoint (wstunnel, on
the secret `<WSTUNNEL_PATH>`) and the wg-easy admin UI on `:51821`; both run in Docker on the VPS.

## Setting up a new VPS

1. Debian 13. `apt install caddy` (Debian's package is fine).
2. Xray: download the Linux amd64 release from github.com/XTLS/Xray-core to `/usr/local/bin/xray` (the same
   binary on both ends; `xray version`). Install `systemd/xray-vps.service` as `xray.service`.
3. Keys, made once for the pair (`xray uuid`, `xray x25519`, `openssl rand -hex 8`):
   - AWS `xray-aws.json`: `<CLIENT_UUID>`, `<PRIVATEKEY>` (x25519 "PrivateKey"), `<SHORT_ID>`.
   - VPS `xray-vps.json`: the same `<CLIENT_UUID>` and `<SHORT_ID>`, `<PUBLICKEY>` (x25519
     "Password (PublicKey)"), `<AWS_GATEWAY_IP>` = `3.18.205.89`.
   - Check each with `xray run -test -c /usr/local/etc/xray/config.json`, then `systemctl enable --now xray`.
4. Lightsail → `claude-gateway` → Networking: TCP 443 from the new VPS's address only.
5. Copy `Caddyfile` (drop or fill the wstunnel/wg-easy parts), `caddy validate --config /etc/caddy/Caddyfile`.
6. Point the DNS records above at the new address (DNS only, TTL 1 min), then `systemctl reload caddy`. Caddy
   gets certificates within a minute; load it only after DNS points here, or Let's Encrypt's failed-validation
   limit can bite.
7. Check: `curl https://claude-ir.rahkar.pro/health`, `https://rahkar.pro/`,
   `https://dash.rahkar.pro/__clerk/v1/environment` (JSON with `auth_config`).

## Rotating the tunnel key

Without printing the private key (last done 2026-11-14):

1. On the AWS box: `xray x25519`; put "PrivateKey" into the inbound's `realitySettings.privateKey`.
2. On the VPS: put "Password (PublicKey)" into the outbound's `realitySettings.publicKey`.
3. `xray run -test -c …` on both, `systemctl restart xray` on both (a few seconds without the relay and the site).
4. Check `curl https://claude-ir.rahkar.pro/health` and `https://dash.rahkar.pro/dashboard`. Delete any backup copy
   holding the old key.

The UUID and short ID can be rotated the same way (`xray uuid`; both ends must match).

## Test browser from Iran

Chromium on a virtual display, viewable over noVNC, to see the site as people in Iran do (`systemd/iranbrowser-*`,
user `browser`, Debian packages `chromium xvfb x11vnc novnc python3-websockify openbox fonts-noto-core`). Everything
listens on the VPS's localhost only. **Off by default** (it uses ~300 MB):

```sh
systemctl enable --now iranbrowser-xvfb iranbrowser-wm iranbrowser-vnc iranbrowser-chromium   # on
ssh -N -L 6080:127.0.0.1:6080 -L 9222:127.0.0.1:9222 gateway-ir                             # on the Mac
# http://localhost:6080/vnc.html to watch; Playwright can attach to http://localhost:9222 (CDP)
systemctl disable --now iranbrowser-chromium iranbrowser-vnc iranbrowser-wm iranbrowser-xvfb  # off
```

On this 2 GB box the first `apt install` of these was killed for memory; running it again worked.

## Rollback of the site move

See `../lightsail/README.md`, "rahkar.pro and the dashboard from the Iranian VPS": three DNS records go back to the
Cloudflare tunnel. The VPS's Caddyfile from before the move is `/etc/caddy/Caddyfile.before-iran-hosting`.
