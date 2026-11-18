#!/usr/bin/env bash
# Make the rclone config for a restore drill, on YOUR machine (not the server):
#
#   bash deploy/cloud/setup-drill-config.sh <bucket> <region>
#
# It asks for the READ-ONLY Spaces key, the encryption password and salt with hidden input and writes
# ~/.config/dayfile-drill/rclone.conf (mode 600). Nothing is echoed, logged or passed on a command line.
# Then run the drill with that file (see deploy/cloud/restore-drill.sh).
set -euo pipefail

bucket="${1:?usage: setup-drill-config.sh <bucket> <region>   e.g. my-backups-bucket sfo3}"
region="${2:?usage: setup-drill-config.sh <bucket> <region>}"
[[ "$bucket" =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || { echo "bad bucket name" >&2; exit 2; }
[[ "$region" =~ ^[a-z]{3}[0-9]$ ]] || { echo "bad region (looks like nyc3, ams3, sfo3)" >&2; exit 2; }
command -v rclone >/dev/null || { echo "install rclone first (for example: sudo pacman -S rclone)" >&2; exit 2; }

ask() { # ask <variable> <prompt>: hidden input, must not be empty
	local value
	read -r -s -p "$2: " value
	echo
	[ -n "$value" ] || { echo "empty value" >&2; exit 2; }
	printf -v "$1" '%s' "$value"
}
ask key "READ-ONLY Spaces access key ID"
ask secret "READ-ONLY Spaces secret key"
ask password "Encryption password"
ask salt "Encryption salt"

dir="${DRILL_DIR:-$HOME/.config/dayfile-drill}"
install -d -m 700 "$dir"
conf="$dir/rclone.conf"
(
	umask 077
	cat >"$conf" <<CONF
[spaces]
type = s3
provider = DigitalOcean
access_key_id = $key
secret_access_key = $secret
endpoint = $region.digitaloceanspaces.com
acl = private
no_check_bucket = true

[backup]
type = crypt
remote = spaces:$bucket/dayfile
password = $(printf '%s' "$password" | rclone obscure -)
password2 = $(printf '%s' "$salt" | rclone obscure -)
filename_encryption = standard
directory_name_encryption = true
CONF
)
chmod 600 "$conf"
unset key secret password salt

echo "checking that the newest backup can be listed..."
RCLONE_CONFIG="$conf" rclone lsf --dirs-only backup: | tail -n 3
echo "config written to $conf. Next: RCLONE_CONFIG=$conf DAYFILE_CMD='uv run dayfile' bash deploy/cloud/restore-drill.sh"
