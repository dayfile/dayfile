#!/usr/bin/env bash
# One-time: store the GitHub OAuth client secret for the web app. Run it yourself, in a terminal:
#
#   ssh -t dayfile@<server> '~/dayfile/deploy/cloud/setup-github.sh'
#
# It asks for the secret with hidden input and writes it to the secrets directory (owner: the container
# user, mode 400), then restarts the web service so GitHub sign-in switches on. Nothing is echoed,
# logged or passed on a command line. GITHUB_CLIENT_ID (not secret) must already be set in .env.
set -euo pipefail

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
here="$(cd "$(dirname "$0")" && pwd)"
dir="${SECRETS_DIR:-/srv/dayfile/secrets}"
grep -qE '^GITHUB_CLIENT_ID=.+' "$here/.env" || { echo "set GITHUB_CLIENT_ID in $here/.env first" >&2; exit 2; }

read -r -s -p "GitHub client secret: " secret
echo
[ "${#secret}" -ge 20 ] || { echo "that does not look like a client secret" >&2; exit 2; }

install -d -o 10001 -g 10001 -m 700 "$dir"
(
	umask 277
	printf '%s' "$secret" >"$dir/github_client_secret.new"
)
chown 10001:10001 "$dir/github_client_secret.new"
chmod 400 "$dir/github_client_secret.new"
mv -f "$dir/github_client_secret.new" "$dir/github_client_secret"
unset secret

cd "$here"
docker compose up -d web
echo "stored $dir/github_client_secret; GitHub sign-in is on once the web service is healthy"
