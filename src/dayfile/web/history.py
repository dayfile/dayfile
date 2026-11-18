"""The status page's memory: one row per probe of the API, kept for 90 days (stdlib sqlite3)."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from dayfile.timefmt import minus

KEEP_DAYS = 90
INTERVAL = 60  # seconds between probes; a longer gap between failures ends an incident
DAY = 86400
SCHEMA = "CREATE TABLE IF NOT EXISTS probe (at TEXT PRIMARY KEY, ok INTEGER NOT NULL) WITHOUT ROWID"


class StatusHistory:
    """Records whether the API answered, and summarizes it as daily bars, uptime and incidents."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._conn() as conn:
            conn.execute(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def record(self, at: str, ok: bool) -> None:
        """Store one probe result and drop rows older than the retention window."""
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO probe VALUES (?, ?)", (at, int(ok)))
            conn.execute("DELETE FROM probe WHERE at < ?", (minus(at, (KEEP_DAYS) * DAY),))

    def summary(self, now: str) -> dict[str, Any]:
        """Uptime over 1, 7, 30 and 90 days, a bar per day, and the latest incidents."""
        with self._conn() as conn:
            uptime = {}
            for days in (1, 7, 30, 90):
                total, good = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(ok), 0) FROM probe WHERE at >= ?",
                    (minus(now, (days) * DAY),),
                ).fetchone()
                uptime[days] = round(100 * good / total, 2) if total else None
            rows = conn.execute(
                "SELECT substr(at, 1, 10), COUNT(*), SUM(ok) FROM probe WHERE at >= ? GROUP BY 1",
                (minus(now, (KEEP_DAYS - 1) * DAY)[:10],),
            ).fetchall()
            down = [r[0] for r in conn.execute("SELECT at FROM probe WHERE ok = 0 ORDER BY at")]
        return {
            "uptime": uptime,
            "days": _bars(now, {d: (n, good) for d, n, good in rows}),
            "incidents": _incidents(down)[-10:][::-1],
        }


def _bars(now: str, seen: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
    days = [minus(now, (i) * DAY)[:10] for i in range(KEEP_DAYS - 1, -1, -1)]
    out = []
    for day in days:
        n, good = seen.get(day, (0, 0))
        pct = 100 * good / n if n else None
        state = "none" if pct is None else "up" if pct >= 99.9 else "part" if pct >= 95 else "down"
        out.append({"day": day, "state": state, "pct": None if pct is None else round(pct, 2)})
    return out


def _incidents(down: list[str]) -> list[dict[str, Any]]:
    """Group failed probes that follow each other into one incident with a rough length."""

    def parse(at: str) -> float:
        return datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").timestamp()

    runs: list[list[str]] = []
    for at in down:
        if runs and parse(at) - parse(runs[-1][-1]) <= 2 * INTERVAL:
            runs[-1].append(at)
        else:
            runs.append([at])
    return [
        {"start": r[0], "minutes": max(1, round((parse(r[-1]) - parse(r[0])) / 60) + 1)}
        for r in runs
    ]
