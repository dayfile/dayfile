import time

from fastapi.testclient import TestClient

from dayfile.timefmt import minus
from dayfile.web import create_web_app
from dayfile.web.history import StatusHistory

NOW = "2026-11-14T12:00:00Z"


def test_uptime_bars_and_incidents(tmp_path):
    h = StatusHistory(tmp_path / "s.db")
    for i in range(10):  # ten minutes ago to now; the middle three fail
        h.record(minus(NOW, (9 - i) * 60), ok=not 3 <= i <= 5)
    s = h.summary(NOW)
    assert s["uptime"][1] == 70.0 and s["uptime"][90] == 70.0
    assert s["incidents"] == [{"start": minus(NOW, 6 * 60), "minutes": 3}]
    assert len(s["days"]) == 90 and s["days"][-1]["state"] == "down"
    assert s["days"][0]["state"] == "none" and s["days"][0]["pct"] is None


def test_separate_failures_are_separate_incidents_and_old_rows_are_dropped(tmp_path):
    h = StatusHistory(tmp_path / "s.db")
    h.record(minus(NOW, 100 * 86400), ok=False)
    h.record(minus(NOW, 7200), ok=False)
    h.record(minus(NOW, 60), ok=False)
    h.record(NOW, ok=True)
    s = h.summary(NOW)
    assert [i["minutes"] for i in s["incidents"]] == [1, 1]
    assert s["uptime"][90] == 33.33  # the 100-day-old failure is gone


def test_empty_history_has_no_percentages(tmp_path):
    s = StatusHistory(tmp_path / "s.db").summary(NOW)
    assert set(s["uptime"].values()) == {None} and s["incidents"] == []


def test_the_page_shows_history_and_the_app_records_probes(tmp_path):
    db = tmp_path / "s.db"
    app = create_web_app(probe=lambda: "ok", status_db=db, probe_every=0.05)
    with TestClient(app) as web:
        time.sleep(0.4)
        text = web.get("/status").text
    assert "Uptime" in text and "Past incidents" in text and "No incidents" in text
    assert "100.0%" in text
