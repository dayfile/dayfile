import pytest

from dayfile import cli
from dayfile.db import connect
from dayfile.gen import digest, weather
from dayfile.ingest.core import ingest_events, prepare
from helpers import authed_client

NOW = "2026-11-06T13:00:00Z"
FORECAST = {"summary": "Clear", "high_f": 57, "low_f": 43, "precip_pct": 10}

OPEN_METEO = {
    "daily": {
        "time": ["2026-11-06"],
        "weather_code": [0],
        "temperature_2m_max": [57.4],
        "temperature_2m_min": [42.6],
        "precipitation_probability_max": [10],
    }
}


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "d.db")
    yield c
    c.close()


def envelope(stored):
    """A stored event minus the server-assigned fields, i.e. what an author would send."""
    return {k: v for k, v in stored.items() if k not in ("seq", "recorded_at")}


def count(conn, where="1"):
    return conn.execute(f"SELECT count(*) FROM events WHERE {where}").fetchone()[0]


# --- weather -----------------------------------------------------------------


def test_weather_event_matches_the_documented_shape():
    ev = weather.build_event("2026-11-06", FORECAST, NOW, ["self", "family"])
    prepare(ev)
    assert ev["source"] == {"id": "gen:weather", "ref": "2026-11-06"}
    assert ev["title"] == "Weather: clear, high 57F"
    assert ev["body"] == "Clear. High 57F, low 43F, 10% chance of rain."
    assert ev["payload"] == {"summary": "Clear", "high_f": 57, "low_f": 43, "precip_pct": 10}
    assert ev["tags"] == ["weather"] and ev["audience"] == ["self", "family"]
    assert ev["visible_at"] == NOW and ev["expires_at"] == "2026-11-07T13:00:00Z"


def test_weather_is_idempotent_per_day_and_skips_the_fetch(conn):
    calls = []

    def fetch():
        calls.append(1)
        return FORECAST

    first = weather.generate(conn, fetch, NOW)
    second = weather.generate(conn, fetch, "2026-11-06T18:00:00Z")
    assert first.created and not second.created and second.event["id"] == first.event["id"]
    assert len(calls) == 1 and count(conn) == 1
    nxt = weather.generate(conn, fetch, "2026-11-07T13:00:00Z")
    assert nxt.created and count(conn) == 2


def test_weather_defaults_to_self_only(conn):
    assert weather.generate(conn, lambda: FORECAST, NOW).event["audience"] == ["self"]


def test_parse_open_meteo():
    assert weather.parse_open_meteo(OPEN_METEO) == FORECAST


def test_parse_open_meteo_handles_missing_precip_and_unknown_codes():
    raw = {
        "daily": {
            **OPEN_METEO["daily"],
            "weather_code": [999],
            "precipitation_probability_max": [None],
        }
    }
    out = weather.parse_open_meteo(raw)
    assert out["precip_pct"] == 0 and out["summary"]


@pytest.mark.parametrize(
    ("code", "text"), [(0, "Clear"), (3, "Overcast"), (63, "Rain"), (95, "Thunderstorm")]
)
def test_wmo_codes(code, text):
    raw = {"daily": {**OPEN_METEO["daily"], "weather_code": [code]}}
    assert weather.parse_open_meteo(raw)["summary"] == text


# --- digest ------------------------------------------------------------------


def ev(n, audience=None, **over):
    base = {
        "specversion": "dayfile/1",
        "id": f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}",
        "source": {"id": "t", "ref": f"r{n}"},
        "occurred_at": "2026-11-05T10:00:00Z",
        "visible_at": "2026-11-05T10:00:00Z",
        "title": f"event {n}",
    }
    if audience:
        base["audience"] = audience
    base.update(over)
    return base


DAY = "2026-11-05"
LATER = "2026-11-06T06:00:00Z"


def test_digest_is_an_ordinary_event_summarising_the_day(conn):
    ingest_events(conn, [ev(1, tags=["todo"], title="Renew passport"), ev(2, title="Idea")])
    res = digest.generate(conn, DAY, LATER)
    prepare(envelope(res.event))
    e = res.event
    assert e["source"]["id"] == "gen:digest"
    assert e["title"] == "Digest 2026-11-05: 2 events"
    assert "- Renew passport" in e["body"] and "- Idea" in e["body"]
    assert e["payload"]["day"] == DAY and e["payload"]["count"] == 2
    assert e["tags"] == ["digest"] and e["audience"] == ["self"]
    assert count(conn, "source_id = 'gen:digest'") == 1  # written back to the log


def test_digest_window_is_the_utc_day_by_visible_at(conn):
    ingest_events(
        conn,
        [
            ev(1, visible_at="2026-11-04T23:59:59Z", occurred_at="2026-11-04T23:59:59Z"),
            ev(2, visible_at="2026-11-05T00:00:00Z", occurred_at="2026-11-05T00:00:00Z"),
            ev(3, visible_at="2026-11-05T23:59:59Z"),
            ev(4, visible_at="2026-11-06T00:00:00Z", occurred_at="2026-11-06T00:00:00Z"),
        ],
    )
    ids = digest.generate(conn, DAY, "2026-11-07T00:00:00Z").event["payload"]["ids"]
    assert ids == [ev(2)["id"], ev(3)["id"]]


