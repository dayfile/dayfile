"""Operations: health check and online backup."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.cli import main
from dayfile.db import connect
from dayfile.ingest.core import ingest_events


def ev(n):
    return {
        "specversion": "dayfile/1",
        "id": f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}"[:26],
        "source": {"id": "t", "ref": f"r{n}"},
        "occurred_at": "2026-11-06T10:00:00Z",
        "title": f"e{n}",
    }


def test_healthz_is_public_and_minimal(tmp_path):
    r = TestClient(create_app(tmp_path / "d.db")).get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert "no-store" in r.headers["cache-control"]


def test_healthz_answers_head_for_uptime_monitors(tmp_path):
    assert TestClient(create_app(tmp_path / "d.db")).head("/healthz").status_code == 200


def test_status_names_the_components(tmp_path):
    r = TestClient(create_app(tmp_path / "d.db")).get("/status")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok"
    assert body["components"] == {"api": "ok", "storage": "ok"} and body["checked_at"]


def test_status_fails_when_the_database_is_unusable(tmp_path):
    bad = tmp_path / "missing" / "x.db"
    r = TestClient(create_app(bad), raise_server_exceptions=False).get("/status")
    assert r.status_code == 503 and r.json()["components"]["storage"] == "unavailable"


def test_healthz_fails_when_the_database_is_unusable(tmp_path):
    bad = tmp_path / "missing-dir" / "d.db"
    r = TestClient(create_app(bad), raise_server_exceptions=False).get("/healthz")
    assert r.status_code == 503 and r.json() == {"status": "unavailable"}


def test_backup_copies_everything_and_passes_integrity_check(tmp_path, capsys):
    src, dst = tmp_path / "d.db", tmp_path / "out" / "copy.db"
    conn = connect(src)
    ingest_events(conn, [ev(1), ev(2)])
    conn.close()
    assert main(["backup", str(dst), "--db", str(src)]) == 0
    copy = sqlite3.connect(dst)
    assert copy.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (dst.stat().st_mode & 0o777) == 0o600  # backups hold private data


def test_backup_refuses_to_overwrite(tmp_path, capsys):
    src, dst = tmp_path / "d.db", tmp_path / "copy.db"
    connect(src).close()
    dst.write_text("precious")
    assert main(["backup", str(dst), "--db", str(src)]) == 1
    assert dst.read_text() == "precious"
    assert "exists" in capsys.readouterr().err


def test_backup_of_a_missing_database_does_not_create_one(tmp_path):
    assert main(["backup", str(tmp_path / "c.db"), "--db", str(tmp_path / "nope.db")]) == 1
    assert not (tmp_path / "nope.db").exists()


@pytest.mark.parametrize("name", ["copy.db"])
def test_backup_leaves_no_partial_file_on_failure(tmp_path, name):
    src = tmp_path / "bad.db"
    src.write_bytes(b"not a database" * 100)
    assert main(["backup", str(tmp_path / name), "--db", str(src)]) == 1
    assert not (tmp_path / name).exists()
