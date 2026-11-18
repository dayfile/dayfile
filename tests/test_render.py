import json
import re
import xml.etree.ElementTree as ET

from dayfile.render.atom import render_atom
from dayfile.render.jsonfeed import render_json

ATOM = "{http://www.w3.org/2005/Atom}"
DF = "{urn:dayfile:1}"
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def event(**over):
    ev = {
        "specversion": "dayfile/1",
        "id": "01JC8Z4Q7M2X9K3T5W6N8R0VBA",
        "source": {"id": "adapter:email-in", "ref": "r"},
        "occurred_at": "2026-11-05T14:02:11Z",
        "visible_at": "2026-11-07T09:00:00Z",
        "recorded_at": "2026-11-05T14:02:12Z",
        "title": "Renew passport",
        "body": "Needs photo.",
        "tags": ["admin", "family"],
        "urgency": 2,
        "payload": {"due": "2026-11-14"},
        "refs": [{"rel": "replies-to", "id": "01JC8Z5A1B2C3D4E5F6G7H8J9K"}],
        "links": [
            {"rel": "alternate", "href": "https://example.com/other"},
            {"rel": "related", "href": "https://example.com/form"},
        ],
        "audience": ["self"],
        "seq": 4,
    }
    ev.update(over)
    return ev


FEED = "http://localhost/feed.json?tag=admin"


def jf(events, **kw):
    kw.setdefault("next_url", None)
    kw.setdefault("cursor", events[0]["seq"] if events else 0)
    return render_json(events, FEED, **kw)


def at(events, **kw):
    kw.setdefault("next_url", None)
    return ET.fromstring(render_atom(events, FEED.replace(".json", ".atom"), **kw))


# --- JSON Feed ---------------------------------------------------------------


def test_jsonfeed_top_level_is_version_1_1():
    feed = jf([event()])
    assert feed["version"] == "https://jsonfeed.org/version/1.1"
    assert feed["title"]
    assert feed["feed_url"] == FEED
    assert isinstance(feed["items"], list)


def test_jsonfeed_item_mapping():
    [item] = jf([event()])["items"]
    assert item["id"] == "urn:dayfile:01JC8Z4Q7M2X9K3T5W6N8R0VBA"
    assert item["title"] == "Renew passport"
    assert item["content_text"] == "Needs photo."
    assert item["date_published"] == "2026-11-07T09:00:00Z"
    assert item["date_modified"] == "2026-11-05T14:02:12Z"
    assert item["tags"] == ["admin", "family"]
    assert item["url"] == "https://example.com/form"


def test_jsonfeed_dayfile_extension():
    [item] = jf([event()])["items"]
    assert item["_dayfile"] == {
        "seq": 4,
        "urgency": 2,
        "payload": {"due": "2026-11-14"},
        "refs": [{"rel": "replies-to", "id": "01JC8Z5A1B2C3D4E5F6G7H8J9K"}],
    }


def test_jsonfeed_works_from_title_alone():
    bare = {k: v for k, v in event().items() if k in {"id", "title", "visible_at", "seq"}}
    bare["recorded_at"] = "2026-11-05T14:02:12Z"
    [item] = jf([bare])["items"]
    assert item["title"] == "Retracted note" or item["title"] == bare["title"]
    assert item["content_text"] == ""  # JSON Feed requires a content field
    assert "url" not in item and "tags" not in item


def test_jsonfeed_unknown_type_ignores_payload_for_display():
    [item] = jf([event(type="urn:acme:sensor.anomaly", payload={"x": 1})])["items"]
    assert item["title"] and item["content_text"]
    assert "x" not in item["content_text"]


def test_jsonfeed_next_url_and_cursor():
    feed = jf([event()], next_url="http://localhost/feed.json?since_seq=4", cursor=4)
    assert feed["next_url"] == "http://localhost/feed.json?since_seq=4"
    assert feed["_dayfile"]["cursor"] == 4
    assert "next_url" not in jf([event()])


def test_jsonfeed_empty():
    feed = jf([])
    assert feed["items"] == []
    json.dumps(feed)


