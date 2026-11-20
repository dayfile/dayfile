import pytest
from fastapi.testclient import TestClient

from dayfile import cli
from dayfile.app import create_app
from dayfile.auth.tokens import create_token
from dayfile.db import connect
from dayfile.gen import review

NOW = "2026-11-19T12:00:00Z"


@pytest.fixture
def seeded(tmp_path):
    db = tmp_path / "d.db"
    conn = connect(db)
    made = review.generate(conn, NOW)
    read = create_token(conn, ["self"]).value
    conn.close()
    client = TestClient(create_app(db, clock=lambda: NOW))
    return db, made, lambda path: client.get(path, headers={"Authorization": f"Bearer {read}"})


def titles(response):
    assert response.status_code == 200, response.text
    return [e["title"] for e in response.json()["events"]]


def test_a_week_of_backups_with_one_failure(seeded):
    _, _, get = seeded
    found = titles(get("/events?tag=backup&limit=100"))
    assert sum(t == "Nightly backup finished" for t in found) == 6
    assert "Nightly backup FAILED" in found


def test_deploys_include_a_rollback(seeded):
    _, _, get = seeded
    found = titles(get("/events?tag=deploy&limit=100"))
    assert any(t.startswith("Deployed web") for t in found)
    assert any(t.startswith("Rolled back") for t in found)


def test_todos_are_open_and_closed_and_one_comes_from_the_heartbeat(seeded):
    _, made, get = seeded
    open_ = titles(get("/events?tag=todo&status=open&limit=100"))
    closed = titles(get("/events?tag=todo&status=closed&limit=100"))
    assert "Renew the TLS certificate for staging.example.net" in open_
    assert any(t.startswith("weekly-report: nothing from cron:weekly-report") for t in open_)
    assert sorted(closed) == [
        "Investigate the failed nightly backup",
        "Rotate the API signing key",
    ]
    assert made["heartbeat_todos"] == 1
    assert len(titles(get("/events?tag=heartbeat&status=open"))) == 1


def test_running_it_again_adds_nothing(seeded):
    db, _, _ = seeded
    conn = connect(db)
    before = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    again = review.generate(conn, "2026-11-20T09:00:00Z")  # even a day later
    after = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    conn.close()
    assert after == before and again["events"] == 0


def test_the_cli_command_seeds_a_database(tmp_path, capsys):
    db = tmp_path / "x.db"
    assert cli.main(["gen", "review-log", "--db", str(db)]) == 0
    assert "new events" in capsys.readouterr().out
    conn = connect(db)
    assert (
        conn.execute("SELECT count(*) FROM events").fetchone()[0] == 19
    )  # 7 backups, 5 deploys, 4 to-dos, 2 closings, 1 heartbeat
    conn.close()
