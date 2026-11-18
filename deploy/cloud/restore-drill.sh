#!/usr/bin/env bash
# Restore drill: pull the newest backup from the encrypted remote into a scratch directory, open every
# tenant's database, check it, and print how long it took. Run it on your own machine (or any throwaway
# one), never on the production Droplet, with the READ-ONLY key. It changes nothing in the bucket.
#
#   RCLONE_CONFIG   rclone config for the crypt remote (read-only key plus the password and salt)
#   BACKUP_REMOTE   the remote, e.g. "backup:" (default)
#   DAYFILE_CMD     how to run dayfile (default "dayfile"; for example "uv run dayfile")
#   $1              a specific stamp to restore (default: the newest)
#
# Record the result as a row in docs/ops/drills.md: date, what was restored, elapsed, problems.
set -euo pipefail

remote="${BACKUP_REMOTE:-backup:}"
dayfile="${DAYFILE_CMD:-dayfile}"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
start="$(date +%s)"

stamp="${1:-$(rclone lsf --dirs-only "$remote" | sed 's#/$##' | sort | tail -n 1)}"
[ -n "$stamp" ] || { echo "restore-drill: the remote holds no backups" >&2; exit 1; }
echo "restoring $stamp from $remote into a scratch directory"
rclone copy "$remote$stamp" "$scratch"

# shellcheck disable=SC2086  # DAYFILE_CMD may be several words
$dayfile tenant verify-backup "$scratch" && result="ok" || result="PROBLEMS (see above)"
elapsed=$(($(date +%s) - start))
echo
tenants="$(find "$scratch" -name '*.db.gz' ! -name '*.accounts.db.gz' | wc -l)"
accounts=""
[ -e "$scratch/accounts.accounts.db.gz" ] && accounts=" + accounts database"
echo "| $(date -u +%F) | $stamp | $tenants tenants$accounts | ${elapsed}s | $result |"
[ "$result" = "ok" ]
