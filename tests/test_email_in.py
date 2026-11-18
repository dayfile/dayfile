import hashlib
from email.message import EmailMessage

import pytest

from dayfile.db import connect
from dayfile.ingest.core import ingest_events, prepare
from dayfile.ingest.email_in import message_to_event, poll_once


def make(subject="Hello", body="Some text", msgid="<abc@example.com>", frm="a@example.com",
         date="Fri, 06 Nov 2026 15:00:00 +0200"):  # fmt: skip
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = frm, subject, date
    if msgid:
        m["Message-ID"] = msgid
    m.set_content(body)
    return m.as_bytes()


VOICE_BODY = (
    "<https://voice.google.com>\nCall plumber tomorrow\nand bring the invoice\n\n"
    "YOUR ACCOUNT <https://voice.google.com> HELP CENTER\n"
    "<https://support.google.com/voice#topic=1707989> HELP FORUM\n"
)


def voice(body=VOICE_BODY, msgid="<+1408.deadbeef@txt.voice.google.com>"):
    return make(
        subject="New text message from Gg (408) 658-2587",
        body=body,
        msgid=msgid,
        frm='"Gg (SMS)" <14088007815.14086582587.tok@txt.voice.google.com>',
        date="Fri, 06 Nov 2026 20:05:00 +0000",
    )


def test_plain_email_becomes_valid_note():
    ev = message_to_event(make())
    prepare(ev)  # validates against the schema
    assert "tags" not in ev  # a plain note has no status
    assert ev["title"] == "Hello"
    assert ev["body"] == "Some text"
    assert ev["source"]["id"] == "adapter:email-in"
    assert ev["occurred_at"] == "2026-11-06T13:00:00Z"


def test_source_ref_is_hash_of_message_id():
    ev = message_to_event(make(msgid="<abc@example.com>"))
    assert ev["source"]["ref"] == hashlib.sha256(b"<abc@example.com>").hexdigest()
    other = message_to_event(make(msgid="<zzz@example.com>"))
    assert other["source"]["ref"] != ev["source"]["ref"]


def test_missing_message_id_falls_back_to_content_hash():
    raw = make(msgid=None)
    assert message_to_event(raw)["source"]["ref"] == message_to_event(raw)["source"]["ref"]


@pytest.mark.parametrize(
    "subject", ["todo: Call plumber", "TODO:Call plumber", "Todo:  Call plumber"]
)
def test_todo_prefix_tags_todo_and_strips_prefix(subject):
    ev = message_to_event(make(subject=subject))
    assert ev["tags"] == ["todo"]
    assert ev["title"] == "Call plumber"


def test_empty_subject_gets_placeholder_title():
    ev = message_to_event(make(subject=""))
    prepare(ev)
    assert ev["title"]


def test_long_subject_is_truncated_to_schema_limit():
    ev = message_to_event(make(subject="x" * 500))
    prepare(ev)
    assert len(ev["title"]) <= 300


def test_google_voice_sms_uses_text_not_subject():
    ev = message_to_event(voice())
    prepare(ev)
    assert ev["title"] == "Call plumber tomorrow"
    assert ev["body"] == "Call plumber tomorrow\nand bring the invoice"
    assert ev["tags"] == ["sms"]
    assert ev["payload"] == {"from_name": "Gg", "from_number": "(408) 658-2587"}
    assert ev["occurred_at"] == "2026-11-06T20:05:00Z"


def test_google_voice_sms_todo_prefix():
    ev = message_to_event(voice(body=VOICE_BODY.replace("Call plumber", "todo: Call plumber", 1)))
    assert ev["tags"] == ["sms", "todo"]
    assert ev["title"] == "Call plumber tomorrow"


def test_redelivery_is_idempotent(tmp_path):
    conn = connect(tmp_path / "d.db")
    for _ in range(2):
        ingest_events(conn, [message_to_event(voice())])
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


class FakeImap:
    """Mimics imaplib's UID search/fetch, including Gmail's `UID n:*` returning the last message."""

    def __init__(self, messages):
        self.messages = messages  # {uid: raw bytes}
        self.selected = None

    def select(self, folder, readonly=False):
        self.selected = (folder, readonly)
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        if command == "search":
            start = int(args[-1].removeprefix("UID ").split(":")[0])
            uids = [u for u in sorted(self.messages) if u >= start] or [max(self.messages)]
            return "OK", [" ".join(map(str, uids)).encode()]
        uid = int(args[0])
        return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.messages[uid]), b")"]


def test_poll_ingests_new_messages_and_advances_cursor(tmp_path):
    conn = connect(tmp_path / "d.db")
    imap = FakeImap({1: make(msgid="<1@x>"), 2: make(msgid="<2@x>"), 3: voice()})
    last = poll_once(imap, conn, "SMS", 0)
    assert last == 3
    assert imap.selected == ("SMS", True)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 3


def test_poll_does_not_refetch_the_gmail_last_message_quirk(tmp_path):
    conn = connect(tmp_path / "d.db")
    imap = FakeImap({1: make(msgid="<1@x>"), 2: make(msgid="<2@x>")})
    assert poll_once(imap, conn, "SMS", 2) == 2
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_poll_from_zero_twice_is_idempotent(tmp_path):
    conn = connect(tmp_path / "d.db")
    imap = FakeImap({1: make(msgid="<1@x>"), 2: voice()})
    poll_once(imap, conn, "SMS", 0)
    poll_once(imap, conn, "SMS", 0)  # e.g. after a process restart
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_poll_tolerates_an_unparsable_date(tmp_path):
    conn = connect(tmp_path / "d.db")
    bad = make(date="not a date")
    imap = FakeImap({1: bad, 2: make(msgid="<2@x>")})
    assert poll_once(imap, conn, "SMS", 0) == 2
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
