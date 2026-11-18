"""Accounts store (M16a): invites are single-use secrets stored hashed; redemption is atomic."""

import sqlite3
import threading

import pytest

from dayfile.web.accounts import store

NOW = "2026-11-10T09:00:00Z"
LATER = "2026-11-20T09:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "accounts.db")
    yield c
    c.close()


def signup(conn, code, gid=1, login="ann", now=NOW):
    return store.create_account(conn, code, github_id=gid, github_login=login, now=now)


def test_invite_code_has_a_readable_shape_and_is_stored_only_as_a_hash(conn):
    code = store.create_invite(conn, note="for ann", now=NOW)
    assert len(code) == 14 and code.count("-") == 2
    dump = " ".join(str(v) for row in conn.execute("SELECT * FROM invite") for v in row)
    assert code not in dump and code.replace("-", "") not in dump


def test_redeeming_creates_the_account_and_records_who_used_what(conn):
    code = store.create_invite(conn, note="x", now=NOW)
    account = signup(conn, code)
    assert account["github_id"] == 1 and account["status"] == "active"
    assert conn.execute("SELECT count(*) FROM redeem").fetchone()[0] == 1
    assert store.get_account(conn, github_id=1)["github_login"] == "ann"


def test_codes_are_forgiving_about_case_spacing_and_lookalike_letters(conn):
    code = store.create_invite(conn, note="x", now=NOW)
    sloppy = code.lower().replace("-", " ").replace("0", "o").replace("1", "l")
    assert signup(conn, sloppy)["github_id"] == 1


def test_a_single_use_code_works_once(conn):
    code = store.create_invite(conn, note="x", now=NOW)
    signup(conn, code, gid=1)
    with pytest.raises(store.InviteError):
        signup(conn, code, gid=2, login="bob")
    assert store.get_account(conn, github_id=2) is None


def test_multi_use_codes_stop_at_their_limit(conn):
    code = store.create_invite(conn, note="x", uses=2, now=NOW)
    signup(conn, code, gid=1)
    signup(conn, code, gid=2, login="bob")
    with pytest.raises(store.InviteError):
        signup(conn, code, gid=3, login="cy")


@pytest.mark.parametrize("bad", ["", "nope", "AAAA-AAAA-AAAA", "x" * 200])
def test_unknown_codes_are_refused_without_creating_anything(conn, bad):
    with pytest.raises(store.InviteError):
        signup(conn, bad)
    assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 0


def test_expired_and_revoked_codes_are_refused_with_the_same_error(conn):
    expired = store.create_invite(conn, note="x", expires_at="2026-11-10T08:00:00Z", now=NOW)
    revoked = store.create_invite(conn, note="y", now=NOW)
    store.revoke_invite(conn, store.list_invites(conn)[1]["id"], now=NOW)
    messages = set()
    for code in (expired, revoked, "ZZZZ-ZZZZ-ZZZZ"):
        with pytest.raises(store.InviteError) as e:
            signup(conn, code)
        messages.add(str(e.value))
    assert len(messages) == 1  # does not reveal which case it was


def test_an_existing_github_identity_does_not_burn_another_invite(conn):
    first = store.create_invite(conn, note="x", now=NOW)
    second = store.create_invite(conn, note="y", now=NOW)
    signup(conn, first, gid=1)
    with pytest.raises(store.AccountExists):
        signup(conn, second, gid=1)
    assert conn.execute("SELECT uses FROM invite ORDER BY rowid").fetchall()[1][0] == 0


def test_two_simultaneous_redemptions_of_one_single_use_code_yield_one_account(tmp_path):
    path = tmp_path / "a.db"
    setup = store.connect(path)
    code = store.create_invite(setup, note="x", now=NOW)
    setup.close()
    results, barrier = [], threading.Barrier(2)

    def attempt(gid):
        c = store.connect(path)
        barrier.wait()
        try:
            signup(c, code, gid=gid, login=f"u{gid}")
            results.append("ok")
        except store.InviteError:
            results.append("refused")
        finally:
            c.close()

    threads = [threading.Thread(target=attempt, args=(g,)) for g in (1, 2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["ok", "refused"]
    check = store.connect(path)
    assert check.execute("SELECT count(*) FROM account").fetchone()[0] == 1
    check.close()


def test_the_audit_log_is_append_only(conn):
    store.create_invite(conn, note="x", now=NOW)
    assert conn.execute("SELECT count(*) FROM audit").fetchone()[0] == 1
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE audit SET action = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM audit")


def test_audit_entries_never_hold_the_code(conn):
    code = store.create_invite(conn, note="x", now=NOW)
    signup(conn, code)
    rows = " ".join(str(v) for row in conn.execute("SELECT * FROM audit") for v in row)
    assert code not in rows and code.replace("-", "") not in rows


def test_suspending_an_account_is_reversible_and_audited(conn):
    signup(conn, store.create_invite(conn, note="x", now=NOW))
    store.set_status(conn, "ann", "suspended", now=LATER)
    assert store.get_account(conn, github_id=1)["status"] == "suspended"
    store.set_status(conn, "ann", "active", now=LATER)
    assert store.get_account(conn, github_id=1)["status"] == "active"
    actions = [r[0] for r in conn.execute("SELECT action FROM audit ORDER BY id")]
    assert actions.count("account.status") == 2


def test_the_database_file_is_private(tmp_path):
    store.connect(tmp_path / "accounts.db").close()
    assert (tmp_path / "accounts.db").stat().st_mode & 0o077 == 0


def test_listing_invites_shows_state_but_never_a_code(conn):
    store.create_invite(conn, note="for ann", uses=3, now=NOW)
    (row,) = store.list_invites(conn)
    assert row["note"] == "for ann" and row["uses"] == 0 and row["max_uses"] == 3
    assert "code" not in row and "code_hash" not in row
