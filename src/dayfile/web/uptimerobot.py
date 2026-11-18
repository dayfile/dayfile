"""Uptime figures from UptimeRobot, an independent monitor that checks us from outside."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

API = "https://api.uptimerobot.com/v2/getMonitors"
PERIODS = (1, 7, 30, 90)  # days
DAY = 86400
MAX_BODY = 4 * 1024 * 1024
UP, DOWN, SEEMS_DOWN = 2, 9, 8
Report = dict[str, Any]


def read_key(path: str | None) -> str | None:
    """The read-only API key from its secret file, or None if there is no usable file."""
    try:
        return Path(path).read_text().strip() or None if path else None
    except OSError:
        return None


def _bar(pct: float | None) -> str:
    return "none" if pct is None else "up" if pct >= 99.9 else "part" if pct >= 95 else "down"


def _floats(text: Any, count: int) -> list[float | None]:
    parts = str(text or "").split("-")
    out: list[float | None] = []
    for part in parts[:count]:
        try:
            out.append(float(part))
        except ValueError:
            out.append(None)
    return out + [None] * (count - len(out))


def _day_ranges(now: int) -> tuple[list[int], str]:
    """The 90 UTC days ending today, as start times and the `start_end-...` text the API takes."""
    today = now - now % DAY
    starts = [today - i * DAY for i in range(89, -1, -1)]
    return starts, "-".join(f"{s}_{min(s + DAY - 1, now)}" for s in starts)


def parse(data: Any, now: int, show: frozenset[str] = frozenset()) -> Report | None:
    """Turn the API's answer into what the page shows (only the monitors named in `show`, if any).

    Returns None if it is not a usable answer.
    """
    if not isinstance(data, dict) or data.get("stat") != "ok":
        return None
    starts, _ = _day_ranges(now)
    monitors, incidents = [], []
    for m in data.get("monitors") or []:
        if not isinstance(m, dict) or m.get("status") == 0:  # paused monitors say nothing
            continue
        name = str(m.get("friendly_name", "monitor"))
        if show and name not in show:
            continue
        ratios = _floats(m.get("custom_uptime_ratio"), len(PERIODS))
        days = _floats(m.get("custom_uptime_ranges"), len(starts))
        monitors.append(
            {
                "name": name,
                "up": m.get("status") == UP,
                "uptime": dict(zip(PERIODS, ratios, strict=True)),
                "days": [
                    {"day": time.strftime("%Y-%m-%d", time.gmtime(s)), "pct": p, "state": _bar(p)}
                    for s, p in zip(starts, days, strict=True)
                ],
            }
        )
        for log in m.get("logs") or []:
            if isinstance(log, dict) and log.get("type") == 1:
                incidents.append(
                    {
                        "name": name,
                        "at": int(log.get("datetime", 0)),
                        "minutes": max(1, round(int(log.get("duration", 0)) / 60)),
                    }
                )
    incidents.sort(key=lambda i: i["at"], reverse=True)
    for i in incidents:
        i["start"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(i["at"]))
    return {
        "monitors": monitors,
        "incidents": incidents[:10],
        "all_up": all(m["up"] for m in monitors),
    }


def fetch(
    api_key: str, now: int, show: frozenset[str] = frozenset(), timeout: float = 10.0
) -> Report | None:
    """Ask UptimeRobot for every monitor's uptime and outages; None if it cannot be reached."""
    form = urllib.parse.urlencode(
        {
            "api_key": api_key,
            "format": "json",
            "custom_uptime_ratios": "-".join(map(str, PERIODS)),
            "custom_uptime_ranges": _day_ranges(now)[1],
            "logs": 1,
            "logs_limit": 50,
        }
    ).encode()
    request = urllib.request.Request(API, data=form, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return parse(json.loads(response.read(MAX_BODY)), now, show)
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Cached:
    """Asks at most once in `ttl` seconds; if the service is unreachable, reuses the last answer."""

    def __init__(
        self,
        get: Callable[[], Report | None],
        ttl: float = 300,
        stale: float = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.get, self.ttl, self.stale, self.clock = get, ttl, stale, clock
        self.value: Report | None = None
        self.when = -1e18
        self.tried = -1e18

    def __call__(self) -> Report | None:
        """Return the report, refreshing it when it is old."""
        now = self.clock()
        if now - self.tried >= self.ttl:
            self.tried = now
            if fresh := self.get():
                self.value, self.when = fresh, now
        return self.value if now - self.when <= self.stale else None
