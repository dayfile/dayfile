# Launch checklist

What is done, and what is waiting on the operator. Updated 2026-11-18.

## Done

- Open signup with a beta notice, storage quota (25 MiB) and event quota (50,000) per log, 60 new accounts an hour.
- Backups: nightly encrypted copy off the Droplet (the logs and the accounts database), `verify-backup`, a passing restore drill, weekly purge of deleted logs.
- Export and delete from Settings; terms and privacy drafts match the behavior.
- `security.txt`, `www` redirect, gzip, week-long cache for static files, Lighthouse 98/100/100/100.
- CI on every push (Python 3.11 to 3.15); publishing to PyPI by trusted publisher (dayfile 0.1.2 is live).
- Self-host path tested from scratch (token, write, read, backup).
- Healthchecks.io: backup and heartbeat-checker checks are green (2026-11-14); a failed backup pings `/fail`.
- Repository public as one squashed commit (gitleaks clean); secret scanning, push protection, private vulnerability reporting and branch protection on; the earlier history is kept in the private archive repo.
- Smoke test against production passed 2026-11-18 (`smoke-test.sh dayfile.cloud` with `SMOKE_MCP=1`: tokens, write and read isolation, MCP discovery, TLS, redirects), plus every public page, the status JSON and page, headers and the GitHub sign-in redirect.
- Heartbeats (a source or tag going quiet writes an open `heartbeat` to-do), the hosted MCP endpoint and a local `dayfile mcp`.

## Waiting on the operator

### 1. Uptime and alerts (monitors done; check the DigitalOcean alert policies)

UptimeRobot (free), alert contacts `team@dayfile.app` plus a phone push:

| Monitor | Type | Target |
|---|---|---|
| web | HTTP(s) | `https://dayfile.app/healthz` |
| api | HTTP(s), or keyword `"status":"ok"` | `https://dayfile.cloud/status` (needs no particular log) |
| landing | Keyword (alert if "One log" is missing) | `https://dayfile.app/` |

Also in the DigitalOcean console: Monitoring, alert policies for CPU, memory and disk (80%), to the same email.
Healthchecks.io (done 2026-11-14): two checks. **Backup**: period 1 day, grace 3 hours; then store both URLs with `ssh -t -i <ssh-key> dayfile@<droplet> 'sudo bash ~/dayfile/deploy/cloud/set-ping-urls.sh'` (hidden prompts; writes `/etc/dayfile/backup.env` and `heartbeat.env`, mode 600). The backup script pings after a good run and `<URL>/fail` after a failed one. **Heartbeat checker**: period 1 minute, grace 5 minutes; it is pinged after each good run of `dayfile-heartbeat.timer`. Send alerts to `team@dayfile.app` plus a phone push.

### 2. Restore drill (done 2026-11-18)

Passed on the operator's machine: 2 logs and the accounts database restored from `20261117T230154Z` in 1 s (row in
`docs/ops/drills.md`). Repeat quarterly and after any change to the backup code, with the read-only Spaces key and the
rclone crypt password from Bitwarden: `deploy/cloud/setup-drill-config.sh`, then `deploy/cloud/restore-drill.sh`.

### 3. DNS records (done 2026-11-18, DigitalOcean)

Added with `harden-dns.py`: SPF `-all` and DMARC `p=reject` on `dayfile.cloud`, and CAA on both zones (Let's Encrypt, Sectigo and ZeroSSL may issue; no wildcards; reports to `team@dayfile.app`). Checked afterwards: a throwaway log got a fresh Let's Encrypt certificate. Not added: the null MX (DigitalOcean's API refuses it). Still open: tighten `dayfile.app`'s DMARC from `p=none` once you have seen a few days of normal mail.

Fastest: `python3 deploy/cloud/harden-dns.py` (plan only) then `--apply`, on your own machine. It asks for a DO API token with only the domain scopes and a short expiry at a hidden prompt, only ever creates records, and reports conflicts instead of overwriting. Delete the token afterwards. The tables below are what it adds.

`dayfile.cloud` should never send mail:

| Name | Type | Value |
|---|---|---|
| `dayfile.cloud` | MX | priority 0, host `.` (null MX) **not added**: DigitalOcean's API refuses it; SPF and DMARC below already say it sends no mail |
| `dayfile.cloud` | TXT | `v=spf1 -all` |
| `_dmarc.dayfile.cloud` | TXT | `v=DMARC1; p=reject` |

Certificate authorities (Caddy may use either; allow both so issuance keeps working), on both zones:

| Name | Type | Value |
|---|---|---|
| `dayfile.cloud` and `dayfile.app` | CAA | `0 issue "letsencrypt.org"` |
| same | CAA | `0 issue "sectigo.com"` |
| same | CAA | `0 issuewild ";"` |
| same | CAA | `0 iodef "mailto:team@dayfile.app"` |

`dayfile.app` mail is currently Hostinger (MX `mx1`/`mx2.hostinger.com`, SPF includes Hostinger, DMARC `p=none`).
Once you have seen a few days of normal mail, tighten DMARC to `p=quarantine`, later `p=reject`.
After CAA changes, create a throwaway test log and confirm its certificate is issued.

### 4. Legal

Operating entity, governing law, minimum age (13 or 16), whether to require a DCO sign-off or a CLA for outside
contributions. The terms and privacy pages and the signup checkbox still say "draft".

### 5. Dogfood tokens

The old tokens were wiped with the logs on 2026-11-12. If a script or the notifier used one, mint a new token in
Settings and update it there.

### 6. When going public (done 2026-11-18: public, 0.1.0 to 0.1.2 on PyPI)

Make the repo public (the footer's GitHub link starts working), set up the PyPI trusted publisher (project `dayfile`,
workflow `publish.yml`, environment `pypi`), bump the version to 0.1.0, publish a release.

## Connectors (hosted MCP)

- [ ] In claude.ai, Customize, Connectors, Add custom connector with `https://<your-username>.dayfile.cloud/mcp`; confirm the Connect screen appears, approve, and ask what is open. Repeat read only.
- [ ] `claude mcp add --transport http dayfile https://<your-username>.dayfile.cloud/mcp` and `/mcp` sign-in work.
- [ ] Optional: submit dayfile to the connectors directory (a separate review by Anthropic).
- [ ] After a deploy, `SMOKE_MCP=1 deploy/cloud/smoke-test.sh dayfile.cloud` also checks `/mcp`.
