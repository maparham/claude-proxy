# Deploying on a shared Lightsail host behind a Cloudflare tunnel

From the repo root on the Mac that currently runs the gateway (`<server-ip>` here and below is the
instance's public address, shown in the Lightsail console):

```sh
deploy/lightsail/migrate-from-mac.sh ec2-user@<server-ip>     # moves grant + data, starts the container
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

`.github/workflows/monitor.yml` checks `https://claude.<domain>/health` hourly from outside and fails
(GitHub then emails you) if the gateway is unreachable or `"credential"` is false, i.e. the
subscription login needs `claude-proxy login`. "Run workflow" on it checks on demand.

It also fails when `"usage_fresh"` is false: Anthropic's newest 5-hour or weekly usage figure is older
than the longer of `[quota] stale_after_s` and `poll_max_backoff_s` plus 10 minutes (40 minutes by
default). Until fresh figures arrive, ticket holders are limited by estimates (their tokens at the
account's usual rate) and hand-set share limits are skipped, so nothing breaks, but limits are looser.
When it fires:

1. `docker compose exec gateway claude-proxy status` shows each bucket's last figure and its age, and
   whether the credential works. A credential that is NOT OK stops the figures too; fix it with
   `docker compose exec -it gateway claude-proxy login`.
2. With the credential OK, check `docker compose logs --since 2h gateway` for `usage poll` lines: a
   rate-limited poll backs off and recovers by itself; repeated failures usually mean Anthropic is
   having an incident (see its status page), and the figures return with the first response or poll
   after it ends.
3. Nothing to do on the gateway if Anthropic is down; re-run the workflow once figures are fresh again.

One-time setup, and again after changing `claude-gateway-deploy` or `docker-compose.yml`, or to rotate
the key:

```sh
deploy/lightsail/install-deploy-key.sh ec2-user@<server-ip>
```

Paid tickets and rollback: switching `[tickets]` off in `config.toml` does not free ticket users; they get
"Tickets are paused; ask the admin." until each is ungated. The Users page marks them "tickets paused";
Ungate there works with tickets off, cancels any remaining tickets in the same step, and hands them back to
hand-set limits. Rolling the image back to a build from before tickets is different: that code
ignores ticket rows, and a ticket user's sign-up credit is already gone, so they would have no limit at all.
Ungate them and set their limits first.

Roll back by hand: `ssh ec2-user@<server-ip>`, `cd claude-gateway/deploy/lightsail`, list tags with
`docker image ls claude-proxy`, write the one you want to `.env` as `GATEWAY_TAG=<tag>`, then run
`docker compose up -d --no-build`. `update.sh` still ships the local checkout, built on the box as
`claude-proxy:manual-<time>`.

Details:

- `docker compose up -d --no-build` from this directory runs the image named by `GATEWAY_TAG` in `.env`
  (build by hand with `update.sh`, not `--build`, which would overwrite that tag), with `gateway.env` (mode 600) holding
  `CLAUDE_PROXY_CREDENTIAL_KEY` and optionally `META_API_KEY`. `config.toml` is copied into the `data`
  volume as `/data/config.toml`.
- Order requests (see the main README): `SMTP_PASSWORD` in `gateway.env` when `[email]` has an `smtp_user`, and
  `TURNSTILE_SECRET` when `[tickets]` has a `turnstile_site_key` (the site key, made in the Cloudflare dashboard under
  Turnstile for the dashboard's hostname, goes in `config.toml`). Recreate the container after changing `gateway.env`.
  Visitors may place 3 orders a day per IP address. The gateway sees the address of whatever connects to it and trusts
  `X-Forwarded-For` only from `127.0.0.1`; behind the tunnel and Docker's port mapping, or for visitors sharing an
  address (CGNAT, an office), many visitors can look like one, and that limit then blocks them together. They can still
  sign in to order. The `ip` column of the `orders` table shows what the gateway saw.
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

## Off-box backups

The copies in `/data/backups` die with the box, so a daily job in the private
[maparham/claude-proxy-backups](https://github.com/maparham/claude-proxy-backups) repo pulls one more:

1. At 03:23 UTC Actions SSHes in with a key that can run only `claude-gateway-backup`, which takes a
   consistent copy with SQLite's backup API, gzips it and encrypts it with `age` to the public key in
   `/usr/local/etc/claude-gateway-backup.age.pub`, all before it leaves the box. The box has no private
   key and no credential for the backups repo, and the caller can't pick the recipient.
2. The job decrypts the copy with the `AGE_KEY` secret and runs `PRAGMA integrity_check`, checks there
   are users and that the newest request or quota reading is under 24 h old. Only a copy that passes is
   stored, as a release asset `claude_proxy-<UTC time>.db.gz.age`, then read back and compared.
3. The newest 30 releases are kept. Older ones are deleted only after a good copy is stored, so failing
   backups never eat the good ones.

A failed run (box unreachable, no copy, won't decrypt, corrupt, no users, stale) makes GitHub email you.
"Run workflow" there takes a copy now. The whole thing is about 470 KB and a minute of Actions a day.

One-time setup, and again after changing `claude-gateway-backup` or to rotate the SSH key:

```sh
brew install age                                        # age-keygen, the first time only
deploy/lightsail/install-backup-key.sh ec2-user@<server-ip>
```

It installs `age` (pinned, checksum-verified) and the script on the box, locks the new key to that script
in `authorized_keys`, and sets `BACKUP_SSH_KEY`, `BACKUP_KNOWN_HOSTS`, `BACKUP_HOST` in the backups repo.
The first run also makes the age key pair: the private key goes to the `AGE_KEY` secret and is printed
once for your password manager, and is kept nowhere else. Reruns keep it; `--new-age-key` replaces it,
after which older copies need the old key. Keep `CLAUDE_PROXY_CREDENTIAL_KEY` from `gateway.env` in the
password manager too: the subscription grant in the database is encrypted with it, and it isn't backed up.

### Restore

On the Mac, with the age private key from the password manager:

```sh
cd "$(mktemp -d)"
gh release download -R maparham/claude-proxy-backups           # newest copy; add a tag (db-<time>) for an older one
age -d -i - claude_proxy-*.db.gz.age | gunzip >claude_proxy.db   # paste the AGE-SECRET-KEY-... line, then Ctrl-D
python3 -c 'import sqlite3; c = sqlite3.connect("claude_proxy.db"); print(c.execute("PRAGMA integrity_check").fetchone(), c.execute("SELECT count(*) FROM users").fetchone())'
scp claude_proxy.db ec2-user@<server-ip>:claude-gateway/restore.db && rm claude_proxy.db*
```

On the box (a new one is first set up as at the top, with the same `CLAUDE_PROXY_CREDENTIAL_KEY` in
`gateway.env`), put it in the volume as the container's user, moving the current database and its WAL
files aside:

```sh
cd ~/claude-gateway/deploy/lightsail
docker compose stop gateway
docker run --rm -i --network none -v claude-gateway_data:/data "claude-proxy:$(sed -n 's/^GATEWAY_TAG=//p' .env)" sh -c '
  cd /data && mkdir -p backups && t=$(date -u +%Y%m%dT%H%M%SZ)
  for f in claude_proxy.db claude_proxy.db-wal claude_proxy.db-shm; do [ ! -e $f ] || mv $f backups/before-restore-$t.$f; done
  cat >claude_proxy.db && chmod 600 claude_proxy.db' <../../restore.db
rm ../../restore.db
docker compose up -d --no-build
docker compose exec gateway claude-proxy status
```

If `status` shows the credential broken, the refresh token rotated after the copy was taken (or the
credential key differs): run `docker compose exec -it gateway claude-proxy login`. Everything written
since the copy (new sign-ups, keys, usage) is lost.
