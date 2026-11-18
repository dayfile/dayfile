# Design: monitoring and alerts

Status: only `/healthz` and `dayfile tenant usage` exist. DigitalOcean's free agent metrics are available on the Droplet
(the operator ticked monitoring) but no alert policies are set.

## Principles

- Alerts must reach the operator through a channel that does not depend on dayfile itself.
- Few alerts, each actionable. The Droplet is small (1 GiB RAM, ~418 MB free, 1 GiB swap), so memory is the first limit
  to watch.
- Nothing in monitoring may carry tokens or event content.

## Signals and where each lives

| Signal | How | Threshold | Action |
|---|---|---|---|
| Site up | external HTTP probe of `https://dayfile.app/healthz` | 2 failures in a row | check `docker compose ps`, logs |
| API up | probe `https://dayfile.cloud/status` (needs no particular log; a probe of a real log's `/healthz` would also prove on-demand TLS and routing) | 2 failures | same |
| Certificate expiry | probe warns when the cert has under 14 days left | 14 d | Caddy renews at 30 d; if it did not, check ACME logs |
| Backup ran | dead-man's-switch ping after each successful run (`BACKUP_PING_URL`), `/fail` ping after a failed one | `/fail` at once, or no ping for 27 h |
| Heartbeat checker ran | ping after each good run of `dayfile-heartbeat.timer` (`HEARTBEAT_PING_URL`) | no ping for 5 min | `systemctl status dayfile-heartbeat`, journal | run `systemctl status dayfile-backup`, then the drill |
| Droplet CPU | DO alert policy | above 80% for 10 min | look for abuse; upsize |
| Droplet memory | DO alert policy | above 85% for 10 min | containers have `mem_limit`; consider the next plan size |
| Disk | DO alert policy | above 75% | prune images/logs; check tenant growth with `tenant usage` |
| Droplet down or no metrics | DO alert policy "no data" if offered, else covered by the external probes | | |
| Container restarts | `docker compose ps` shows restarts; check weekly until a probe covers it | | |

## Channels

1. **ntfy** on a private random topic at `ntfy.sh` (or self-hosted later): the operator already uses ntfy for dayfile's own
   notifier, so a phone app is in place. The external probe and the dead-man's-switch both support webhooks; DO alert
   policies support email and Slack only, so those go to email.
2. Email to `security@`/`team@dayfile.app` (forwarded, see `dns-and-mail.md`) as the second route.

Using the product's own ntfy notifier for these would make the alert depend on the thing that failed, so it is not used.

## Tools (pick one probe provider)

| Option | Fit |
|---|---|
| Healthchecks.io (hosted free tier or self-hosted) | best for the backup dead-man's-switch; also does simple pings |
| UptimeRobot or Better Stack free tier | HTTP probes with a phone push |
| A cron on the operator's own machine | free, but only runs when it is on |

Recommendation: Healthchecks.io for backups, one hosted uptime provider for the two HTTP probes and cert expiry. Both free
tiers are enough. **[decide]** provider; neither needs access to the server.

## Host hygiene that makes alerts meaningful

- Docker log rotation: set `logging: {driver: json-file, options: {max-size: 10m, max-file: "3"}}` on every service so logs
  cannot fill the disk (the apps log little by design; Caddy discards access logs).
- `journald` size cap (`SystemMaxUse=200M`).
- `restart: unless-stopped` is already set on all services. Healthchecks do not restart containers by themselves; a
  container stuck "unhealthy" shows up through the probes.
- `unattended-upgrades` is on; enable automatic reboot at a fixed quiet hour with a probe to confirm it came back
  (**[decide]**, the service is single-node so a reboot is a minute of downtime).

## Later (not needed yet)

- A `/internal/metrics` endpoint with request counts and 4xx/5xx rates from the process, scraped by nothing for now.
- Per-tenant usage in a dashboard for the operator (from `tenant usage`, which is already JSON).
- A status page on `dayfile.app/status` once there are outside users.

## Inputs from the operator

- Create the alert policies in the DO console (steps listed above) with their email.
- Choose probe and heartbeat providers; give me the heartbeat URL via `backup.env`, never in chat.
- Install ntfy on the phone and choose a topic name that is hard to guess.

## Acceptance

Each alert is triggered once on purpose (stop the container, withhold the ping, fill a temp file) and reaches the phone.
The checks and thresholds are recorded in `docs/ops/runbook.md` with the first-response step per alert.
