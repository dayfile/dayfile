#!/usr/bin/env bash
# Store the healthchecks.io ping URLs for the backup and the heartbeat checker (run as root):
#   sudo bash deploy/cloud/set-ping-urls.sh
# The URLs are secrets: they are typed at a hidden prompt and never printed. Leave a prompt empty to
# keep what is already set. Files: ENV_DIR/backup.env and ENV_DIR/heartbeat.env (mode 600).
set -euo pipefail

dir="${ENV_DIR:-/etc/dayfile}"
tty="${PROMPT_FROM:-/dev/tty}"
install -d -m 700 "$dir"

ask() { # file, variable, label
	local url=""
	printf '%s (hidden; empty keeps the current value): ' "$3" >&2
	IFS= read -rs url <"$tty" || true
	printf '\n' >&2
	url="${url//[$'\r\n ']/}"
	if [ -z "$url" ]; then
		echo "  $3: unchanged" >&2
		return
	fi
	case "$url" in
	https://*) ;;
	*)
		echo "  $3: that does not look like an https URL, nothing written" >&2
		return 1
		;;
	esac
	touch "$dir/$1"
	chmod 600 "$dir/$1"
	grep -v "^$2=" "$dir/$1" >"$dir/$1.new" || true
	printf '%s=%s\n' "$2" "$url" >>"$dir/$1.new"
	chmod 600 "$dir/$1.new"
	mv "$dir/$1.new" "$dir/$1"
	echo "  $3: saved in $dir/$1" >&2
}

rc=0
ask backup.env BACKUP_PING_URL "Backup ping URL" || rc=1
ask heartbeat.env HEARTBEAT_PING_URL "Heartbeat ping URL" || rc=1
for f in backup.env heartbeat.env; do
	if [ -f "$dir/$f" ]; then echo "$dir/$f: $(stat -c '%U %a' "$dir/$f"), $(wc -l <"$dir/$f") line(s)"; fi
done
exit "$rc"
