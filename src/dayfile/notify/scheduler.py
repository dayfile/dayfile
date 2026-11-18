"""Notification scheduler: publish urgent events once, when they become visible."""

import logging
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from dayfile import db
from dayfile.timefmt import now as clock_now

log = logging.getLogger("dayfile.notify")
MIN_URGENCY = 2

CANDIDATES = f"""
SELECT e.seq FROM events e
WHERE e.urgency >= :min_urgency
  AND e.visible_at <= :now
  AND (e.expires_at IS NULL OR e.expires_at >= :now)
  AND (e.seq > :floor_seq OR e.visible_at > :init_at)
  AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.seq = e.seq AND d.channel = :channel)
  AND {db.STATE_CONDITIONS["open"]}
ORDER BY e.seq
"""


def tick(
    conn: sqlite3.Connection,
    publish: Callable[[dict[str, Any]], None],
    now: str,
    channel: str = "ntfy",
) -> int:
    """Publish each urgent event that is newly visible at `now`; return how many were sent.

    The first tick of a channel records where it started and replays no history.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        db.refresh_live_edges(conn, now)
        state = conn.execute("SELECT * FROM notify_state WHERE channel = ?", (channel,)).fetchone()
        if state is None:
            top = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
            conn.execute(
                "INSERT INTO notify_state (channel, floor_seq, init_at) VALUES (?, ?, ?)",
                (channel, top, now),
            )
            state = conn.execute(
                "SELECT * FROM notify_state WHERE channel = ?", (channel,)
            ).fetchone()
        seqs = [
            r["seq"]
            for r in conn.execute(
                CANDIDATES,
                {
                    "min_urgency": MIN_URGENCY,
                    "now": now,
                    "floor_seq": state["floor_seq"],
                    "init_at": state["init_at"],
                    "channel": channel,
                },
            )
        ]
        conn.executemany(
            "INSERT INTO deliveries (seq, channel, status, created_at) VALUES (?, ?, 'pending', ?)",
            [(seq, channel, now) for seq in seqs],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    sent = 0
    for seq in seqs:
        try:
            publish(db.get_event(conn, seq))
        except Exception as e:  # any delivery failure releases the claim for the next tick
            log.warning("publish failed for seq %s on %s: %s", seq, channel, type(e).__name__)
            conn.execute("DELETE FROM deliveries WHERE seq = ? AND channel = ?", (seq, channel))
            continue
        conn.execute(
            "UPDATE deliveries SET status = 'sent' WHERE seq = ? AND channel = ?", (seq, channel)
        )
        sent += 1
    return sent


def run(
    conn: sqlite3.Connection,
    publish: Callable[[dict[str, Any]], None],
    interval: float,
    once: bool = False,
) -> None:
    """Tick forever (or once) every `interval` seconds."""
    while True:
        tick(conn, publish, clock_now())
        if once:
            return
        time.sleep(interval)
