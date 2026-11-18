import pytest

from dayfile import cli
from dayfile.db import connect
from dayfile.models import validate_event


def build(*argv):
    return cli.build_event(cli.parse_args(["add", *argv]))


def test_minimal_add_is_valid_and_has_no_tags():
    ev = build("Buy milk")
    validate_event(ev)
    assert "tags" not in ev
    assert ev["title"] == "Buy milk"
    assert ev["source"]["id"] == "adapter:cli"
    assert ev["source"]["ref"] == ev["id"]
    assert ev["occurred_at"].endswith("Z")


def test_tags_due_and_visible_at():
    ev = build("Renew passport", "--tag", "admin", "--tag", "family", "--due", "2026-11-14",
               "--visible-at", "2026-11-07T11:00:00+02:00")  # fmt: skip
    validate_event(ev)
    assert ev["tags"] == ["admin", "family", "todo"]  # a due date makes it a to-do
    assert ev["payload"] == {"due": "2026-11-14"}
    assert ev["visible_at"] == "2026-11-07T09:00:00Z"


def test_todo_flag_urgency_body_audience():
    ev = build("x", "--todo", "--due", "2026-11-14", "--urgency", "2", "--body", "details",
               "--audience", "family")  # fmt: skip
    validate_event(ev)
    assert ev["tags"] == ["todo"]
    assert (ev["urgency"], ev["body"], ev["audience"]) == (2, "details", ["family"])


def test_naive_visible_at_is_rejected():
    with pytest.raises(SystemExit):
        build("x", "--visible-at", "2026-11-07T09:00:00")


def test_main_posts_event_and_prints_seq(capsys):
    sent = {}

    def fake_post(url, events):
        sent["url"], sent["events"] = url, events
        return 201, {**events[0], "seq": 7}

    code = cli.main(["--url", "http://x:1", "add", "Buy milk"], post=fake_post)
    assert code == 0
    assert sent["url"] == "http://x:1/events"
    assert sent["events"][0]["title"] == "Buy milk"
    assert "seq 7" in capsys.readouterr().out


def test_main_reports_server_rejection(capsys):
    code = cli.main(["add", "x"], post=lambda url, events: (422, {"error": "bad title"}))
    assert code == 1
    assert "bad title" in capsys.readouterr().err


def test_add_with_db_writes_directly_without_a_server(tmp_path, capsys):
    db = tmp_path / "d.db"

    def no_network(url, events):
        raise AssertionError("must not contact a server")

    assert cli.main(["add", "direct note", "--tag", "x", "--db", str(db)], post=no_network) == 0
    assert "seq 1" in capsys.readouterr().out
    conn = connect(db)
    row = conn.execute("SELECT title, source_id FROM events").fetchone()
    assert (row["title"], row["source_id"]) == ("direct note", "adapter:cli")


def test_add_with_db_reports_invalid_events(tmp_path, capsys):
    assert cli.main(["add", "", "--db", str(tmp_path / "d.db")]) == 1  # empty title is invalid
    assert "dayfile:" in capsys.readouterr().err


def test_feed_commands_create_list_publish_and_delete(tmp_path, capsys):
    from dayfile.cli import main

    db = str(tmp_path / "d.db")
    assert main(["feed", "set", "work", "--tag", "work", "--status", "open", "--db", db]) == 0
    assert "private, served at /f/work.json" in capsys.readouterr().out
    assert main(["feed", "set", "work", "--public", "--title", "Work", "--db", db]) == 0
    capsys.readouterr()
    main(["feed", "list", "--db", db])
    listing = capsys.readouterr().out
    assert "work" in listing and "public" in listing and "tag=work&status=open" in listing
    assert main(["feed", "delete", "work", "--db", db]) == 0
    assert main(["feed", "delete", "work", "--db", db]) == 1
    assert main(["feed", "set", "Bad_Name", "--db", db]) == 1
    assert main(["feed", "set", "default", "--tag", "x", "--db", db]) == 1
