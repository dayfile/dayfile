import copy
import json
import os
import stat

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dayfile import cli, signing
from dayfile.db import connect
from dayfile.ingest.core import IngestError, ingest_events, prepare
from helpers import authed_client


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "d.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def key_dir(tmp_path):
    return tmp_path / "keys"


def rich_event():
    return {
        "specversion": "dayfile/1",
        "id": "01JC8Z4Q7M2X9K3T5W6N8R0VBA",
        "source": {"id": "adapter:email-in", "ref": "msgid-9f2c7a"},
        "occurred_at": "2026-11-05T14:02:11Z",
        "visible_at": "2026-11-07T09:00:00Z",
        "expires_at": "2026-12-01T00:00:00Z",
        "title": "Renew passport",
        "body": "Needs photo.",
        "tags": ["admin", "family"],
        "urgency": 2,
        "payload": {"due": "2026-11-14", "n": 1.5, "nested": {"b": 1, "a": [1, 2]}},
        "refs": [{"rel": "replies-to", "id": "01JC8Z5A1B2C3D4E5F6G7H8J9K"}],
        "links": [{"rel": "related", "href": "https://example.com/form"}],
        "audience": ["self", "family"],
    }


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


# --- canonical form and verification -----------------------------------------


def test_signature_roundtrip(key):
    signed = signing.sign_event(rich_event(), "k1", key)
    assert signed["sig"]["kid"] == "k1" and signed["sig"]["alg"] == "ed25519"
    assert signing.verify_event(signed, key.public_key())


def test_signing_is_independent_of_key_order_and_unicode_escapes(key):
    a = rich_event()
    a["title"] = "Café"
    b = json.loads(json.dumps(dict(reversed(list(a.items())))))
    sig_a = signing.sign_event(a, "k", key)["sig"]["value"]
    sig_b = signing.sign_event(b, "k", key)["sig"]["value"]
    assert sig_a == sig_b  # Ed25519 is deterministic, so equal canonical bytes give equal sigs


MUTATIONS = {
    "id": lambda e: e.update(id="01JC8Z4Q7M2X9K3T5W6N8R0VBB"),
    "type": lambda e: e.update(type="note"),
    "source.id": lambda e: e["source"].update(id="other"),
    "source.ref": lambda e: e["source"].update(ref="other"),
    "occurred_at": lambda e: e.update(occurred_at="2026-11-05T14:02:12Z"),
    "visible_at": lambda e: e.update(visible_at="2026-11-07T09:00:01Z"),
    "expires_at": lambda e: e.update(expires_at="2026-12-02T00:00:00Z"),
    "expires_at removed": lambda e: e.pop("expires_at"),
    "title": lambda e: e.update(title="Renew passport!"),
    "body": lambda e: e.update(body="Needs photos."),
    "body removed": lambda e: e.pop("body"),
    "tags": lambda e: e["tags"].append("extra"),
    "tags reordered": lambda e: e["tags"].reverse(),
    "urgency": lambda e: e.update(urgency=3),
    "payload value": lambda e: e["payload"].update(due="2026-11-15"),
    "payload nested": lambda e: e["payload"]["nested"]["a"].append(3),
    "payload key added": lambda e: e["payload"].update(x=1),
    "refs": lambda e: e["refs"][0].update(rel="completes"),
    "links": lambda e: e["links"][0].update(href="https://evil.example"),
    "audience": lambda e: e["audience"].append("ops"),
    "specversion": lambda e: e.update(specversion="dayfile/2"),
}


@pytest.mark.parametrize("name", MUTATIONS)
def test_altering_any_signed_field_fails_verification(key, name):
    signed = signing.sign_event(rich_event(), "k", key)
    tampered = copy.deepcopy(signed)
    MUTATIONS[name](tampered)
    assert not signing.verify_event(tampered, key.public_key())


