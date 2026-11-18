import base64

import pytest

from dayfile import cli
from dayfile.db import connect
from dayfile.ingest.core import ingest_events
from dayfile.notify.ntfy import build_request, encode_header, priority
from dayfile.notify.scheduler import tick


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "d.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


class Sink:
    def __init__(self):
        self.sent = []

    def __call__(self, event):
        self.sent.append(event["title"])


def ev(n, **over):
    base = {
        "specversion": "dayfile/1",
        "id": f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}",
        "tags": ["todo"],
        "source": {"id": "t", "ref": f"r{n}"},
        "occurred_at": "2026-11-06T10:00:00Z",
        "title": f"event {n}",
        "urgency": 2,
    }
    base.update(over)
    return base


# --- ntfy request construction ----------------------------------------------


def test_priority_mapping():
    assert (priority(2), priority(3)) == (4, 5)
    assert priority(0) == 3


def test_ascii_headers_pass_through_and_unicode_is_rfc2047():
    assert encode_header("Renew passport") == "Renew passport"
    encoded = encode_header("Café ☕")
    assert encoded.startswith("=?UTF-8?B?") and encoded.endswith("?=")
    assert base64.b64decode(encoded[10:-2]).decode() == "Café ☕"


def test_newlines_cannot_inject_headers():
    assert "\n" not in encode_header("a\r\nX-Evil: 1") and "\r" not in encode_header("a\r\nb")
    req = build_request("https://ntfy.example/topic", {"title": "a\r\nX-Evil: 1", "urgency": 3})
    assert "X-evil" not in {k.capitalize() for k in req.headers}
    assert all("\n" not in v and "\r" not in v for v in req.headers.values())


def test_request_fields():
    event = {
        "title": "Edge node 07",
        "body": "vibration above threshold",
        "urgency": 3,
        "tags": ["fleet/anomaly", "ops"],
        "links": [{"rel": "related", "href": "https://example.com/n07"}],
    }
    req = build_request("https://ntfy.example/topic", event)
    assert req.full_url == "https://ntfy.example/topic" and req.get_method() == "POST"
    h = {k.lower(): v for k, v in req.headers.items()}
    assert h["title"] == "Edge node 07" and h["priority"] == "5"
    assert h["tags"] == "fleet/anomaly,ops" and h["click"] == "https://example.com/n07"
    assert req.data == b"vibration above threshold"


def test_body_falls_back_to_title():
    assert build_request("https://x/t", {"title": "Hello", "urgency": 2}).data == b"Hello"


# --- scheduler ---------------------------------------------------------------


def test_time_released_urgent_todo_publishes_exactly_once_at_visible_at(conn, db_path):
    sink = Sink()
    tick(conn, sink, "2026-11-06T09:00:00Z")  # first run: initializes, nothing to say
    ingest_events(conn, [ev(1, visible_at="2026-11-07T09:00:00Z")])
    assert tick(conn, sink, "2026-11-06T12:00:00Z") == 0
    assert tick(conn, sink, "2026-11-07T08:59:59Z") == 0
    assert tick(conn, sink, "2026-11-07T09:00:00Z") == 1
    assert tick(conn, sink, "2026-11-07T09:00:30Z") == 0
    # a restart: new connection, fresh scheduler, same database
    again = connect(db_path)
    assert tick(again, sink, "2026-11-07T09:01:00Z") == 0
    again.close()
    assert sink.sent == ["event 1"]


def test_first_run_does_not_replay_history(conn):
    sink = Sink()
    ingest_events(conn, [ev(1, urgency=3)])  # already released before the notifier ever ran
    assert tick(conn, sink, "2026-11-06T12:00:00Z") == 0
    ingest_events(conn, [ev(2, urgency=3)])
    assert tick(conn, sink, "2026-11-06T12:00:30Z") == 1
    assert sink.sent == ["event 2"]


def test_events_pending_release_at_first_run_are_still_delivered(conn):
    sink = Sink()
    ingest_events(conn, [ev(1, visible_at="2026-11-07T09:00:00Z")])
    assert tick(conn, sink, "2026-11-06T12:00:00Z") == 0
    assert tick(conn, sink, "2026-11-07T09:00:00Z") == 1


def test_late_arriving_urgent_event_with_old_date_is_delivered(conn):
    sink = Sink()
    tick(conn, sink, "2026-11-06T12:00:00Z")
    ingest_events(conn, [ev(1, occurred_at="2026-11-06T08:00:00Z")])  # e.g. delayed email
    assert tick(conn, sink, "2026-11-06T12:00:30Z") == 1


