#!/usr/bin/env bash
# The numbers that matter on launch day. Counts only: never log content, tokens or answers.
#
#   launch-stats.sh             headline numbers
#   launch-stats.sh accounts    who signed up (GitHub login, username, date)
#
# Run it on the Droplet, or from anywhere:
#   ssh dayfile@<droplet> dayfile/deploy/cloud/launch-stats.sh
#
#   COMPOSE   how to run docker compose (default "sudo docker compose")
set -euo pipefail

cd "$(dirname "$0")"
compose="${COMPOSE:-sudo docker compose}"
# shellcheck disable=SC2086  # COMPOSE may be several words
run() { $compose exec -T "$@"; }

if [ "${1:-}" = accounts ]; then
	run web python -m dayfile.web.admin account list | python3 -c "$(cat <<'PY'
import json, sys

for line in sys.stdin:
    a = json.loads(line)
    who = "(deleted)" if a["status"] == "deleted" else a["github_login"]
    print(f"{a['created_at'][:16]}Z  {a['status']:<9} {who:<24} {a['slug'] or '-'}")
PY
)"
	exit 0
fi

accounts="$(run web python -m dayfile.web.admin stats)"
logs="$(run dayfile dayfile tenant stats)"
ACCOUNTS="$accounts" LOGS="$logs" python3 - <<'PY'
import json, os
from datetime import datetime, timedelta, timezone

a, t = json.loads(os.environ["ACCOUNTS"]), json.loads(os.environ["LOGS"])
today = datetime.now(timezone.utc).date()
per_day = a["new_per_day"]
week = sum(per_day.get((today - timedelta(days=i)).isoformat(), 0) for i in range(7))
interest = a["feature_interest"]

print(f"as of {a['at']}")
print(f"accounts    {a['accounts']['active']} active, {a['accounts']['deleted']} deleted,"
      f" {a['accounts']['suspended']} suspended")
print(f"new         {per_day.get(today.isoformat(), 0)} today, {week} in the last 7 days")
print(f"logs        {t['logs']} ({t['activated']} have events, {t['unreadable']} unreadable)")
print(f"discovery   {a['with_signup_answer']} plan answers, {a['with_contact_email']} contact emails")
print("roadmap     " + ", ".join(
    f"{name} {v['signed_in'] + v['signed_out']}" for name, v in interest.items()))
PY