def test_server_assigned_fields_are_outside_the_signature(key):
    signed = signing.sign_event(rich_event(), "k", key)
    stored_like = {**signed, "seq": 99, "recorded_at": "2030-01-01T00:00:00Z", "status": "done"}
    assert signing.verify_event(stored_like, key.public_key())
    stored_like["recorded_at"] = "2031-02-03T04:05:06Z"
    assert signing.verify_event(stored_like, key.public_key())


def test_wrong_key_malformed_and_missing_signatures_fail(key):
    signed = signing.sign_event(rich_event(), "k", key)
    other = Ed25519PrivateKey.generate().public_key()
    assert not signing.verify_event(signed, other)
    for value in ["", "!!!not base64!!!", "AAAA"]:
        assert not signing.verify_event(
            {**signed, "sig": {**signed["sig"], "value": value}}, key.public_key()
        )
    assert not signing.verify_event(rich_event(), key.public_key())


def test_defaults_do_not_change_the_signed_form(key):
    bare = rich_event()
    for k in ("visible_at", "audience", "urgency", "expires_at"):
        bare.pop(k)
    a = signing.sign_event(bare, "k", key)
    explicit = {**bare, "visible_at": bare["occurred_at"], "audience": ["self"], "urgency": 0}
    assert signing.verify_event({**explicit, "sig": a["sig"]}, key.public_key())


# --- key store ---------------------------------------------------------------


def test_create_key_writes_a_private_file_and_registers_the_public_key(conn, key_dir):
    kid = signing.create_key(conn, key_dir, note="laptop")
    path = key_dir / f"{kid}.pem"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(key_dir).st_mode) == 0o700
    private = signing.load_private_key(key_dir, kid)
    assert (
        signing.public_key_for(conn, kid).public_bytes_raw()
        == private.public_key().public_bytes_raw()
    )
    assert [k["kid"] for k in signing.list_keys(conn)] == [kid]
    assert "PRIVATE" not in str(signing.list_keys(conn))


def test_key_ids_cannot_escape_the_key_directory(conn, key_dir):
    for bad in ["../evil", "a/b", "", ".hidden/..", "x" * 100, "sp ace"]:
        with pytest.raises(ValueError):
            signing.create_key(conn, key_dir, kid=bad)
        with pytest.raises(ValueError):
            signing.load_private_key(key_dir, bad)


def test_create_key_never_overwrites(conn, key_dir):
    signing.create_key(conn, key_dir, kid="mine")
    with pytest.raises(ValueError):
        signing.create_key(conn, key_dir, kid="mine")


def test_import_public_key(conn, key):
    raw = key.public_key().public_bytes_raw()
    signing.import_public_key(conn, "friend", signing.b64(raw))
    assert signing.public_key_for(conn, "friend").public_bytes_raw() == raw
    with pytest.raises(ValueError):
        signing.import_public_key(conn, "bad", "AAAA")


# --- ingest and read-time status ---------------------------------------------


def test_valid_signature_is_stored_unchanged_and_reported_valid(conn, key_dir):
    kid = signing.create_key(conn, key_dir)
    signed = signing.sign_event(rich_event(), kid, signing.load_private_key(key_dir, kid))
    [res] = ingest_events(conn, [signed])
    assert res.event["sig"] == signed["sig"]
    assert signing.status(conn, res.event) == "valid"


def test_stored_event_still_verifies_after_a_database_roundtrip(conn, key_dir):
    kid = signing.create_key(conn, key_dir)
    key = signing.load_private_key(key_dir, kid)
    bare = {k: v for k, v in rich_event().items() if k not in ("urgency", "expires_at", "audience")}
    bare["tags"] = []  # empty lists are dropped by the store
    [res] = ingest_events(conn, [signing.sign_event(bare, kid, key)])
    assert signing.verify_event(res.event, key.public_key())


