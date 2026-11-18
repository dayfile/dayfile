import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.cloudevents import from_cloudevent, to_cloudevent
from dayfile.db import connect
from dayfile.ingest.core import ingest_events, prepare
from helpers import authed_client

EXAMPLES = [
    json.loads(line)
    for line in (Path(__file__).parents[1] / "examples" / "events.jsonl").read_text().splitlines()
    if line.strip()
]
NOW = "2026-11-06T12:00:00Z"
EXT_NAME = re.compile(r"^[a-z0-9]{1,20}$")
CORE = {
    "specversion",
    "id",
    "source",
    "type",
    "datacontenttype",
    "dataschema",
    "subject",
    "time",
    "data",
}


def stored(events):
    conn = connect(":memory:")
    out = [r.event for r in ingest_events(conn, events)]
    conn.close()
    return out


def envelope(ev):
    return {k: v for k, v in ev.items() if k not in ("seq", "recorded_at", "status")}


# --- export ------------------------------------------------------------------


def test_required_attributes_and_types():
    [ev] = stored([EXAMPLES[0]])
    ce = to_cloudevent(ev)
    assert ce["specversion"] == "1.0" and ce["id"] == ev["id"] and ce["type"] == "app.dayfile.event"
    assert ce["source"] == "adapter:email-in"
    assert ce["time"] == "2026-11-05T14:02:11Z"
    assert ce["datacontenttype"] == "application/json" and ce["data"] == {"due": "2026-11-14"}


def test_extension_attributes_follow_the_cloudevents_rules():
    for ev in stored(EXAMPLES):
        ce = to_cloudevent(ev)
        for name, value in ce.items():
            if name in CORE:
                continue
            assert EXT_NAME.match(name), name  # lowercase alphanumerics, at most 20
            assert isinstance(value, str | int | bool), (name, value)  # no arrays or objects


def test_tags_and_audience_become_delimited_strings():
    [ev] = stored([EXAMPLES[2]])
    ce = to_cloudevent(ev)
    assert ce["dayfileaudience"] == "self,family" and ce["dayfiletags"] == "weather"
    assert ce["dayfiletitle"] == ev["title"] and ce["dayfilespec"] == "dayfile/1"


def test_event_without_payload_has_no_data():
    [ev] = stored([EXAMPLES[3]])
    ce = to_cloudevent(ev)
    assert "data" not in ce and "datacontenttype" not in ce


def test_server_assigned_position_is_exported_for_information():
    [ev] = stored([EXAMPLES[0]])
    ce = to_cloudevent(ev)
    assert ce["dayfileseq"] == ev["seq"] and ce["dayfilerecordedat"] == ev["recorded_at"]


# --- round trip --------------------------------------------------------------


@pytest.mark.parametrize("raw", EXAMPLES, ids=lambda e: e["id"][-6:])
def test_dayfile_events_roundtrip_losslessly(raw):
    [ev] = stored([raw])
    back = from_cloudevent(to_cloudevent(ev), NOW)
    assert back == prepare(envelope(ev))


def test_signature_survives_the_roundtrip():
    ev = {**EXAMPLES[0], "sig": {"kid": "k", "alg": "ed25519", "value": "A" * 86 + "=="}}
    [s] = stored([ev])
    assert from_cloudevent(to_cloudevent(s), NOW)["sig"] == ev["sig"]


def test_source_with_unusual_characters_roundtrips():
    ev = {**EXAMPLES[3], "source": {"id": "device: edge node/7", "ref": "r 1"}}
    [s] = stored([ev])
    ce = to_cloudevent(s)
    assert " " not in ce["source"]
    assert from_cloudevent(ce, NOW)["source"] == ev["source"]


# --- import of foreign events ------------------------------------------------

FOREIGN = {
    "specversion": "1.0",
    "id": "A234-1234-1234",
    "source": "https://github.com/cloudevents/spec/pull/123",
    "type": "com.github.pull_request.opened",
    "subject": "PR 123 opened",
    "time": "2026-11-06T14:30:00+02:00",
    "datacontenttype": "application/json",
    "data": {"action": "opened", "number": 123},
}


