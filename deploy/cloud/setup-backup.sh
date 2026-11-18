#!/usr/bin/env bash
# One-time setup of the backup credentials on the server. Run it yourself, in a terminal:
#
#   ssh -t dayfile@<server> '~/dayfile/deploy/cloud/setup-backup.sh <bucket> <region>'
#
# It asks for the Spaces access key, the encryption password and salt with hidden input and writes
# /etc/dayfile/rclone.conf (root only, mode 600). Nothing is echoed, logged or passed on a command
# line. It then proves the setup by writing, reading and deleting one encrypted test file.
set -euo pipefail

bucket="${1:?usage: setup-backup.sh <bucket> <region>   e.g. my-backups-bucket nyc3}"
region="${2:?usage: setup-backup.sh <bucket> <region>}"
[[ "$bucket" =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || { echo "bad bucket name" >&2; exit 2; }
[[ "$region" =~ ^[a-z]{3}[0-9]$ ]] || { echo "bad region (looks like nyc3, ams3, sfo3)" >&2; exit 2; }
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
command -v rclone >/dev/null || apt-get install -y rclone

ask() { # ask <variable> <prompt>: hidden input, must not be empty
	local value
	read -r -s -p "$2: " value
	echo
	[ -n "$value" ] || { echo "empty value" >&2; exit 2; }
	printf -v "$1" '%s' "$value"
}
ask key "Spaces access key ID"
ask secret "Spaces secret key"
ask password "Encryption password"
ask salt "Encryption salt"

install -d -m 700 /etc/dayfile
conf=/etc/dayfile/rclone.conf
(
	umask 077
	cat >"$conf" <<EOF
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
EOF
)
chmod 600 "$conf"
unset key secret password salt

export RCLONE_CONFIG="$conf"
probe="setup-test-$(date -u +%s)"
printf 'ok' | rclone rcat "backup:$probe"
[ "$(rclone cat "backup:$probe")" = ok ] || { echo "read-back failed" >&2; exit 1; }
rclone deletefile "backup:$probe"
echo "backup storage works. Config: $conf (use BACKUP_REMOTE=backup:)"
