#!/usr/bin/env bash
# Post-deploy check for the managed profile. Creates a throwaway tenant, exercises it end to end,
# then moves it to .trash. Exits non-zero on the first failure.
#
#   smoke-test.sh <base-domain> [slug]
#
#   DAYFILE_CMD      how to run dayfile (default "dayfile"); needs DAYFILE_TENANTS_DIR set
#   SMOKE_MCP        set to 1 to also check /mcp (the cloud profile serves it)
#   SMOKE_ORIGIN     talk to this URL with a Host header instead of https://<slug>.<domain>,
#                    e.g. http://127.0.0.1:8000 for a local server. Skips the checks that need
#                    the real proxy (TLS, redirect, blocked /internal).
set -euo pipefail

domain="${1:?usage: smoke-test.sh <base-domain> [slug]}"
slug="${2:-smoke$(date +%s)}"
dayfile="${DAYFILE_CMD:-dayfile}"
host="$slug.$domain"
origin="${SMOKE_ORIGIN:-https://$host}"
edge=1
[ -n "${SMOKE_ORIGIN:-}" ] && edge=0

pass() { printf 'ok    %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1" >&2; exit 1; }

# shellcheck disable=SC2086  # DAYFILE_CMD may be several words
run() { $dayfile "$@"; }
request() { # request <expected-status> <description> <curl args...>
	local want="$1" what="$2" got
	shift 2
	got="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H "Host: ${HOSTH:-$host}" "$@")" || got=000
	[ "$got" = "$want" ] && pass "$what" || fail "$what (wanted $want, got $got)"
}

db="$(run tenant create "$slug")"
trap 'run tenant delete "$slug" --yes >/dev/null 2>&1 || true' EXIT
pass "created tenant $slug"

write_out="$(run --url "$origin" token create --labels self --write-source smoke --db "$db")"
write_token="$(printf '%s\n' "$write_out" | head -n 1)"
read_out="$(run --url "$origin" token create --labels self --db "$db")"
read_url="$(printf '%s\n' "$read_out" | head -n 1)"
read_token="$(printf '%s' "$read_url" | sed -n 's#.*/t/\([^/]*\)/feed.json#\1#p')"
[ -n "$write_token" ] && [ -n "$read_token" ] || fail "could not mint tokens"
pass "minted a write token and a read token"

event='{"specversion":"dayfile/1","id":"01JC8Z4Q7M2X9K3T5W6N8R0001","source":{"id":"smoke","ref":"1"},"occurred_at":"2026-11-06T10:00:00Z","title":"smoke test"}'
request 401 "anonymous read is refused" "$origin/events"
request 201 "write token can post" -X POST -H "Authorization: Bearer $write_token" \
	-H 'content-type: application/json' -d "$event" "$origin/events"
request 403 "read token cannot post" -X POST -H "Authorization: Bearer $read_token" \
	-H 'content-type: application/json' -d "$event" "$origin/events"
request 200 "read token can read" -H "Authorization: Bearer $read_token" "$origin/events"
request 403 "write token cannot read" -H "Authorization: Bearer $write_token" "$origin/events"

body="$(curl -s --max-time 15 -H "Host: $host" -H "Authorization: Bearer $read_token" "$origin/events")"
case "$body" in *"smoke test"*) pass "the event reads back" ;; *) fail "the event did not read back" ;; esac

if [ "${SMOKE_MCP:-0}" = 1 ]; then
	request 401 "the MCP endpoint asks for a credential" -X POST -H 'content-type: application/json' \
		-d '{}' "$origin/mcp"
	request 200 "the MCP discovery document is served" "$origin/.well-known/oauth-protected-resource/mcp"
fi

other="smokeother$$.$domain"
if [ "$edge" = 1 ]; then
	# The proxy asks dayfile before issuing a certificate, so an unknown name never completes TLS.
	request 000 "an unknown tenant gets no certificate" "https://$other/events"
	request 200 "health check answers" "$origin/healthz"
	request 404 "/internal/tls-ask is hidden from outside" "$origin/internal/tls-ask?domain=$host"
	request 308 "plain HTTP is redirected to HTTPS" "http://$host/events"
else
	HOSTH="$other" request 404 "an unknown tenant is 404" -H "Authorization: Bearer $read_token" "$origin/events"
	request 200 "health check answers" "$origin/healthz"
fi
echo "smoke test passed for $host"
