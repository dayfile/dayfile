"""JSON Feed 1.1 renderer."""

from typing import Any
from urllib.parse import urlsplit

VERSION = "https://jsonfeed.org/version/1.1"
EXTENSION_FIELDS = ("seq", "status", "urgency", "payload", "refs", "sig", "sig_status")


def render_json(
    events: list[dict[str, Any]],
    feed_url: str,
    next_url: str | None = None,
    cursor: int = 0,
    authorization_uri: str | None = None,
    released_cursor: str | None = None,
    title: str = "dayfile",
) -> dict[str, Any]:
    """Render events (already in display order) as a JSON Feed document."""
    feed: dict[str, Any] = {"version": VERSION, "title": title, "feed_url": feed_url}
    feed["icon"] = icon_url(feed_url, "icon-512.png")
    feed["favicon"] = icon_url(feed_url, "icon-64.png")
    if next_url:
        feed["next_url"] = next_url
    feed["_dayfile"] = {"cursor": cursor}
    if released_cursor:
        feed["_dayfile"]["released_cursor"] = released_cursor
    if authorization_uri:
        feed["_dayfile"]["authorization_uri"] = authorization_uri
    feed["items"] = [_item(ev) for ev in events]
    return feed


def icon_url(feed_url: str, name: str) -> str:
    """Absolute URL of a public icon on the feed's own host; never carries the token path."""
    parts = urlsplit(feed_url)
    return f"{parts.scheme}://{parts.netloc}/{name}"


def _item(ev: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": f"urn:dayfile:{ev['id']}",
        "title": ev["title"],
        "content_text": ev.get("body", ""),
        "date_published": ev["visible_at"],
        "date_modified": ev["recorded_at"],
    }
    if ev.get("tags"):
        item["tags"] = ev["tags"]
    if href := related_href(ev):
        item["url"] = href
    item["_dayfile"] = {k: ev[k] for k in EXTENSION_FIELDS if k in ev}
    return item


def related_href(ev: dict[str, Any]) -> str | None:
    """Return the href of the first `related` link, if any."""
    return next((lk["href"] for lk in ev.get("links", []) if lk["rel"] == "related"), None)
