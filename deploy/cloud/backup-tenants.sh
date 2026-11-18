#!/usr/bin/env bash
# Nightly backup of every tenant: the container writes consistent, verified, gzipped copies (plus each
# tenant.json) into a staging directory, then this script uploads them with rclone and prunes old ones.
# Run as root on the host: the staged files belong to the container user.
#
#   BACKUP_REMOTE        rclone destination, e.g. "backup:" (the encrypted remote made by setup-backup.sh)
#   RCLONE_CONFIG        rclone config file (e.g. /etc/dayfile/rclone.conf)
#   BACKUP_STAGE         host directory bind-mounted into the container (default /srv/dayfile/backups)
#   BACKUP_STAGE_IN_CONTAINER  where that directory appears inside the container (default /backups)
#   BACKUP_KEEP_DAYS     delete uploads older than this (default 14)
#   BACKUP_PING_URL      optional dead man's switch (healthchecks.io style): fetched after a fully
#                        successful run, and "$BACKUP_PING_URL/fail" fetched when the run fails
#   ACCOUNTS_CMD         optional: how to run `python -m dayfile.web.admin` where the accounts database is
#                        (in production "docker compose -f <repo>/deploy/cloud/compose.yaml exec -T web
#                        python -m dayfile.web.admin"); its `backup` output is stored beside the tenants
#   DAYFILE_CMD          how to run dayfile (default "dayfile"); in production
#                        "docker compose -f <repo>/deploy/cloud/compose.yaml exec -T dayfile dayfile"
#
# One broken tenant never stops the others; the run still exits 1 so the timer reports it.
set -euo pipefail

: "${BACKUP_REMOTE:?set BACKUP_REMOTE}"
host_stage="${BACKUP_STAGE:-/srv/dayfile/backups}"
inner_stage="${BACKUP_STAGE_IN_CONTAINER:-/backups}"
keep_days="${BACKUP_KEEP_DAYS:-14}"
dayfile="${DAYFILE_CMD:-dayfile}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
stage="$host_stage/$stamp"
finish() {
	code=$?
	rm -rf "$stage"
	if [ "$code" -ne 0 ] && [ -n "${BACKUP_PING_URL:-}" ]; then
		curl -fsS --max-time 10 "${BACKUP_PING_URL%/}/fail" >/dev/null || true
	fi
}
trap finish EXIT

failed=0
# shellcheck disable=SC2086  # DAYFILE_CMD may be several words
$dayfile tenant backup-all "$inner_stage/$stamp" >/dev/null || failed=1

# The accounts database (usernames, GitHub links, consent) lives in the web container, which cannot see
# the staging directory, so its copy comes out on stdout and is written here by the host.
if [ -n "${ACCOUNTS_CMD:-}" ]; then
	mkdir -p "$stage"
	# shellcheck disable=SC2086  # ACCOUNTS_CMD may be several words
	if ! $ACCOUNTS_CMD backup >"$stage/accounts.accounts.db.gz.partial"; then
		echo "backup: the accounts database could not be copied" >&2
		rm -f "$stage/accounts.accounts.db.gz.partial"
		failed=1
	else
		mv "$stage/accounts.accounts.db.gz.partial" "$stage/accounts.accounts.db.gz"
	fi
fi

if [ -d "$stage" ] && [ -n "$(ls -A "$stage")" ]; then
	rclone copy "$stage" "$BACKUP_REMOTE$stamp"
else
	echo "backup: nothing was written to $stage" >&2
	exit 1
fi
# Only our own file kinds, only below the remote root, and only after they are old enough.
rclone delete --min-age "${keep_days}d" --include "*.db.gz" --include "*.tenant.json" "$BACKUP_REMOTE"
rclone rmdirs --leave-root "$BACKUP_REMOTE"

if [ "$failed" -ne 0 ]; then
	echo "backup: at least one tenant failed (see above)" >&2
	exit 1
fi
if [ -n "${BACKUP_PING_URL:-}" ]; then
	curl -fsS --max-time 10 "$BACKUP_PING_URL" >/dev/null
fi
