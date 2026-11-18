# Design: backups, replication and restore drills

Status: scripts written (M13) but never run against real storage; no bucket exists, `BACKUP_REMOTE` is blank.

## Goals

- Lose at most 24 hours of data to a disk or Droplet loss now; seconds once there are outside users.
- Backups survive compromise of the server (an attacker with the Droplet must not be able to read or silently destroy
  every copy).
- A restore has been practiced, with the time written down.

## Layers

| Layer | Covers | RPO | Cost/effort | When |
|---|---|---|---|---|
| A. Nightly encrypted copies to Spaces (`backup-tenants.sh`) | tenant dbs, and (to add) `tenant.json`, `accounts.db` | 24 h | bucket plus a timer | before M16 goes live, ideally now |
| B. Droplet snapshots (DO "Backups", weekly, ~20% of the plan price; check current price) | whole disk incl. Docker volumes, `.env`, Caddy certificates | up to 7 d | one checkbox | now; it is the cheap catch-all |
| C. Litestream to Spaces (compose profile `replicate`) | tenant dbs continuously | seconds | about 30 to 50 MB RAM on a box with ~418 MB free | before the first non-operator tenant |
| D. Pull copy to the operator's machine | whole bucket, with a separate read-only key | 24 h | an rclone cron or manual run | once, then monthly |

Recommendation: A and B now, D after the first successful drill, C when outside users arrive. C is skipped for now
because the dogfood log is operator-owned and the RAM is tight.

## Gaps found in the current script (fix when implementing)

1. `backup-tenants.sh` saves only `dayfile.db`. **`tenant.json` (limits, suspension, owner) is not backed up.** Without
   it a restored tenant comes back unlimited and unsuspended. Add `$slug-$stamp.tenant.json` to the stage.
2. `accounts.db` (M16) needs the same treatment through `sqlite3`'s backup API, in the `web` container.
3. The script runs `rclone delete --min-age` on the remote root, which would also delete other things stored under that
   prefix. Constrain to `$BACKUP_REMOTE` and use `--include`/`--rmdirs`, and test against a local `type = local` remote.
4. (Checked: `dayfile backup` already runs `PRAGMA integrity_check` on each copy.)
5. Nothing runs it yet: needs a host systemd timer (below).
6. **The script cannot work as written with the deployed layout.** The tenant files belong to uid 10001 with mode 700, and the
   script stages copies on the host while `DAYFILE_CMD` runs inside the container, so the container writes where the host
   cannot see, and the host user could not read the files anyway. Fix: a `dayfile tenant backup-all <dir>` command that runs
   in the container and writes gzip copies plus `tenant.json` into a dedicated directory (`/srv/dayfile/backups`, bind-mounted
   into the container, owned by uid 10001); a root systemd service then runs `rclone` over that directory and clears it. The
   logic moves from bash into tested Python and the script shrinks to the upload step.

## Storage

- DigitalOcean Spaces bucket, same region as the Droplet (in-region transfer; check current Spaces pricing, historically a
  flat monthly base with included storage and transfer). Private bucket, no CDN, no listing.
- **Two access keys**, both limited to this bucket if Spaces' scoped keys are available (check at creation):
  `dayfile-backup-write` on the server, and `dayfile-backup-read` kept offline for restores and layer D. Spaces has no
  write-only permission, so a compromised server could delete the bucket contents. Mitigations: bucket versioning and a
  lifecycle rule if available (verify), layer D's independent copy, and Droplet snapshots (B), which live outside the
  bucket. Do not claim immutability we cannot confirm.
- Encryption: an rclone **crypt** remote layered on the bucket (file names and contents), configured with a random
  password and salt. The operator keeps both in a password manager *and* on the server in `rclone.conf` (0600). Losing
  them makes every backup unreadable; the drill proves the copies in the password manager are the right ones.

## Schedule

A systemd service plus timer on the host (not in the container):

```
dayfile-backup.service   ExecStart=/home/dayfile/dayfile/deploy/cloud/backup-tenants.sh
                         Environment=DAYFILE_CMD="docker compose -f /home/dayfile/dayfile/deploy/cloud/compose.yaml exec -T dayfile dayfile"
                         EnvironmentFile=/home/dayfile/backup.env (BACKUP_REMOTE, BACKUP_PING_URL, 0600)
dayfile-backup.timer     OnCalendar=*-*-* 03:17:00 UTC, Persistent=true, RandomizedDelaySec=10m
```

`rclone` is installed on the host with apt. Success pings the dead-man's-switch URL (see `monitoring.md`); a missing ping
raises the alert, which also covers the case where the timer never fires. Each run also posts a `backup.done` event to
the dogfood log when its write token is configured, as a convenience, not as the alert path.

## Restore drill

Script `deploy/cloud/restore-drill.sh` (to write), run from the operator's machine or a throwaway container, never on the
production Droplet:

1. `rclone copy` the newest stamp from the crypt remote to a temp dir using the read-only key.
2. `gunzip`, then for each tenant: `PRAGMA integrity_check` (must be `ok`), `SELECT count(*), max(seq) FROM events`, and
   confirm the append-only triggers exist.
3. Start `dayfile` locally with the restored directory and `DAYFILE_BASE_DOMAIN=localhost`, run
   `smoke-test.sh` style reads with a token from the restored `tokens` table (or a freshly minted one).
4. Compare the event count with `tenant usage` taken before the drill and print the **elapsed time**.
5. Litestream (when enabled): `litestream restore -o <path> s3://…/tenants/<slug>/dayfile.db`, same checks.

Cadence: once right after the first nightly run, then quarterly and after any change to backup code. Results go in
`docs/ops/drills.md` (date, what was restored, elapsed, problems). The drill is not done until a row exists.

## Retention

Nightly: 14 days (existing `BACKUP_KEEP_DAYS`), plus the first backup of each month kept 12 months (add a second prefix
`monthly/`). Replicas: 168 h (existing). Deleted accounts age out of these within the stated windows; the privacy page
quotes them, so change both together.

## Inputs from the operator

- Create the Spaces bucket and two access keys (or give the region and I give exact click steps); put the secrets in the
  server's `backup.env`, never in chat.
- Choose and store the rclone crypt password and salt in the password manager.
- Pick the dead-man's-switch service (see `monitoring.md`).

## Acceptance

Timer fires nightly; ping seen; a drill row exists with an elapsed time; `tenant.json` and `accounts.db` appear in the
backup; tests cover the script against a local rclone remote (no network).
