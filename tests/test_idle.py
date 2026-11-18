"""IMAP IDLE wait and the persisted UID cursor."""

import socket
import threading
import time

import pytest

from dayfile.db import connect
from dayfile.ingest.email_in import idle_wait, load_cursor, save_cursor, sync
from test_email_in import FakeImap, make, voice


class PairImap:
    """Just enough of imaplib.IMAP4 for idle_wait, over a real socket pair."""

    def __init__(self, sock):
        self.sock = sock
        self.file = sock.makefile("rb")

    def _new_tag(self):
        return b"A001"

    def send(self, data):
        self.sock.sendall(data)

    def readline(self):
        return self.file.readline()


def serve(server, script, log):
    """Play a fake IDLE server: `script` is a list of (delay, bytes) sent after the IDLE command."""

    def run():
        f = server.makefile("rb")
        log.append(f.readline())  # A001 IDLE
        for delay, data in script:
            time.sleep(delay)
            server.sendall(data)
        log.append(f.readline())  # DONE
        server.sendall(b"A001 OK IDLE terminated\r\n")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


@pytest.fixture
def pair():
    a, b = socket.socketpair()
    yield PairImap(a), b
    a.close()
    b.close()


def test_idle_returns_true_on_exists_and_sends_done(pair):
    imap, server = pair
    log = []
    t = serve(server, [(0, b"+ idling\r\n"), (0.1, b"* 5 EXISTS\r\n")], log)
    assert idle_wait(imap, timeout=5) is True
    t.join(2)
    assert log == [b"A001 IDLE\r\n", b"DONE\r\n"]


def test_idle_returns_false_on_quiet_timeout(pair):
    imap, server = pair
    log = []
    t = serve(server, [(0, b"+ idling\r\n")], log)
    started = time.monotonic()
    assert idle_wait(imap, timeout=0.3) is False
    assert time.monotonic() - started < 3
    t.join(2)
    assert log[-1] == b"DONE\r\n"


def test_idle_sees_exists_that_arrived_with_the_continuation(pair):
    imap, server = pair
    log = []
    t = serve(server, [(0, b"+ idling\r\n* 7 EXISTS\r\n")], log)
    assert idle_wait(imap, timeout=0.3) is True
    t.join(2)


def test_idle_ignores_unrelated_untagged_lines(pair):
    imap, server = pair
    log = []
    t = serve(server, [(0, b"+ idling\r\n"), (0.05, b"* OK still here\r\n")], log)
    assert idle_wait(imap, timeout=0.3) is False
    t.join(2)


def test_idle_refused_raises(pair):
    imap, server = pair

    def refuse():
        server.makefile("rb").readline()
        server.sendall(b"A001 BAD IDLE not supported\r\n")

    threading.Thread(target=refuse, daemon=True).start()
    with pytest.raises(Exception, match="IDLE"):
        idle_wait(imap, timeout=1)


def test_cursor_roundtrip_and_validity_reset(tmp_path):
    conn = connect(tmp_path / "d.db")
    assert load_cursor(conn, "SMS") == (None, 0)
    save_cursor(conn, "SMS", "77", 12)
    assert load_cursor(conn, "SMS") == ("77", 12)
    save_cursor(conn, "SMS", "77", 15)
    assert load_cursor(conn, "SMS") == ("77", 15)
    assert load_cursor(conn, "Other") == (None, 0)


class ValidityImap(FakeImap):
    def __init__(self, messages, validity="100"):
        super().__init__(messages)
        self.validity = validity

    def response(self, name):
        return name, [self.validity.encode()]


def test_sync_resumes_after_restart_without_refetching(tmp_path):
    conn = connect(tmp_path / "d.db")
    imap = ValidityImap({1: make(msgid="<1@x>"), 2: voice()})
    assert sync(imap, conn, "SMS") == 2
    imap.messages[3] = make(msgid="<3@x>")
    assert sync(imap, conn, "SMS") == 3  # a fresh sync() call, as after a restart
    assert load_cursor(conn, "SMS") == ("100", 3)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 3


def test_changed_uidvalidity_restarts_from_zero_idempotently(tmp_path):
    conn = connect(tmp_path / "d.db")
    imap = ValidityImap({1: make(msgid="<1@x>"), 2: voice()})
    sync(imap, conn, "SMS")
    imap.validity = "200"  # mailbox was rebuilt: UIDs mean something else now
    imap.messages = {1: voice(msgid="<+1999.other@txt.voice.google.com>"), 5: make(msgid="<5@x>")}
    assert sync(imap, conn, "SMS") == 5
    assert load_cursor(conn, "SMS") == ("200", 5)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 4  # nothing duplicated


def test_sync_without_uidvalidity_support_still_works(tmp_path):
    conn = connect(tmp_path / "d.db")
    assert sync(FakeImap({1: make(msgid="<1@x>")}), conn, "SMS") == 1
