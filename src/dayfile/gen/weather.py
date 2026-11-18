"""Weather generator: one `weather.daily` event per day, idempotent through `source.ref`."""

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from dayfile import db
from dayfile.ingest.core import IngestResult, ingest_events
from dayfile.timefmt import plus
from dayfile.ulid import new_ulid

SOURCE_ID = "gen:weather"
Forecast = dict[str, Any]

# WMO weather interpretation codes, as returned by Open-Meteo.
WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Rain showers", 82: "Heavy rain showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
}  # fmt: skip


def build_event(day: str, forecast: Forecast, now: str, audience: list[str]) -> dict[str, Any]:
    """Build the daily weather event for `day` from a forecast."""
    high, low, pct = forecast["high_f"], forecast["low_f"], forecast["precip_pct"]
    summary = forecast["summary"]
    return {
        "specversion": "dayfile/1",
        "id": new_ulid(),
        "source": {"id": SOURCE_ID, "ref": day},
        "occurred_at": now,
        "visible_at": now,
        "expires_at": plus(now, 86400),
        "title": f"Weather: {summary.lower()}, high {high}F",
        "body": f"{summary}. High {high}F, low {low}F, {pct}% chance of rain.",
        "tags": ["weather"],
        "payload": {"summary": summary, "high_f": high, "low_f": low, "precip_pct": pct},
        "audience": audience,
    }


def generate(
    conn: Any,
    fetch: Callable[[], Forecast],
    now: str,
    audience: list[str] | None = None,
    day: str | None = None,
) -> IngestResult:
    """Write today's weather event, or return the existing one without calling `fetch`."""
    day = day or now[:10]
    if seq := db.find_by_source(conn, SOURCE_ID, day):
        return IngestResult(db.get_event(conn, seq), created=False)
    event = build_event(day, fetch(), now, audience or ["self"])
    return ingest_events(conn, [event])[0]


def parse_open_meteo(raw: dict[str, Any]) -> Forecast:
    """Convert an Open-Meteo daily response into a Forecast."""
    daily = raw["daily"]
    pct = daily["precipitation_probability_max"][0]
    return {
        "summary": WMO.get(daily["weather_code"][0], "Mixed conditions"),
        "high_f": round(daily["temperature_2m_max"][0]),
        "low_f": round(daily["temperature_2m_min"][0]),
        "precip_pct": 0 if pct is None else round(pct),
    }


def open_meteo(lat: float, lon: float, timeout: float = 10) -> Forecast:
    """Fetch today's forecast for a location from Open-Meteo (no API key)."""
    query = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "forecast_days": 1,
        }
    )
    with urllib.request.urlopen(
        f"https://api.open-meteo.com/v1/forecast?{query}", timeout=timeout
    ) as r:
        return parse_open_meteo(json.load(r))
