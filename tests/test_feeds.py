"""Named feeds and public visibility: saved filters, `/f/<name>`, anonymous public reads."""

import json
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from dayfile import feeds
from dayfile.app import create_app
from dayfile.auth.tokens import create_token
from dayfile.db import connect
from dayfile.ingest.core import ingest_events

NOW = "2026-11-06T12:00:00Z"
ATOM = "{http://www.w3.org/2005/Atom}"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "d.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def client(db_path, conn):
    return TestClient(create_app(db_path, clock=lambda: NOW))


def post(conn, n, audience=None, tags=(), **over):
    event = {
        "specversion": "dayfile/1",
        "id": f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}",
        "source": {"id": "s", "ref": f"r{n}"},
        "occurred_at": "2026-11-06T10:00:00Z",
        "title": f"event {n}",
        "tags": list(tags),
        **over,
    }
    if audience is not None:
        event["audience"] = audience
    ingest_events(conn, [event])


def bearer(conn, labels=("self",)):
    return {"Authorization": f"Bearer {create_token(conn, list(labels)).value}"}


def titles(response):
    return sorted(i["title"] for i in response.json()["items"])


@pytest.fixture
def seeded(conn):
    post(conn, 1, ["public"], tags=["blog"])
    post(conn, 2, ["self"], tags=["blog"])  # private, matches the filter
    post(conn, 3, ["public"], tags=["work"])  # public, does not match
    post(conn, 4, ["self", "public"], tags=["blog"])
    feeds.save_feed(conn, "blog", NOW, title="My blog", filter={"tag": "blog"}, visibility="public")
    feeds.save_feed(conn, "notes", NOW, filter={"tag": "blog"})


# --- the store ---


def test_a_feed_round_trips_with_its_filter_and_defaults_to_private(conn):
    feeds.save_feed(
        conn, "work", NOW, filter={"tag": ["work", "ops"], "status": "open", "within": "7d"}
    )
    got = feeds.get_feed(conn, "work")
    assert got["visibility"] == "private" and got["title"] is None
    assert sorted(map(tuple, got["filter"])) == [
        ("status", "open"),
        ("tag", "ops"),
        ("tag", "work"),
        ("within", "7d"),
    ]
    assert [f["name"] for f in feeds.list_feeds(conn)] == ["work"]


def test_saving_again_changes_only_what_is_given(conn):
    feeds.save_feed(conn, "work", NOW, title="Work", filter={"tag": "work"})
    feeds.save_feed(conn, "work", NOW, visibility="public")
    got = feeds.get_feed(conn, "work")
    assert (
        got["title"] == "Work"
        and got["visibility"] == "public"
        and got["filter"] == [["tag", "work"]]
    )


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("Bad_Name", {}),
        ("", {}),
        ("x" * 40, {}),
        ("ok", {"filter": {"since_seq": "1"}}),  # paging is not a filter
        ("ok", {"filter": {"color": "red"}}),
        ("ok", {"filter": {"status": "bogus"}}),
        ("ok", {"filter": {"tag": "Bad Tag"}}),
        ("ok", {"visibility": "shared"}),
        ("ok", {"title": "t" * 81}),
        ("default", {"filter": {"tag": "x"}}),  # the default feed has no filter
    ],
)
def test_bad_feeds_are_refused(conn, name, kwargs):
    with pytest.raises(feeds.FeedError):
        feeds.save_feed(conn, name, NOW, **kwargs)
    assert feeds.list_feeds(conn) == []


def test_a_log_may_hold_fifty_feeds_at_most(conn):
    for i in range(50):
        feeds.save_feed(conn, f"f{i}", NOW)
    with pytest.raises(feeds.FeedError):
        feeds.save_feed(conn, "one-too-many", NOW)


def test_deleting_a_feed_is_repeatable_and_the_default_cannot_be_deleted(conn):
    feeds.save_feed(conn, "work", NOW)
    assert feeds.delete_feed(conn, "work") is True and feeds.delete_feed(conn, "work") is False
    with pytest.raises(feeds.FeedError):
        feeds.delete_feed(conn, "default")


# --- named feeds with a token ---


def test_a_named_feed_serves_only_what_its_filter_matches(client, conn, seeded):
    r = client.get("/f/notes.json", headers=bearer(conn))
    # tagged blog, and visible to a self token: its own events plus the public one
    assert r.status_code == 200 and titles(r) == ["event 1", "event 2", "event 4"]


