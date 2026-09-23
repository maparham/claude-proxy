# Deploying on a shared Lightsail host behind a Cloudflare tunnel

From the repo root on the Mac that currently runs the gateway:

```sh
deploy/lightsail/migrate-from-mac.sh ec2-user@3.139.146.5     # moves grant + data, starts the container
deploy/lightsail/apply-tunnel.sh ec2-user@3.139.146.5 rahkar.pro
```

Then add two proxied CNAMEs in Cloudflare (`claude`, `claude-dash` -> `<tunnel-id>.cfargotunnel.com`).

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
