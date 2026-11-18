"""Sample events for the public `/demo` page: a believable log, built fresh relative to `now`."""

from typing import Any

from dayfile import timefmt

# (seconds ago, source, title, details, tags, status)
SAMPLES: tuple[tuple[int, str, str, str, tuple[str, ...], str | None], ...] = (
    (300, "cron:backup", "Nightly backup finished: 12 logs, 4.1 MB", "", ("backup",), None),
    (2400, "ci", "Deployed v1.8.2 to production", "All checks passed.", ("deploy", "ops"), None),
    (
        7200,
        "web",
        "Renew the domain",
        "**Due Friday.** Auto-renew is off.",
        ("todo", "admin"),
        "open",
    ),
    (
        10800,
        "monitor",
        "Disk at 82% on web-1",
        "Threshold is 80%.\n\n- clear old logs\n- or grow the volume",
        ("alert", "ops"),
        None,
    ),
    (90000, "web", "Rotate the API keys", "", ("todo", "security"), "closed"),
    (180000, "cron:certs", "TLS certificate renews in 12 days", "", ("tls",), None),
)


def sample_events(now: str) -> list[dict[str, Any]]:
    """The sample log, newest first, with times counted back from `now`."""
    events = []
    for index, (ago, source, title, body, tags, status) in enumerate(SAMPLES):
        at = timefmt.minus(now, ago)
        event: dict[str, Any] = {
            "id": f"01DEM0{index:020d}",
            "seq": len(SAMPLES) - index,
            "source": {"id": source},
            "occurred_at": at,
            "visible_at": at,
            "title": title,
            "tags": list(tags),
            "urgency": 0,
        }
        if body:
            event["body"] = body
        if status:
            event["status"] = status
        events.append(event)
    return events


def matching(events: list[dict[str, Any]], tags: list[str], status: str) -> list[dict[str, Any]]:
    """Keep events with every tag and the status given (`none`: no status; empty: any)."""
    kept = []
    for event in events:
        if not set(tags) <= set(event["tags"]):
            continue
        have = event.get("status", "none")
        if status in ("", "all") or status == have:
            kept.append(event)
    return kept
