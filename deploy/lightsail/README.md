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

## Automatic deploys

Every push to `master` whose tests pass goes live (`.github/workflows/deploy.yml`). "Run workflow" on
that workflow redeploys `master`. Actions builds the image, streams it with `config.toml` over SSH, and
`claude-gateway-deploy` on the box does the rest:

1. accepts the image only if it is tagged exactly `claude-proxy:<commit sha>`
2. backs up the database and config to `/data/backups/<time>.*` (the last 5 are kept)
3. copies `config.toml` from the repo into the volume, so the repo is the source of truth for config
4. stops the old container (10 s for in-flight streams) and starts the new one
5. requires `/health` and `credential: OK` from `claude-proxy status`, otherwise puts the previous image
   and config back and fails the workflow run
6. keeps the 3 newest images for rollback

A deploy never touches `gateway.env`, the grant or the rest of the data volume. The database is never
restored automatically, because a copy from before the deploy may hold a refresh token that has since
rotated. The deploy key in `authorized_keys` can run only `claude-gateway-deploy`. Rollback puts the old
code on whatever schema the new code left behind; migrations in `db.py` are additive, but one that
isn't can make the rollback fail too, and then the backup in `/data/backups` is the way back.

One-time setup, and again after changing `claude-gateway-deploy` or `docker-compose.yml`, or to rotate
the key:

```sh
deploy/lightsail/install-deploy-key.sh ec2-user@3.139.146.5
```

Roll back by hand: `ssh ec2-user@3.139.146.5`, `cd claude-gateway/deploy/lightsail`, list tags with
`docker image ls claude-proxy`, write the one you want to `.env` as `GATEWAY_TAG=<tag>`, then run
`docker compose up -d --no-build`. `update.sh` still ships the local checkout, built on the box as
`claude-proxy:manual-<time>`.

Details:

- `docker compose up -d --no-build` from this directory runs the image named by `GATEWAY_TAG` in `.env`
  (build by hand with `update.sh`, not `--build`, which would overwrite that tag), with `gateway.env` (mode 600) holding
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