def test_only_urgency_two_and_above(conn):
    sink = Sink()
    tick(conn, sink, "2026-11-06T09:00:00Z")
    ingest_events(conn, [ev(1, urgency=0), ev(2, urgency=1), ev(3, urgency=2), ev(4, urgency=3)])
    assert tick(conn, sink, "2026-11-06T12:00:00Z") == 2
    assert sink.sent == ["event 3", "event 4"]


def test_expired_retracted_and_superseded_events_stay_quiet(conn):
    sink = Sink()
    tick(conn, sink, "2026-11-06T09:00:00Z")
    gone = {"source": {"id": "t", "ref": "x"}, "urgency": 0}
    ingest_events(
        conn,
        [
            ev(1, expires_at="2026-11-06T11:00:00Z"),
            ev(2),
            ev(3),
            ev(4, **gone, refs=[{"rel": "retracts", "id": ev(2)["id"]}], title="gone"),
            ev(5, **{**gone, "source": {"id": "t", "ref": "y"}}, title="new version",
               refs=[{"rel": "supersedes", "id": ev(3)["id"]}]),
        ],
    )  # fmt: skip
    assert tick(conn, sink, "2026-11-06T12:00:00Z") == 0


def test_failed_publish_is_retried_and_then_sent_once(conn):
    tick(conn, Sink(), "2026-11-06T09:00:00Z")
    ingest_events(conn, [ev(1)])

    def broken(_):
        raise OSError("ntfy unreachable")

    assert tick(conn, broken, "2026-11-06T12:00:00Z") == 0
    sink = Sink()
    assert tick(conn, sink, "2026-11-06T12:00:30Z") == 1
    assert tick(conn, sink, "2026-11-06T12:01:00Z") == 0
    assert sink.sent == ["event 1"]
    assert conn.execute("SELECT status FROM deliveries").fetchall()[0][0] == "sent"


def test_a_claim_left_by_a_crash_is_not_resent(conn):
    tick(conn, Sink(), "2026-11-06T09:00:00Z")
    ingest_events(conn, [ev(1)])
    conn.execute(
        "INSERT INTO deliveries (seq, channel, status, created_at)"
        " VALUES (1, 'ntfy', 'pending', '2026-11-06T11:00:00Z')"
    )  # process died between claim and publish
    sink = Sink()
    assert tick(conn, sink, "2026-11-06T12:00:00Z") == 0 and sink.sent == []


def test_channels_are_independent(conn):
    a, b = Sink(), Sink()
    tick(conn, a, "2026-11-06T09:00:00Z", channel="ntfy")
    tick(conn, b, "2026-11-06T09:00:00Z", channel="other")
    ingest_events(conn, [ev(1)])
    assert tick(conn, a, "2026-11-06T12:00:00Z", channel="ntfy") == 1
    assert tick(conn, b, "2026-11-06T12:00:00Z", channel="other") == 1


def test_event_log_is_untouched_by_delivery(conn):
    tick(conn, Sink(), "2026-11-06T09:00:00Z")
    ingest_events(conn, [ev(1)])
    before = [tuple(r) for r in conn.execute("SELECT * FROM events")]
    tick(conn, Sink(), "2026-11-06T12:00:00Z")
    assert [tuple(r) for r in conn.execute("SELECT * FROM events")] == before


def test_old_databases_gain_the_delivery_tables(tmp_path):
    path = tmp_path / "old.db"
    c = connect(path)
    c.executescript("DROP TABLE deliveries; DROP TABLE notify_state; PRAGMA user_version = 2;")
    c.close()
    c = connect(path)
    c.execute("SELECT * FROM deliveries").fetchall()
    c.execute("SELECT * FROM notify_state").fetchall()
    c.close()


# --- CLI ---------------------------------------------------------------------


def test_cli_notify_once_requires_an_ntfy_url(capsys, db_path, monkeypatch):
    monkeypatch.delenv("NTFY_URL", raising=False)
    assert cli.main(["notify", "--once", "--db", str(db_path)]) == 1
    assert "NTFY_URL" in capsys.readouterr().err


def test_cli_notify_once_publishes_and_never_prints_the_topic_url(capsys, db_path):
    c = connect(db_path)
    tick(c, Sink(), "2026-01-01T00:00:00Z")
    ingest_events(c, [ev(1, visible_at="2026-01-02T00:00:00Z")])
    c.close()
    sent = []
    code = cli.main(
        [
            "notify",
            "--once",
            "--db",
            str(db_path),
            "--ntfy-url",
            "https://ntfy.example/s3cret-topic",
        ],
        publish=lambda url, event: sent.append((url, event["title"])),
    )
    out = capsys.readouterr()
    assert code == 0 and sent == [("https://ntfy.example/s3cret-topic", "event 1")]
    assert "s3cret-topic" not in out.out + out.err
