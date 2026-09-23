# Deploying on a shared Lightsail host behind a Cloudflare tunnel

From the repo root on the Mac that currently runs the gateway:

```sh
deploy/lightsail/migrate-from-mac.sh ec2-user@3.139.146.5     # moves grant + data, starts the container
```

Then publish the two listeners through the tunnel. The `aws-vps` tunnel on this box is **managed from
the Cloudflare dashboard**, so its local `config.yml` ingress is ignored: add them under Networking →
Tunnels → aws-vps → Routes → Add route → Published application:

| Hostname | Service URL |
|---|---|
| `claude.rahkar.pro` | `http://127.0.0.1:18480` |
| `claude-dash.rahkar.pro` | `http://127.0.0.1:18481` |

(`apply-tunnel.sh` is for a tunnel run from a local config file.)

Live since 2026-09-23: `ANTHROPIC_BASE_URL=https://claude.rahkar.pro`, dashboard at
`https://claude-dash.rahkar.pro/dashboard`. Streams of over 90 s pass through Cloudflare intact, and the
dashboard sees real client IPs.

Details:

- `docker compose up -d --build` from this directory, with `gateway.env` (mode 600) holding
  `CLAUDE_PROXY_CREDENTIAL_KEY` and optionally `META_API_KEY`. `config.toml` is copied into the `data`
  volume as `/data/config.toml`.
- cloudflared ingress (above the catch-all `http_status:404` rule):
  ```yaml
  - hostname: claude.<domain>
    service: http://127.0.0.1:18480
  - hostname: claude-dash.<domain>
    service: http://127.0.0.1:18481
  ```
  plus a proxied CNAME for each hostname to `<tunnel-id>.cfargotunnel.com`.
- Admin commands run in the container: `docker compose exec gateway claude-proxy status`,
  `... user add <name>`, `... limit set ...`.
- Only one process may hold the subscription grant. When moving it from another host, stop that
  gateway first, copy the database with `sqlite3 <db> ".backup <copy>"`, and retire the old copy.
