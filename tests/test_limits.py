"""Read rate limit and the cap on held connections (streams and long-polls)."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import create_token
from dayfile.db import connect
from dayfile.ratelimit import Held, Window
from dayfile.tenancy import DirectoryTenancy

NOW = "2026-11-06T12:00:00Z"


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "d.db"
    connect(path).close()
    return path


def token(db_path, labels=("self",)):
    conn = connect(db_path)
    try:
        return create_token(conn, list(labels)).value
    finally:
        conn.close()


def client(app, tok=None):
    return TestClient(app, headers={"Authorization": f"Bearer {tok}"} if tok else {})


# --- the two building blocks ---------------------------------------------------------


def test_window_allows_up_to_the_limit_then_names_the_wait():
    mono = Mono()
    w = Window(mono)
    assert [w.charge("k", 1, 3, 60) for _ in range(3)] == [0, 0, 0]
    wait = w.charge("k", 1, 3, 60)
    assert 1 <= wait <= 61
    mono.t += 61
    assert w.charge("k", 1, 3, 60) == 0  # a fresh window


def test_window_keys_are_independent_and_a_big_charge_is_all_or_nothing():
    w = Window(Mono())
    assert w.charge("a", 2, 3, 60) == 0
    assert w.charge("a", 2, 3, 60) > 0  # would exceed, so nothing is spent
    assert w.charge("a", 1, 3, 60) == 0  # the failed charge cost nothing
    assert w.charge("b", 3, 3, 60) == 0


def test_window_forgets_expired_keys():
    mono = Mono()
    w = Window(mono, prune_at=10)
    for n in range(50):
        w.charge(f"k{n}", 1, 5, 60)
    mono.t += 120
    w.charge("fresh", 1, 5, 60)
    assert len(w) <= 11


def test_held_caps_concurrent_leases_per_key():
    h = Held(Mono())
    a = h.acquire("k", 2, ttl=30)
    b = h.acquire("k", 2, ttl=30)
    assert a and b and h.acquire("k", 2, ttl=30) is None
    assert h.acquire("other", 2, ttl=30)  # another key is unaffected
    h.release(a)
    assert h.acquire("k", 2, ttl=30)


def test_a_lost_lease_expires_instead_of_leaking_forever():
    mono = Mono()
    h = Held(mono)
    assert h.acquire("k", 1, ttl=30)  # never released: the client vanished
    assert h.acquire("k", 1, ttl=30) is None
    mono.t += 31
    assert h.acquire("k", 1, ttl=30)


def test_releasing_twice_is_harmless():
    h = Held(Mono())
    lease = h.acquire("k", 1, ttl=30)
    h.release(lease)
    h.release(lease)
    assert h.acquire("k", 1, ttl=30)


# --- read rate limit -----------------------------------------------------------------


def test_reads_are_limited_per_token_with_retry_after(db_path):
    app = create_app(db_path, clock=lambda: NOW, read_limit=(3, 60))
    c = client(app, token(db_path))
    assert [c.get("/events").status_code for _ in range(3)] == [200, 200, 200]
    r = c.get("/events")
    assert r.status_code == 429 and r.json() == {"error": "read rate limit exceeded"}
    assert int(r.headers["retry-after"]) >= 1 and r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path", ["/events", "/events/abc", "/feed.json", "/feed.atom", "/feed.cloudevents"]
)
def test_every_read_route_is_limited(db_path, path):
    app = create_app(db_path, clock=lambda: NOW, read_limit=(1, 60))
    c = client(app, token(db_path))
    c.get(path)
    assert c.get(path).status_code == 429


def test_path_token_feeds_are_limited_too(db_path):
    app = create_app(db_path, clock=lambda: NOW, read_limit=(1, 60))
    tok = token(db_path)
    c = TestClient(app)
    assert c.get(f"/t/{tok}/feed.json").status_code == 200
    assert c.get(f"/t/{tok}/feed.json").status_code == 429


def test_conditional_requests_count_because_they_still_cost_a_query(db_path):
    app = create_app(db_path, clock=lambda: NOW, read_limit=(2, 60))
    c = client(app, token(db_path))
    etag = c.get("/events").headers["etag"]
    assert c.get("/events", headers={"If-None-Match": etag}).status_code == 304
    assert c.get("/events", headers={"If-None-Match": etag}).status_code == 429


def test_tokens_do_not_share_a_read_budget(db_path):
    app = create_app(db_path, clock=lambda: NOW, read_limit=(1, 60))
    a, b = client(app, token(db_path)), client(app, token(db_path))
    a.get("/events")
    assert a.get("/events").status_code == 429
    assert b.get("/events").status_code == 200


def test_the_budget_refills_when_the_window_ends(db_path):
    mono = Mono()
    app = create_app(db_path, clock=lambda: NOW, read_limit=(1, 60), monotonic=mono)
    c = client(app, token(db_path))
    c.get("/events")
    assert c.get("/events").status_code == 429
    mono.t += 61
    assert c.get("/events").status_code == 200


def test_failed_credentials_do_not_spend_anyones_budget(db_path):
    app = create_app(db_path, clock=lambda: NOW, read_limit=(1, 60))
    good = token(db_path)
    for _ in range(5):
        assert client(app, "x.y").get("/events").status_code == 401
    assert client(app, good).get("/events").status_code == 200


def test_a_stream_polling_internally_is_not_charged_per_poll(db_path):
    app = create_app(
        db_path, clock=lambda: NOW, read_limit=(2, 60), poll_interval=0.01, stream_seconds=0.3
    )
    c = client(app, token(db_path))
    assert c.get("/events/stream?since_seq=0").status_code == 200  # ~30 internal polls
    assert c.get("/events").status_code == 200  # one budget unit left


def test_read_budgets_are_per_tenant(tmp_path):
    tenancy = DirectoryTenancy(tmp_path / "t", "example.test")
    app = create_app(tenancy=tenancy, clock=lambda: NOW, read_limit=(1, 60))
    toks = {}
    for slug in ("acme", "globex"):
        t = tenancy.create(slug)
        toks[slug] = TestClient(
            app,
            base_url=f"http://{slug}.example.test",
            headers={"Authorization": f"Bearer {token(t.db_path)}"},
        )
    toks["acme"].get("/events")
    assert toks["acme"].get("/events").status_code == 429
    assert toks["globex"].get("/events").status_code == 200


def test_default_read_limit_is_generous_for_feed_readers(db_path):
    c = client(create_app(db_path, clock=lambda: NOW), token(db_path))
    assert all(c.get("/feed.json").status_code == 200 for _ in range(60))


# --- held connections ----------------------------------------------------------------


def hold_stream(app, tok, out, seconds_hint=0.0):
    r = client(app, tok).get("/events/stream?since_seq=0")
    out.append(r.status_code)


def test_streams_beyond_the_cap_are_refused_until_one_closes(db_path):
    app = create_app(
        db_path, clock=lambda: NOW, held_limit=2, poll_interval=0.02, stream_seconds=0.6
    )
    tok = token(db_path)
    done: list[int] = []
    threads = [threading.Thread(target=hold_stream, args=(app, tok, done)) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    r = client(app, tok).get("/events/stream?since_seq=0")
    assert r.status_code == 429 and r.json() == {"error": "too many open streams"}
    assert int(r.headers["retry-after"]) >= 1
    for t in threads:
        t.join()
    assert done == [200, 200]
    assert client(app, tok).get("/events/stream?since_seq=0").status_code == 200  # slots freed


def test_the_cap_is_per_token(db_path):
    app = create_app(
        db_path, clock=lambda: NOW, held_limit=1, poll_interval=0.02, stream_seconds=0.6
    )
    a, b = token(db_path), token(db_path)
    done: list[int] = []
    t = threading.Thread(target=hold_stream, args=(app, a, done))
    t.start()
    time.sleep(0.2)
    assert client(app, a).get("/events/stream?since_seq=0").status_code == 429
    assert client(app, b).get("/events/stream?since_seq=0").status_code == 200
    t.join()


def test_long_polls_count_but_plain_reads_do_not(db_path):
    app = create_app(db_path, clock=lambda: NOW, held_limit=1, poll_interval=0.02)
    tok = token(db_path)
    out: list[int] = []

    def poll():
        out.append(client(app, tok).get("/events?since_seq=0&wait=1").status_code)

    t = threading.Thread(target=poll)
    t.start()
    time.sleep(0.25)
    c = client(app, tok)
    assert c.get("/events?since_seq=0&wait=1").status_code == 429
    assert c.get("/events?since_seq=0").status_code == 200  # no wait, nothing held
    t.join()
    assert out == [200]
    assert c.get("/events?since_seq=0&wait=1").status_code == 200  # slot released


def test_a_refused_stream_does_not_hold_a_slot(db_path):
    app = create_app(
        db_path, clock=lambda: NOW, held_limit=1, poll_interval=0.02, stream_seconds=0.5
    )
    tok = token(db_path)
    done: list[int] = []
    t = threading.Thread(target=hold_stream, args=(app, tok, done))
    t.start()
    time.sleep(0.2)
    for _ in range(5):
        assert client(app, tok).get("/events/stream?since_seq=0").status_code == 429
    t.join()
    assert client(app, tok).get("/events/stream?since_seq=0").status_code == 200


def test_a_revoked_stream_ends_promptly(db_path):
    from dayfile.auth.tokens import revoke_token

    app = create_app(db_path, clock=lambda: NOW, held_limit=1, poll_interval=0.02, stream_seconds=5)
    conn = connect(db_path)
    issued = create_token(conn, ["self"])
    done: list[int] = []
    t = threading.Thread(target=hold_stream, args=(app, issued.value, done))
    t.start()
    time.sleep(0.2)
    revoke_token(conn, issued.id, NOW)
    conn.close()
    t.join(timeout=3)
    assert not t.is_alive() and done == [200]
