"""Operator stats: aggregate counts across tenants, and never a log's content."""

import itertools
import json
import sqlite3

import pytest

from dayfile import cli, stats
from dayfile.tenancy import DirectoryTenancy
from dayfile.timefmt import minus

NOW = "2026-11-16T12:00:00Z"
DAY = 86400
IDS = itertools.count(1)


@pytest.fixture
def tenancy(tmp_path):
    return DirectoryTenancy(tmp_path / "tenants", "x.test")


def post(tenancy, slug, source, days_ago):
    """Append one event recorded `days_ago` days before NOW, as a source would."""
    n = next(IDS)
    found = tenancy.get(slug) or tenancy.create(slug)
    at = minus(NOW, int(days_ago * DAY))
    conn = sqlite3.connect(found.db_path)
    conn.execute(
        "INSERT INTO events (id, source_id, source_ref, occurred_at, visible_at, title,"
        " recorded_at) VALUES (?, ?, ?, ?, ?, 'secret title', ?)",
        (f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}", source, f"r{n}", at, at, at),
    )
    conn.commit()
    conn.close()


def summary(tenancy, *exclude):
    names = [n for n in tenancy.list() if n not in exclude]
    return stats.summarize({n: tenancy.get(n).db_path for n in names}, NOW)


def test_counts_logs_activated_and_multi_source(tenancy):
    tenancy.create("empty")
    post(tenancy, "one", "phone", 1)
    post(tenancy, "two", "phone", 1)
    post(tenancy, "two", "email", 2)
    got = summary(tenancy)
    assert (got["logs"], got["activated"], got["multi_source"]) == (3, 2, 1)


def test_events_the_server_writes_itself_do_not_count_as_a_source(tenancy):
    post(tenancy, "quiet", "dayfile:heartbeat", 1)
    post(tenancy, "busy", "phone", 1)
    post(tenancy, "busy", "dayfile:heartbeat", 1)
    got = summary(tenancy)
    assert (got["activated"], got["multi_source"]) == (1, 0)


def test_week4_retention_is_measured_from_each_logs_first_event(tenancy):
    post(tenancy, "kept", "phone", 40)
    post(tenancy, "kept", "phone", 2)  # heard from this week
    post(tenancy, "lapsed", "phone", 40)
    post(tenancy, "lapsed", "phone", 10)
    post(tenancy, "young", "phone", 5)  # under four weeks old: not in the cohort
    assert summary(tenancy)["week4"] == {"cohort": 2, "retained": 1, "share": 0.5}


def test_no_cohort_yet_has_no_share(tenancy):
    post(tenancy, "young", "phone", 1)
    assert summary(tenancy)["week4"] == {"cohort": 0, "retained": 0, "share": None}


def test_the_stats_connection_cannot_read_titles_bodies_payloads_or_tags(tenancy):
    post(tenancy, "acme", "phone", 1)
    conn = stats.open_counts_only(tenancy.get("acme").db_path)
    try:
        for sql in (
            "SELECT title FROM events",
            "SELECT body FROM events",
            "SELECT payload FROM events",
            "SELECT tag FROM event_tags",
            "SELECT * FROM events",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
                conn.execute(sql).fetchall()
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO events (id) VALUES ('x')")  # and it is read-only
    finally:
        conn.close()


def test_cli_prints_numbers_only_and_honors_exclude(tenancy, capsys):
    post(tenancy, "acme", "phone", 1)
    post(tenancy, "mine", "phone", 1)
    root = str(tenancy.root)
    assert cli.main(["tenant", "stats", "--root", root, "--exclude", "mine"]) == 0
    out = capsys.readouterr().out
    got = json.loads(out)
    assert got["logs"] == 1 and got["activated"] == 1
    assert "acme" not in out and "secret title" not in out and "phone" not in out