def test_the_filter_cannot_be_widened_or_replaced_from_the_url(client, conn, seeded):
    h = bearer(conn)
    for query in ("tag=work", "status=all", "type=note", "within=1d", "tag_any=x"):
        assert client.get(f"/f/notes.json?{query}", headers=h).status_code == 400, query
    assert client.get("/f/notes.json?limit=1", headers=h).status_code == 200


def test_paging_works_on_named_feeds(client, conn, seeded):
    h = bearer(conn)
    first = client.get("/f/notes.json?limit=1", headers=h).json()
    assert len(first["items"]) == 1 and "next_url" not in first  # newest page has no next_url
    nxt = client.get("/f/notes.json?since_seq=0&limit=1", headers=h).json()
    assert nxt["next_url"] and "/f/notes.json" in nxt["next_url"]


def test_every_format_and_the_bare_form_work(client, conn, seeded):
    h = bearer(conn)
    assert (
        client.get("/f/notes.json", headers=h)
        .headers["content-type"]
        .startswith("application/feed+json")
    )
    atom = client.get("/f/notes.atom", headers=h)
    assert atom.headers["content-type"].startswith("application/atom+xml")
    assert len(ET.fromstring(atom.content).findall(ATOM + "entry")) == 3
    assert len(client.get("/f/notes.cloudevents", headers=h).json()) == 3
    bare = client.get("/f/notes", headers=h)
    full = client.get("/f/notes.json", headers=h).json()
    assert bare.status_code == 200 and bare.json()["items"] == full["items"]
    assert (
        client.get("/feed", headers=h).json()["items"]
        == client.get("/feed.json", headers=h).json()["items"]
    )


def test_a_token_in_the_path_works_for_named_feeds(client, conn, seeded):
    token = create_token(conn, ["self"]).value
    assert titles(client.get(f"/t/{token}/f/notes.json")) == ["event 1", "event 2", "event 4"]
    assert client.get(f"/t/{token}/f/notes.atom").status_code == 200
    assert client.get(f"/t/{token}/f/notes").status_code == 200
    assert client.get(f"/t/{token}/feed").status_code == 200


def test_the_feed_title_is_used(client, conn, seeded):
    h = bearer(conn)
    assert client.get("/f/blog.json", headers=h).json()["title"] == "My blog"
    atom = ET.fromstring(client.get("/f/blog.atom", headers=h).content)
    assert atom.find(ATOM + "title").text == "My blog"
    assert client.get("/f/notes.json", headers=h).json()["title"] == "dayfile"


def test_unknown_feeds_are_404_with_a_token_and_401_without(client, conn, seeded):
    assert client.get("/f/nope.json", headers=bearer(conn)).status_code == 404
    assert client.get("/f/nope.json").status_code == 401
    assert client.get("/f/Bad_Name.json", headers=bearer(conn)).status_code == 404


# --- private versus public ---


def test_private_feeds_and_missing_feeds_look_the_same_to_strangers(client, seeded):
    private, missing = client.get("/f/notes.json"), client.get("/f/nope.json")
    assert private.status_code == missing.status_code == 401
    assert private.json() == missing.json()
    assert "www-authenticate" in private.headers


def test_a_public_feed_needs_no_credential_and_shows_only_public_events(client, seeded):
    r = client.get("/f/blog.json")
    assert r.status_code == 200
    assert titles(r) == ["event 1", "event 4"]  # tag blog AND labeled public; never event 2 or 3


@pytest.mark.parametrize("path", ["/f/blog.json", "/f/blog.atom", "/f/blog.cloudevents", "/f/blog"])
def test_public_feeds_never_leak_private_events_in_any_format(client, seeded, path):
    body = client.get(path).text
    assert "event 2" not in body and "event 3" not in body


