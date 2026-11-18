from fastapi.testclient import TestClient

from dayfile.web import create_web_app, uptimerobot

NOW = 1_794_657_600  # 2026-11-14T12:00:00Z


def answer(**over):
    monitor = {
        "friendly_name": "API",
        "status": 2,
        "custom_uptime_ratio": "100.000-99.950-99.900-99.800",
        "custom_uptime_ranges": "-".join(["100.000"] * 88 + ["97.500", "0.000"]),
        "logs": [
            {"type": 1, "datetime": NOW - 3600, "duration": 600},
            {"type": 2, "datetime": NOW - 3000, "duration": 0},
        ],
        **over,
    }
    return {"stat": "ok", "monitors": [monitor, {"friendly_name": "Paused", "status": 0}]}


def test_parse_reads_ratios_bars_and_down_logs_and_skips_paused_monitors():
    r = uptimerobot.parse(answer(), NOW)
    (m,) = r["monitors"]
    assert m["up"] and m["uptime"] == {1: 100.0, 7: 99.95, 30: 99.9, 90: 99.8}
    assert [d["state"] for d in m["days"][-3:]] == ["up", "part", "down"] and len(m["days"]) == 90
    assert r["incidents"][0]["minutes"] == 10 and r["incidents"][0]["name"] == "API"
    assert r["incidents"][0]["start"] == "2026-11-14T11:00:00Z" and r["all_up"]


def test_parse_shows_only_the_named_monitors():
    data = answer()
    data["monitors"].append({**data["monitors"][0], "friendly_name": "Keyword"})
    assert [m["name"] for m in uptimerobot.parse(data, NOW)["monitors"]] == ["API", "Keyword"]
    only = uptimerobot.parse(data, NOW, frozenset({"API"}))
    assert [m["name"] for m in only["monitors"]] == ["API"]
    assert {i["name"] for i in only["incidents"]} == {"API"}


def test_parse_marks_a_down_monitor_and_rejects_errors():
    assert not uptimerobot.parse(answer(status=9), NOW)["all_up"]
    assert uptimerobot.parse({"stat": "fail"}, NOW) is None
    assert uptimerobot.parse("nonsense", NOW) is None
    odd = uptimerobot.parse(answer(custom_uptime_ratio="x", custom_uptime_ranges=""), NOW)
    assert odd["monitors"][0]["uptime"][1] is None


def test_cache_asks_rarely_and_survives_an_outage_for_a_while():
    t, calls, replies = [0.0], [], [{"v": 1}, None, None]

    def get():
        calls.append(1)
        return replies.pop(0)

    cached = uptimerobot.Cached(get, ttl=300, stale=3600, clock=lambda: t[0])
    assert cached() == {"v": 1} and cached() == {"v": 1} and len(calls) == 1
    t[0] = 400
    assert cached() == {"v": 1} and len(calls) == 2  # it failed, the last answer is reused
    t[0] = 4000
    assert cached() is None  # too old to trust


def test_the_status_page_credits_uptimerobot_and_names_the_measurement(monkeypatch):
    monkeypatch.setenv("DAYFILE_STATUS_PAGE_URL", "https://stats.uptimerobot.com/abc")
    report = uptimerobot.parse(answer(), NOW)
    web = TestClient(create_web_app(probe=lambda: "ok", uptime=lambda: report))
    text = web.get("/status").text
    assert "UptimeRobot" in text and "https://uptimerobot.com" in text
    assert "every 5 minutes" in text and "https://stats.uptimerobot.com/abc" in text
    assert "99.95%" in text and "All systems operational" in text
    down = uptimerobot.parse(answer(status=9), NOW)
    page = TestClient(create_web_app(probe=lambda: "ok", uptime=lambda: down)).get("/status")
    assert "Something is down" in page.text
