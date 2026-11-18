#!/usr/bin/env bash
# One-time: store the UptimeRobot read-only API key for the status page. Run it yourself, in a terminal:
#
#   ssh -t dayfile@<server> '~/dayfile/deploy/cloud/setup-uptimerobot.sh'
#
# It asks for the key with hidden input, writes it to the secrets directory (owner: the container user,
# mode 400) and restarts the web service. Use the READ-ONLY key (Integrations, API), not the main one. Nothing is echoed.
set -euo pipefail

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
here="$(cd "$(dirname "$0")" && pwd)"
dir="${SECRETS_DIR:-/srv/dayfile/secrets}"

read -r -s -p "UptimeRobot read-only API key: " key
echo
key="${key//[$'\r\n ']/}"
[ "${#key}" -ge 16 ] || { echo "that does not look like an API key; nothing written" >&2; exit 2; }

install -d -o 10001 -g 10001 -m 700 "$dir"
(
	umask 277
	printf '%s' "$key" >"$dir/uptimerobot_key.new"
)
chown 10001:10001 "$dir/uptimerobot_key.new"
chmod 400 "$dir/uptimerobot_key.new"
mv -f "$dir/uptimerobot_key.new" "$dir/uptimerobot_key"
unset key

cd "$here"
docker compose up -d web
echo "stored $dir/uptimerobot_key; the status page uses UptimeRobot once the web service is healthy"
