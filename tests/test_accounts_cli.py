"""The operator CLI for accounts: codes are printed once, listings never show them."""

import json

import pytest

from dayfile.web import admin
from dayfile.web.accounts import store


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "accounts.db")


def test_invite_create_prints_a_code_that_can_be_redeemed(db, capsys):
    assert (
        admin.main(["--db", db, "invite", "create", "--note", "for ann", "--expires", "14d"]) == 0
    )
    code = capsys.readouterr().out.strip()
    conn = store.connect(db)
    assert store.create_account(conn, code, 7, "ann", "2026-01-01T00:00:00Z")["github_id"] == 7


def test_listing_shows_invites_without_codes(db, capsys):
    admin.main(["--db", db, "invite", "create", "--note", "n"])
    code = capsys.readouterr().out.strip()
    admin.main(["--db", db, "invite", "list"])
    out = capsys.readouterr().out
    assert code not in out and json.loads(out)["note"] == "n"


def test_revoke_and_suspend_report_missing_targets(db, capsys):
    assert admin.main(["--db", db, "invite", "revoke", "nope"]) == 1
    assert admin.main(["--db", db, "account", "suspend", "nobody"]) == 1


def test_bad_arguments_are_refused(db, capsys):
    assert admin.main(["--db", db, "invite", "create", "--note", "n", "--uses", "0"]) == 2
    assert admin.main(["--db", db, "invite", "create", "--note", "n", "--expires", "soon"]) == 2


def test_stats_counts_accounts_and_signups_per_day_with_no_names(db, capsys):
    conn = store.connect(db)
    for n, (login, at) in enumerate(
        [
            ("ann", "2026-11-16T09:00:00Z"),
            ("bob", "2026-11-15T23:59:59Z"),
            ("old", "2026-09-01T00:00:00Z"),
        ]
    ):
        code = store.create_invite(conn, "t", at)
        account = store.create_account(conn, code, n + 1, login, at)
        store.claim_slug(conn, account["id"], login, at)
    store.set_status(conn, "bob", "suspended", "2026-11-16T10:00:00Z")
    got = store.account_stats(conn, "2026-11-16T12:00:00Z")
    assert got["accounts"] == {"active": 2, "suspended": 1, "deleted": 0}
    assert len(got["new_per_day"]) == 30 and sum(got["new_per_day"].values()) == 2
    assert got["new_per_day"]["2026-11-16"] == 1 and got["new_per_day"]["2026-11-15"] == 1
    conn.close()
    assert admin.main(["--db", db, "stats", "--exclude", "ann"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["accounts"]["active"] == 1
    assert not any(name in out for name in ("ann", "bob", "old"))


def test_stats_counts_answers_and_lists_them_only_when_asked(db, capsys):
    conn = store.connect(db)
    at = "2026-11-16T09:00:00Z"
    account = store.create_account(conn, store.create_invite(conn, "t", at), 1, "ann", at)
    store.set_answer(conn, account["id"], "my backups", at)
    conn.close()
    admin.main(["--db", db, "stats"])
    out = capsys.readouterr().out
    assert json.loads(out)["with_signup_answer"] == 1 and "my backups" not in out
    admin.main(["--db", db, "stats", "--list-answers"])
    listed = json.loads(capsys.readouterr().out)["signup_answers"]
    assert listed == [{"text": "my backups", "answered_at": at}]


def test_stats_counts_consenting_emails_and_lists_them_only_when_asked(db, capsys):
    conn = store.connect(db)
    at = "2026-11-16T09:00:00Z"
    for n, login in enumerate(["ann", "bob"]):
        account = store.create_account(conn, store.create_invite(conn, "t", at), n + 1, login, at)
        store.set_email(conn, account["id"], f"{login}@example.com", at)
    store.set_status(conn, "bob", "suspended", at)  # a suspended account is not emailed
    conn.close()
    admin.main(["--db", db, "stats"])
    out = capsys.readouterr().out
    assert json.loads(out)["with_contact_email"] == 1 and "@" not in out
    admin.main(["--db", db, "stats", "--list-emails"])
    listed = json.loads(capsys.readouterr().out)["contact_emails"]
    assert [e["email"] for e in listed] == ["ann@example.com"]


def test_backup_writes_a_gzipped_verified_copy_to_stdout(db, tmp_path, capsysbinary):
    import gzip
    import sqlite3

    conn = store.connect(db)
    admin.main(["--db", db, "invite", "create", "--note", "n"])
    code = capsysbinary.readouterr().out.decode().strip()
    store.create_account(conn, code, 7, "ann", "2026-01-01T00:00:00Z")
    conn.close()
    assert admin.main(["--db", db, "backup"]) == 0
    packed = capsysbinary.readouterr().out
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(packed))
    copy = sqlite3.connect(restored)
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT github_login FROM account").fetchone()[0] == "ann"
