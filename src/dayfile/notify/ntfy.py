"""ntfy publisher: turn an event into an HTTP request and send it."""

import base64
import urllib.request
from typing import Any


def priority(urgency: int) -> int:
    """Map an event urgency to an ntfy priority (2 is high, 3 is max)."""
    return {2: 4, 3: 5}.get(urgency, 3)


def encode_header(value: str) -> str:
    """Make a value safe as an HTTP header: no line breaks, RFC 2047 for non-ASCII."""
    value = " ".join(value.split())
    if value.isascii():
        return value
    return "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


def build_request(topic_url: str, event: dict[str, Any]) -> urllib.request.Request:
    """Build the ntfy POST for an event; the message is the body, falling back to the title."""
    headers = {
        "Title": encode_header(event["title"]),
        "Priority": str(priority(event.get("urgency", 0))),
    }
    if tags := event.get("tags"):
        headers["Tags"] = encode_header(",".join(tags))
    related = next((lk["href"] for lk in event.get("links", []) if lk["rel"] == "related"), None)
    if related:
        headers["Click"] = encode_header(related)
    data = (event.get("body") or event["title"]).encode()
    return urllib.request.Request(topic_url, data, headers, method="POST")


def publish(topic_url: str, event: dict[str, Any], timeout: float = 10) -> None:
    """Send an event to an ntfy topic; raises OSError on any delivery failure."""
    with urllib.request.urlopen(build_request(topic_url, event), timeout=timeout):
        pass