def test_foreign_event_becomes_a_valid_dayfile_event():
    ev = from_cloudevent(FOREIGN, NOW)
    prepare(ev)
    assert re.match(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$", ev["id"])
    assert ev["source"] == {"id": FOREIGN["source"], "ref": "A234-1234-1234"}
    assert ev["tags"] == ["com-github-pull_request-opened"]  # the foreign type, kept as a tag
    assert ev["occurred_at"] == "2026-11-06T12:30:00Z"
    assert ev["title"] == "PR 123 opened" and ev["payload"] == FOREIGN["data"]


def test_title_falls_back_to_subject_then_type():
    no_subject = {k: v for k, v in FOREIGN.items() if k != "subject"}
    assert from_cloudevent(no_subject, NOW)["title"] == FOREIGN["type"]


def test_redelivery_of_a_foreign_event_is_idempotent():
    conn = connect(":memory:")
    for _ in range(2):
        ingest_events(conn, [from_cloudevent(FOREIGN, NOW)])
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_missing_time_uses_the_clock():
    assert (
        from_cloudevent({k: v for k, v in FOREIGN.items() if k != "time"}, NOW)["occurred_at"]
        == NOW
    )


def test_a_foreign_type_becomes_a_tag_and_exports_as_ours():
    ce = {**FOREIGN, "type": "io.Knative.Event-Created"}
    ev = from_cloudevent(ce, NOW)
    prepare(ev)
    assert ev["tags"] == ["io-knative-event-created"]
    [kept] = stored([ev])
    assert to_cloudevent(kept)["type"] == "app.dayfile.event"


@pytest.mark.parametrize(
    ("ce", "payload"),
    [
        (
            {**FOREIGN, "data": "plain text", "datacontenttype": "text/plain"},
            {"data": "plain text"},
        ),
        ({**FOREIGN, "data": [1, 2]}, {"data": [1, 2]}),
        (
            {
                **{k: v for k, v in FOREIGN.items() if k not in ("data", "datacontenttype")},
                "data_base64": "aGk=",
            },
            {"data_base64": "aGk="},
        ),
    ],
)
def test_non_object_data_is_wrapped(ce, payload):
    assert from_cloudevent(ce, NOW)["payload"] == payload


@pytest.mark.parametrize("missing", ["id", "source", "type"])
def test_missing_required_attribute(missing):
    with pytest.raises(ValueError, match=missing):
        from_cloudevent({k: v for k, v in FOREIGN.items() if k != missing}, NOW)


@pytest.mark.parametrize(
    "bad", [{"specversion": "0.3"}, {"source": ""}, {"id": ""}, {"time": "yesterday"}]
)
def test_invalid_attributes(bad):
    with pytest.raises(ValueError):
        from_cloudevent({**FOREIGN, **bad}, NOW)


def test_unknown_extensions_are_dropped():
    ev = from_cloudevent({**FOREIGN, "traceparent": "00-abc"}, NOW)
    assert "traceparent" not in json.dumps(ev)


# --- HTTP --------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "d.db"


def test_post_structured_cloudevent(db_path):
    client = authed_client(db_path)
    r = client.post(
        "/events",
        content=json.dumps(FOREIGN),
        headers={"content-type": "application/cloudevents+json"},
    )
    assert r.status_code == 201 and r.json()["source"]["ref"] == "A234-1234-1234"
    again = client.post(
        "/events",
        content=json.dumps(FOREIGN),
        headers={"content-type": "application/cloudevents+json"},
    )
    assert again.status_code == 200


def test_post_batch_and_reject_garbage(db_path):
    client = authed_client(db_path)
    batch = [FOREIGN, {**FOREIGN, "id": "second"}]
    r = client.post(
        "/events",
        content=json.dumps(batch),
        headers={"content-type": "application/cloudevents-batch+json"},
    )
    assert r.status_code == 201 and len(r.json()) == 2
    bad = client.post(
        "/events",
        content=json.dumps({**FOREIGN, "specversion": "9.9"}),
        headers={"content-type": "application/cloudevents+json"},
    )
    assert bad.status_code == 422
    assert TestClient(create_app(db_path)).get("/feed.cloudevents").status_code == 401


def test_export_batch_is_audience_filtered_and_pageable(db_path):
    owner = authed_client(db_path, labels=("self",), clock=lambda: "2026-11-07T10:00:00Z")
    owner.post("/events", json=[*EXAMPLES[:1], {**EXAMPLES[3], "audience": ["ops"]}])
    r = owner.get("/feed.cloudevents")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/cloudevents-batch+json")
    assert [ce["id"] for ce in r.json()] == [EXAMPLES[0]["id"]]  # the ops-only note is not visible
    assert "private" in r.headers["cache-control"] and r.headers["vary"] == "Authorization"
    paged = owner.get("/feed.cloudevents?since_seq=0&limit=1")
    assert 'rel="next"' not in paged.headers.get("link", "")  # only one visible event
    owner.post("/events", json=EXAMPLES[2])
    paged = owner.get("/feed.cloudevents?since_seq=0&limit=1")
    assert 'rel="next"' in paged.headers["link"] and "since_seq=" in paged.headers["link"]


def test_export_then_import_into_another_instance(tmp_path):
    a = authed_client(tmp_path / "a.db", labels=("self",), clock=lambda: "2026-11-09T17:00:00Z")
    a.post("/events", json=EXAMPLES[:4])
    exported = a.get("/feed.cloudevents").text
    b = authed_client(tmp_path / "b.db")
    r = b.post(
        "/events", content=exported, headers={"content-type": "application/cloudevents-batch+json"}
    )
    assert r.status_code == 201
    ids_a = {ce["id"] for ce in json.loads(exported)}
    assert {e["id"] for e in r.json()} == ids_a  # native identity is preserved


def test_every_export_carries_the_one_cloudevents_type():
    conn = connect(":memory:")
    raw = {
        "specversion": "dayfile/1",
        "id": "01JC8Z4Q7M2X9K3T5W6N8R0VBA",
        "source": {"id": "t", "ref": "1"},
        "occurred_at": NOW,
        "title": "plain",
    }
    (result,) = ingest_events(conn, [raw])
    assert to_cloudevent(result.event)["type"] == "app.dayfile.event"
