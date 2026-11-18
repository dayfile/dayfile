"""Atom 1.0 renderer with a `urn:dayfile:1` extension namespace."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dayfile.render.jsonfeed import EXTENSION_FIELDS, icon_url, related_href

ATOM_NS, DF_NS = "http://www.w3.org/2005/Atom", "urn:dayfile:1"
EPOCH = "1970-01-01T00:00:00Z"
ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
PAGING = {"since_seq", "limit", "released_after"}

ET.register_namespace("", ATOM_NS)
ET.register_namespace("d", DF_NS)


def render_atom(
    events: list[dict[str, Any]],
    feed_url: str,
    next_url: str | None,
    authorization_uri: str | None = None,
    cursor: int | None = None,
    released_cursor: str | None = None,
    title: str = "dayfile",
) -> bytes:
    """Render events (already in display order) as an Atom feed document."""
    feed = ET.Element(_a("feed"))
    _text(feed, "id", _feed_id(feed_url))
    _text(feed, "title", title)
    _text(feed, "updated", max((e["recorded_at"] for e in events), default=EPOCH))
    ET.SubElement(ET.SubElement(feed, _a("author")), _a("name")).text = "dayfile"
    _text(feed, "icon", icon_url(feed_url, "icon-512.png"))
    ET.SubElement(feed, _a("link"), rel="self", href=feed_url)
    if next_url:
        ET.SubElement(feed, _a("link"), rel="next", href=next_url)
    if authorization_uri:
        ET.SubElement(feed, _a("link"), rel="authorization", href=authorization_uri)
    if cursor is not None:
        ET.SubElement(feed, _d("cursor")).text = str(cursor)
    if released_cursor:
        ET.SubElement(feed, _d("released_cursor")).text = released_cursor
    for ev in events:
        _entry(feed, ev)
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


def _entry(feed: ET.Element, ev: dict[str, Any]) -> None:
    entry = ET.SubElement(feed, _a("entry"))
    _text(entry, "id", f"urn:dayfile:{ev['id']}")
    _text(entry, "title", ev["title"])
    _text(entry, "published", ev["visible_at"])
    _text(entry, "updated", ev["recorded_at"])
    _text(entry, "content", ev.get("body", ""), type="text")
    for tag in ev.get("tags", []):
        ET.SubElement(entry, _a("category"), term=_clean(tag))
    if href := related_href(ev):
        ET.SubElement(entry, _a("link"), rel="related", href=href)
    for name in EXTENSION_FIELDS:
        if name not in ev:
            continue
        if name == "refs":
            for ref in ev["refs"]:
                ET.SubElement(entry, _d("ref"), rel=ref["rel"], id=ref["id"])
        elif name in ("payload", "sig"):
            ET.SubElement(entry, _d(name)).text = _clean(json.dumps(ev[name], sort_keys=True))
        else:
            ET.SubElement(entry, _d(name)).text = str(ev[name])


def _feed_id(feed_url: str) -> str:
    """Stable id from the feed's filters, ignoring paging parameters."""
    parts = urlsplit(feed_url)
    query = urlencode(sorted(p for p in parse_qsl(parts.query) if p[0] not in PAGING))
    path = re.sub(r"^/t/[^/]+", "", parts.path)  # the credential is not part of feed identity
    canonical = urlunsplit(("", "", path, query, ""))
    return "urn:dayfile:feed:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _text(parent: ET.Element, name: str, text: str, **attrs: str) -> None:
    ET.SubElement(parent, _a(name), **attrs).text = _clean(text)


def _clean(text: str) -> str:
    return ILLEGAL_XML.sub("", text)


def _a(name: str) -> str:
    return f"{{{ATOM_NS}}}{name}"


def _d(name: str) -> str:
    return f"{{{DF_NS}}}{name}"
