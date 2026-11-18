# Managed-service profile

Deployed on a DigitalOcean Droplet. This directory is the harness: a Compose file, an edge config, replication, backups. The
operations handbook, with the service inventory, rebuild steps, secrets and incident table, is
`docs/ops/README.md` (the operator's handbook, not in the public repo); the architecture is in [`docs/CLOUD.md`](../../docs/CLOUD.md).

| File | Purpose |
|---|---|
| `compose.yaml` | dayfile (one worker), `web` (the site, no tenant files), Caddy, optional Litestream (`--profile replicate`) |
| `Caddyfile` | on-demand TLS per tenant subdomain, `/internal/*` blocked, no access log; `dayfile.app` is proxied to the `web` container |
| `litestream.yml` | continuous replication of every `tenants/<slug>/dayfile.db` |
| `backup-tenants.sh` | nightly: the container writes verified, gzipped copies plus each `tenant.json` into a staging directory; the host uploads them with rclone and prunes old ones |
| `setup-backup.sh` | one-time: stores the Spaces key and encryption password and salt in `/etc/dayfile/rclone.conf` (root only) from hidden prompts |
| `systemd/` | `dayfile-backup.service` and `.timer`: run the backup nightly at about 03:17 with catch-up after downtime; `dayfile-heartbeat.service` and `.timer`: every minute, write the events for sources that went quiet; `dayfile-purge.service` and `.timer`: weekly, remove deleted logs from `.trash/` after 30 days |
| `.env.example` | every setting, no secrets |
| `cloud-init.yaml` | paste as the Droplet's user data: hardened, swap, Docker, firewall, updates (no repo checkout) |
| `setup-github.sh`, `setup-uptimerobot.sh`, `set-ping-urls.sh` | one-time or rotating secrets, each from hidden prompts: the GitHub client secret, the UptimeRobot read-only key (for `/status`), the healthchecks.io ping URLs |
| `restore-drill.sh` | restore the newest backup on your own machine and check it |
| `smoke-test.sh` | after first boot: tenant, tokens, write, read, isolation, edge checks, then cleans up |

## First boot (when you decide to deploy)

0. Create the Droplet with `cloud-init.yaml` as user data (put your SSH public key in it first). It prepares
   `/srv/dayfile/tenants`. Then clone the repo with a read-only GitHub deploy key (the repo is private).
1. DNS: `A dayfile.cloud` and `A *.dayfile.cloud` → the server's (reserved) IP, plus `CAA dayfile.cloud 0 issue "letsencrypt.org"`. `A dayfile.app` → the same IP: Caddy proxies it to the `web` container (the site and log viewer), with its own certificate and outside the on-demand hook. Firewall: 22, 80, 443 only.
2. `cp .env.example .env && chmod 600 .env`, fill it in.
3. `docker compose up -d`, then `docker compose exec dayfile dayfile tenant create <your-own-slug>`: dogfood first.
4. Backups, once (as root on the host): `install -d -o 10001 -g 10001 -m 700 /srv/dayfile/backups`; run
   `setup-backup.sh <bucket> <region>` yourself; copy `systemd/*` to `/etc/systemd/system/`, then
   `systemctl daemon-reload && systemctl enable --now dayfile-backup.timer dayfile-purge.timer dayfile-heartbeat.timer`. Test with `systemctl start dayfile-backup.service`
   and `journalctl -u dayfile-backup`. Optional `/etc/dayfile/backup.env` (written by `set-ping-urls.sh`) holds `BACKUP_PING_URL` (the script also pings `<URL>/fail` on failure); the heartbeat unit reads `/etc/dayfile/heartbeat.env` for `HEARTBEAT_PING_URL`.
5. Add `--profile replicate` once anyone else's data is on the box.

## Runbooks

**Restore one tenant from a nightly copy.** `rclone copy remote:dayfile/<stamp>/<slug>-<stamp>.db.gz .`, `gunzip`,
stop writes by `dayfile tenant suspend <slug>`, replace `tenants/<slug>/dayfile.db` (delete any `-wal`/`-shm` beside
it), `dayfile tenant resume <slug>`. Check with `dayfile tenant show <slug>`.

**Restore from Litestream.** `litestream restore -o /tmp/<slug>.db <replica-url>/tenants/<slug>/dayfile.db`, then as above.

**Suspend an abusive tenant.** `dayfile tenant suspend <slug>`: effective on the next request, no restart, data kept.

**Disk filling.** `dayfile tenant usage` to find large tenants; lower their `--max-events`; grow the volume; do not
delete files under `tenants/` by hand.

**Upgrade.** Take a backup first (the schema upgrade runs when each tenant file is next opened). Deploy to your own
tenant first, watch `/healthz`, then roll everyone.

**Delete a customer.** `dayfile tenant delete <slug> --yes`, then remove the `.trash` copy (account deletion from the web app erases the files at once), their nightly copies and
their Litestream prefix on a schedule you state in your terms.

## After first boot

`./smoke-test.sh dayfile.cloud` runs from the host (needs `dayfile` installed and `DAYFILE_TENANTS_DIR` readable). It creates a
throwaway tenant, mints tokens, writes and reads an event over HTTPS, checks that another tenant's token fails, that an
unknown subdomain is 404, that `/internal/tls-ask` is not reachable from outside, and that plain HTTP is redirected,
then moves the throwaway tenant to `.trash`. A non-zero exit means do not announce the service.