def test_public_responses_are_publicly_cacheable_and_keyed_by_authorization(client, seeded):
    r = client.get("/f/blog.json")
    assert r.headers["cache-control"] == "public, max-age=60"
    assert "authorization" in r.headers["vary"].lower()
    again = client.get("/f/blog.json", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304


def test_a_credentialed_response_stays_private(client, conn, seeded):
    r = client.get("/f/blog.json", headers=bearer(conn))
    assert r.headers["cache-control"].startswith("private")
    assert titles(r) == ["event 1", "event 2", "event 4"]  # own events plus public ones


def test_anonymous_and_credentialed_etags_differ(client, conn, seeded):
    assert (
        client.get("/f/blog.json").headers["etag"]
        != client.get("/f/blog.json", headers=bearer(conn)).headers["etag"]
    )


def test_a_bad_token_on_a_public_feed_is_refused_not_quietly_downgraded(client, seeded):
    r = client.get("/f/blog.json", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_making_a_feed_private_stops_serving_it_at_once(client, conn, seeded):
    assert client.get("/f/blog.json").status_code == 200
    feeds.save_feed(conn, "blog", NOW, visibility="private")
    assert client.get("/f/blog.json").status_code == 401


def test_public_feeds_take_no_filter_parameters_from_strangers(client, seeded):
    assert client.get("/f/blog.json?tag=work").status_code == 400
    assert client.get("/f/blog.json?audience=self").status_code == 400


def test_the_default_feed_is_private_until_it_is_published(client, conn, seeded):
    assert client.get("/feed.json").status_code == 401
    feeds.save_feed(conn, "default", NOW, visibility="public")
    assert titles(client.get("/feed.json")) == ["event 1", "event 3", "event 4"]
    assert client.get("/feed").status_code == 200


def test_public_events_are_visible_to_every_token_holder(client, conn, seeded):
    only_family = bearer(conn, ("family",))
    assert titles(client.get("/feed.json", headers=only_family)) == [
        "event 1",
        "event 3",
        "event 4",
    ]


def test_a_public_label_does_not_let_a_write_token_read_or_widen_anything(client, conn, seeded):
    writer = {"Authorization": f"Bearer {create_token(conn, ['self'], write_sources=['s']).value}"}
    assert client.get("/f/blog.json", headers=writer).status_code == 403


def test_public_feeds_are_read_only(client, seeded):
    event = {"title": "x"}
    assert client.post("/events", json=event).status_code == 401
    assert client.post("/f/blog.json", json=event).status_code == 405


def test_anonymous_reads_have_their_own_budget(db_path, conn, seeded):
    app = create_app(db_path, clock=lambda: NOW, anon_read_limit=(2, 60))
    c = TestClient(app)
    codes = [c.get("/f/blog.json").status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]
    assert c.get("/f/blog.json", headers=bearer(conn)).status_code == 200  # tokens are unaffected


def test_suspended_tenants_serve_nothing_publicly(tmp_path):
    from dayfile.tenancy import DirectoryTenancy

    tenancy = DirectoryTenancy(tmp_path / "t", "example.test")
    tenant = tenancy.create("acme")
    c = connect(tenant.db_path)
    feeds.save_feed(c, "blog", NOW, visibility="public")
    c.close()
    app = TestClient(create_app(tenancy=tenancy), base_url="http://acme.example.test")
    assert app.get("/f/blog.json").status_code == 200
    tenancy.update("acme", suspended=True)
    assert app.get("/f/blog.json").status_code == 403


def test_tenants_cannot_read_each_others_public_feeds(tmp_path):
    from dayfile.tenancy import DirectoryTenancy

    tenancy = DirectoryTenancy(tmp_path / "t", "example.test")
    for slug in ("acme", "globex"):
        c = connect(tenancy.create(slug).db_path)
        post(c, 1, ["public"], title=f"{slug} secret", tags=["blog"])
        if slug == "acme":
            feeds.save_feed(c, "blog", NOW, filter={"tag": "blog"}, visibility="public")
        c.close()
    app = create_app(tenancy=tenancy)
    acme = TestClient(app, base_url="http://acme.example.test")
    globex = TestClient(app, base_url="http://globex.example.test")
    assert json.dumps(acme.get("/f/blog.json").json()).count("acme secret") == 1
    assert globex.get("/f/blog.json").status_code == 401  # globex has no such feed


def test_public_events_also_reach_token_holders_through_the_json_api(client, conn, seeded):
    family = bearer(conn, ("family",))
    got = client.get("/events", headers=family).json()["events"]
    assert sorted(e["title"] for e in got) == ["event 1", "event 3", "event 4"]
    one = client.get("/events/01JC8Z4Q7M2X9K3T5W6N8R0003", headers=family)
    assert one.status_code == 200
    assert client.get("/events/01JC8Z4Q7M2X9K3T5W6N8R0002", headers=family).status_code == 404