# --- Atom --------------------------------------------------------------------


def test_atom_required_feed_elements():
    root = at([event()])
    assert root.tag == f"{ATOM}feed"
    for name in ("id", "title", "updated"):
        assert root.find(f"{ATOM}{name}") is not None, name
    assert root.find(f"{ATOM}author/{ATOM}name") is not None
    assert RFC3339.match(root.find(f"{ATOM}updated").text)
    selfs = [lk for lk in root.findall(f"{ATOM}link") if lk.get("rel") == "self"]
    assert selfs and selfs[0].get("href").endswith("feed.atom?tag=admin")


def test_atom_feed_updated_is_latest_recorded_at_and_stable():
    a = event(recorded_at="2026-11-05T14:02:12Z")
    b = event(id="01JC8Z5A1B2C3D4E5F6G7H8J9K", recorded_at="2026-11-09T00:00:00Z", seq=5)
    root = at([b, a])
    assert root.find(f"{ATOM}updated").text == "2026-11-09T00:00:00Z"
    assert render_atom([b, a], "http://x/feed.atom", None) == render_atom(
        [b, a], "http://x/feed.atom", None
    )


def test_atom_empty_feed_has_epoch_updated():
    assert at([]).find(f"{ATOM}updated").text == "1970-01-01T00:00:00Z"


def test_atom_entry_mapping():
    [entry] = at([event()]).findall(f"{ATOM}entry")
    assert entry.find(f"{ATOM}id").text == "urn:dayfile:01JC8Z4Q7M2X9K3T5W6N8R0VBA"
    assert entry.find(f"{ATOM}title").text == "Renew passport"
    assert entry.find(f"{ATOM}published").text == "2026-11-07T09:00:00Z"
    assert entry.find(f"{ATOM}updated").text == "2026-11-05T14:02:12Z"
    content = entry.find(f"{ATOM}content")
    assert content.get("type") == "text" and content.text == "Needs photo."
    assert [c.get("term") for c in entry.findall(f"{ATOM}category")] == ["admin", "family"]
    related = [lk for lk in entry.findall(f"{ATOM}link") if lk.get("rel") == "related"]
    assert [lk.get("href") for lk in related] == ["https://example.com/form"]


def test_atom_dayfile_namespace_elements():
    [entry] = at([event()]).findall(f"{ATOM}entry")
    assert entry.find(f"{DF}seq").text == "4"
    assert entry.find(f"{DF}urgency").text == "2"
    assert json.loads(entry.find(f"{DF}payload").text) == {"due": "2026-11-14"}
    [ref] = entry.findall(f"{DF}ref")
    assert (ref.get("rel"), ref.get("id")) == ("replies-to", "01JC8Z5A1B2C3D4E5F6G7H8J9K")


def test_atom_next_link():
    root = at([event()], next_url="http://localhost/feed.atom?since_seq=4")
    nxt = [lk for lk in root.findall(f"{ATOM}link") if lk.get("rel") == "next"]
    assert nxt[0].get("href") == "http://localhost/feed.atom?since_seq=4"


def test_atom_escapes_markup_and_strips_illegal_control_chars():
    ev = event(title="a <b> & c\x0b", body="x\x00y ]]> <script>")
    xml = render_atom([ev], "http://x/feed.atom", None)
    entry = ET.fromstring(xml).find(f"{ATOM}entry")
    assert entry.find(f"{ATOM}title").text == "a <b> & c"
    assert entry.find(f"{ATOM}content").text == "xy ]]> <script>"


def test_atom_entry_without_body_has_empty_content_element():
    ev = event()
    del ev["body"]
    [entry] = at([ev]).findall(f"{ATOM}entry")
    content = entry.find(f"{ATOM}content")  # RFC 4287: content or an alternate link is required
    assert content is not None and not content.text


def test_status_is_exposed_in_both_formats_when_present():
    [item] = jf([event(status="done")])["items"]
    assert item["_dayfile"]["status"] == "done"
    [entry] = at([event(status="done")]).findall(f"{ATOM}entry")
    assert entry.find(f"{DF}status").text == "done"