def test_tampered_signed_event_is_rejected_and_nothing_is_written(conn, key_dir):
    kid = signing.create_key(conn, key_dir)
    signed = signing.sign_event(rich_event(), kid, signing.load_private_key(key_dir, kid))
    signed["title"] = "Something else"
    with pytest.raises(IngestError, match="signature"):
        ingest_events(conn, [signed])
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_unknown_key_is_stored_and_becomes_valid_when_the_key_is_registered(conn, key):
    signed = signing.sign_event(rich_event(), "friend", key)
    [res] = ingest_events(conn, [signed])
    assert signing.status(conn, res.event) == "unknown-key"
    signing.import_public_key(conn, "friend", signing.b64(key.public_key().public_bytes_raw()))
    assert signing.status(conn, res.event) == "valid"


def test_unknown_key_with_a_garbage_signature_reads_as_invalid(conn):
    forged = {**rich_event(), "sig": {"kid": "nobody", "alg": "ed25519", "value": "AAAA"}}
    [res] = ingest_events(conn, [forged])
    assert signing.status(conn, res.event) == "invalid"


def test_unsigned_events_have_no_status(conn):
    [res] = ingest_events(conn, [rich_event()])
    assert signing.status(conn, res.event) is None


def test_feed_exposes_sig_status(db_path, conn, key_dir):
    kid = signing.create_key(conn, key_dir)
    signed = signing.sign_event(rich_event(), kid, signing.load_private_key(key_dir, kid))
    plain = {**rich_event(), "id": "01JC8Z4Q7M2X9K3T5W6N8R0VBC", "source": {"id": "x", "ref": "y"}}
    ingest_events(conn, [signed, plain])
    client = authed_client(db_path, labels=("self",), clock=lambda: "2026-12-01T00:00:00Z")
    items = {
        i["id"].split(":")[-1]: i["_dayfile"] for i in client.get("/feed.json").json()["items"]
    }
    assert items[signed["id"]]["sig_status"] == "valid" and items[signed["id"]]["sig"]["kid"] == kid
    assert "sig_status" not in items[plain["id"]]


def test_http_rejects_a_forged_signature(db_path, conn, key_dir):
    kid = signing.create_key(conn, key_dir)
    signed = signing.sign_event(rich_event(), kid, signing.load_private_key(key_dir, kid))
    signed["payload"]["due"] = "2030-01-01"
    client = authed_client(db_path)
    r = client.post("/events", json=signed)
    assert r.status_code == 422 and "signature" in r.json()["error"]


def test_prepare_still_validates_signed_events(key):
    prepare(signing.sign_event(rich_event(), "k", key))


# --- CLI ---------------------------------------------------------------------


def test_cli_key_create_list_and_import(capsys, db_path, key_dir):
    assert (
        cli.main(
            ["key", "create", "--kid", "laptop", "--key-dir", str(key_dir), "--db", str(db_path)]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "laptop" in out and "PRIVATE" not in out
    assert cli.main(["key", "list", "--db", str(db_path)]) == 0
    assert "laptop" in capsys.readouterr().out
    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    code = cli.main(
        ["key", "import", "--kid", "friend", "--public", signing.b64(other), "--db", str(db_path)]
    )
    assert code == 0
    assert (
        cli.main(
            ["key", "create", "--kid", "laptop", "--key-dir", str(key_dir), "--db", str(db_path)]
        )
        == 1
    )


def test_cli_add_sign_produces_a_verifiable_event(db_path, key_dir):
    conn = connect(db_path)
    kid = signing.create_key(conn, key_dir)
    sent = []
    code = cli.main(
        ["add", "Signed note", "--tag", "a", "--sign", kid, "--key-dir", str(key_dir)],
        post=lambda url, events: sent.append(events[0]) or (201, {**events[0], "seq": 1}),
    )
    assert code == 0
    pub = signing.public_key_for(conn, kid)
    assert signing.verify_event(sent[0], pub)
    [res] = ingest_events(conn, sent)  # and the server accepts it
    assert signing.status(conn, res.event) == "valid"
    conn.close()
