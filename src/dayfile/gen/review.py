"""Sample log for a reviewer: a week of backups and deploys, to-dos, one missed heartbeat."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

from dayfile import heartbeats
from dayfile.ingest.core import IngestResult, ingest_events
from dayfile.timefmt import FORMAT, minus
from dayfile.ulid import new_ulid

HEARTBEAT = "weekly-report"
HEARTBEAT_SOURCE = "cron:weekly-report"


def _at(now: str, days_ago: int, hour: int, minute: int = 0) -> str:
    """The time `days_ago` days before today (UTC) at hour:minute."""
    today = datetime.strptime(now, FORMAT).replace(tzinfo=UTC).date()
    day = datetime.combine(today - timedelta(days=days_ago), datetime.min.time(), tzinfo=UTC)
    return (day + timedelta(hours=hour, minutes=minute)).strftime(FORMAT)


def _event(source: str, ref: str, when: str, title: str, **fields: Any) -> dict[str, Any]:
    return {
        "specversion": "dayfile/1",
        "id": new_ulid(),
        "source": {"id": source, "ref": ref},
        "occurred_at": when,
        **fields,
        "title": title,
    }


def _backups(now: str) -> list[dict[str, Any]]:
    out = []
    for n in range(1, 8):
        when = _at(now, n, 3, 0)
        if n == 4:
            out.append(
                _event(
                    "cron:backup",
                    "backup-4",
                    when,
                    "Nightly backup FAILED",
                    body="rsync to the offsite bucket stopped: no space left on device.",
                    tags=["backup", "alert"],
                    urgency=2,
                )
            )
        else:
            size = 3.1 + n / 20
            out.append(
                _event(
                    "cron:backup",
                    f"backup-{n}",
                    when,
                    "Nightly backup finished",
                    body=f"{size:.1f} GB copied in {38 + n} seconds.",
                    tags=["backup"],
                    payload={"gigabytes": round(size, 1), "seconds": 38 + n},
                )
            )
    return out


def _deploys(now: str) -> list[dict[str, Any]]:
    plan = [
        (6, 11, 20, "web", "1.8.0", "3f9c2ab", True),
        (5, 16, 5, "api", "1.8.0", "b71d0e4", True),
        (3, 9, 40, "web", "1.8.1", "e20a9c7", True),
        (2, 14, 30, "api", "1.8.2", "5c8f1d3", False),
        (1, 10, 10, "api", "1.8.3", "9a4be62", True),
    ]
    out = []
    for days, hour, minute, service, version, commit, ok in plan:
        when = _at(now, days, hour, minute)
        payload = {"service": service, "version": version, "commit": commit}
        if ok:
            out.append(
                _event(
                    "ci:deploy",
                    f"deploy-{service}-{version}",
                    when,
                    f"Deployed {service} {version} to production",
                    body=f"Commit {commit}. Health checks passed.",
                    tags=["deploy"],
                    payload=payload,
                )
            )
        else:
            out.append(
                _event(
                    "ci:deploy",
                    f"deploy-{service}-{version}",
                    when,
                    f"Rolled back {service} {version}",
                    body="The health check failed after 90 seconds; the previous version is back.",
                    tags=["deploy", "alert"],
                    urgency=2,
                    payload=payload,
                )
            )
    return out


def _todos(now: str) -> list[dict[str, Any]]:
    due = (date.fromisoformat(minus(now, 0)[:10]) + timedelta(days=12)).isoformat()
    return [
        _event(
            "adapter:cli",
            "todo-backup",
            _at(now, 4, 7, 30),
            "Investigate the failed nightly backup",
            body="The offsite bucket is full. Check lifecycle rules.",
            tags=["todo", "backup"],
            urgency=2,
        ),
        _event(
            "adapter:cli",
            "todo-signing-key",
            _at(now, 6, 9, 0),
            "Rotate the API signing key",
            tags=["todo", "security"],
        ),
        _event(
            "adapter:cli",
            "todo-certificate",
            _at(now, 2, 15, 0),
            "Renew the TLS certificate for staging.example.net",
            tags=["todo", "ops"],
            urgency=1,
            payload={"due": due},
        ),
        _event(
            "adapter:cli",
            "todo-dependencies",
            _at(now, 1, 10, 30),
            "Review the pending dependency updates",
            tags=["todo", "deploy"],
        ),
    ]


def _close(target: dict[str, Any], when: str) -> dict[str, Any]:
    ref = target["source"]["ref"]
    return {
        "specversion": "dayfile/1",
        "id": new_ulid(),
        "source": {"id": "adapter:cli", "ref": f"close-{ref}"},
        "occurred_at": when,
        "refs": [{"rel": "completes", "id": target["id"]}],
    }


def generate(conn: sqlite3.Connection, now: str) -> dict[str, int]:
    """Write the sample events and one missed heartbeat; running it again adds nothing.

    The events carry old `occurred_at` times, so the log looks lived in, and the heartbeat is set
    three days back so the check at `now` writes its open to-do. Returns counts of new events.
    """
    results: list[IngestResult] = ingest_events(
        conn, [*_backups(now), *_deploys(now), *_todos(now)]
    )
    stored = {r.event["source"]["ref"]: r.event for r in results}
    closing = [
        _close(stored["todo-backup"], _at(now, 3, 10, 15)),
        _close(stored["todo-signing-key"], _at(now, 5, 8, 45)),
    ]
    results += ingest_events(conn, closing)
    if not any(h["name"] == HEARTBEAT for h in heartbeats.list_heartbeats(conn)):
        heartbeats.save_heartbeat(
            conn, HEARTBEAT, minus(now, 3 * 86400), "24h", source=HEARTBEAT_SOURCE
        )
    flagged = heartbeats.check(conn, now)
    return {
        "events": sum(r.created for r in results) + len(flagged),
        "heartbeat_todos": len(flagged),
    }