def test_digest_excludes_unreleased_events_and_other_digests(conn):
    ingest_events(conn, [ev(1), ev(2, visible_at="2026-11-05T22:00:00Z")])
    first = digest.generate(conn, DAY, "2026-11-05T12:00:00Z")
    assert first.event["payload"]["count"] == 1  # ev(2) is not released yet
    second = digest.generate(conn, DAY, "2026-11-05T13:00:00Z", audience=["self", "family"])
    assert second.created and second.event["payload"]["count"] == 1  # the first digest is skipped


def test_empty_day_makes_no_digest(conn):
    assert digest.generate(conn, "2026-11-20", "2026-11-21T06:00:00Z") is None
    assert count(conn) == 0


def test_digest_only_summarises_what_its_audience_may_see(conn):
    ingest_events(
        conn,
        [ev(1, ["self"], title="SECRET private"), ev(2, ["self", "family"], title="shared"),
         ev(3, ["family"], title="for family")],
    )  # fmt: skip
    res = digest.generate(conn, DAY, LATER, audience=["family"])
    assert res.event["audience"] == ["family"]
    assert "SECRET" not in res.event["body"] + res.event["title"]
    assert res.event["payload"]["ids"] == [ev(2)["id"], ev(3)["id"]]
    mine = digest.generate(conn, DAY, LATER, audience=["self"])
    assert mine.created and mine.event["payload"]["count"] == 2


def test_digest_is_idempotent_and_skips_the_summarizer(conn):
    ingest_events(conn, [ev(1)])
    calls = []

    def summarizer(day, events):
        calls.append(day)
        return "T", "B"

    a = digest.generate(conn, DAY, LATER, summarizer=summarizer)
    b = digest.generate(conn, DAY, "2026-11-06T09:00:00Z", summarizer=summarizer)
    assert a.created and not b.created and calls == [DAY]


def test_summarizer_is_pluggable_and_title_is_clamped(conn):
    ingest_events(conn, [ev(1)])
    res = digest.generate(conn, DAY, LATER, summarizer=lambda day, events: ("x" * 500, "llm text"))
    prepare(envelope(res.event))
    assert len(res.event["title"]) == 300 and res.event["body"] == "llm text"


def test_summary_reports_lifecycle_status(conn):
    ingest_events(
        conn,
        [ev(1, tags=["todo"], title="Pay bill"),
         ev(2, title="Paid", refs=[{"rel": "completes", "id": ev(1)["id"]}])],
    )  # fmt: skip
    body = digest.generate(conn, DAY, LATER).event["body"]
    assert "- Pay bill (closed)" in body


def test_load_summarizer():
    fn = digest.load_summarizer("dayfile.gen.digest:plain_summarizer")
    assert fn is digest.plain_summarizer
    for bad in ["nocolon", "dayfile.gen.digest:missing", "no.such.module:f"]:
        with pytest.raises(ValueError):
            digest.load_summarizer(bad)


def test_digest_shows_up_in_the_feed(tmp_path):
    path = tmp_path / "d.db"
    client = authed_client(path, labels=("self",), clock=lambda: "2026-11-06T12:00:00Z")
    c = connect(path)
    ingest_events(c, [ev(1)])
    digest.generate(c, DAY, LATER)
    c.close()
    items = client.get("/feed.json?tag=digest").json()["items"]
    assert [i["title"] for i in items] == ["Digest 2026-11-05: 1 event"]


# --- CLI ---------------------------------------------------------------------


def test_cli_gen_digest(capsys, tmp_path):
    path = tmp_path / "d.db"
    c = connect(path)
    ingest_events(c, [ev(1)])
    c.close()
    code = cli.main(["gen", "digest", "--day", DAY, "--db", str(path)])
    assert code == 0 and "Digest 2026-11-05" in capsys.readouterr().out
    assert cli.main(["gen", "digest", "--day", DAY, "--db", str(path)]) == 0
    assert "already" in capsys.readouterr().out


def test_cli_gen_digest_rejects_a_bad_day(capsys, tmp_path):
    assert cli.main(["gen", "digest", "--day", "yesterday", "--db", str(tmp_path / "d.db")]) == 1


def test_cli_gen_weather(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(weather, "open_meteo", lambda lat, lon: FORECAST)
    path = tmp_path / "d.db"
    code = cli.main(["gen", "weather", "--lat", "1.5", "--lon", "2.5", "--db", str(path)])
    assert code == 0 and "Weather: clear, high 57F" in capsys.readouterr().out
    c = connect(path)
    assert count(c, "source_id = 'gen:weather'") == 1
    c.close()


def test_cli_gen_weather_reports_network_failure(capsys, tmp_path, monkeypatch):
    def boom(lat, lon):
        raise OSError("offline")

    monkeypatch.setattr(weather, "open_meteo", boom)
    code = cli.main(["gen", "weather", "--lat", "1", "--lon", "2", "--db", str(tmp_path / "d.db")])
    assert code == 1 and "offline" in capsys.readouterr().err
