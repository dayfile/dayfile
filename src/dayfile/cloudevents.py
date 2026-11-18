"""CloudEvents 1.0 (JSON format) mapping: export dayfile events, import foreign ones.

A dayfile event maps to the core attributes plus lowercase `dayfile*` extension attributes.
Extension values cannot be arrays or objects, so lists become delimited strings and nested
values become JSON strings. Events exported by dayfile round-trip without loss; foreign events
get a fresh ULID with the CloudEvents (source, id) pair as the idempotency key.
"""

import json
import re
from typing import Any
from urllib.parse import quote, unquote

from dayfile.timefmt import normalize
from dayfile.ulid import new_ulid

SPECVERSION = "1.0"
EXPORT_TYPE = "app.dayfile.event"  # CloudEvents requires a type; every export carries this one
SOURCE_SAFE = ":/@.-_~"
JSON_ATTRS = {"dayfilerefs": "refs", "dayfilelinks": "links", "dayfilesig": "sig"}
LIST_ATTRS = {"dayfiletags": "tags", "dayfileaudience": "audience"}
TEXT_ATTRS = {
    "dayfiletitle": "title",
    "dayfilebody": "body",
    "dayfilevisibleat": "visible_at",
    "dayfileexpiresat": "expires_at",
}


def _type_tag(type_: str) -> str | None:
    """A foreign event's `type` as a tag (`com.acme.sensor` becomes `com-acme-sensor`)."""
    if type_ == EXPORT_TYPE:
        return None
    tag = re.sub(r"[^a-z0-9/_-]+", "-", type_.lower()).strip("-")[:64].rstrip("-/_")
    return tag if re.fullmatch(r"[a-z0-9][a-z0-9/_-]{0,63}", tag) else None


def to_cloudevent(ev: dict[str, Any]) -> dict[str, Any]:
    """Map a stored dayfile event to a structured-mode CloudEvent."""
    ce: dict[str, Any] = {
        "specversion": SPECVERSION,
        "id": ev["id"],
        "source": quote(ev["source"]["id"], safe=SOURCE_SAFE),
        "type": EXPORT_TYPE,
        "time": ev["occurred_at"],
        "dayfilespec": ev["specversion"],
        "dayfiletitle": ev["title"],
        "dayfilevisibleat": ev["visible_at"],
        "dayfileaudience": ",".join(ev["audience"]),
        "dayfileurgency": ev.get("urgency", 0),
    }
    if "payload" in ev:
        ce["datacontenttype"] = "application/json"
        ce["data"] = ev["payload"]
    if "ref" in ev["source"]:
        ce["dayfilesourceref"] = ev["source"]["ref"]
    for attr, field in (("dayfilebody", "body"), ("dayfileexpiresat", "expires_at")):
        if field in ev:
            ce[attr] = ev[field]
    if ev.get("tags"):
        ce["dayfiletags"] = ",".join(ev["tags"])
    for attr, field in JSON_ATTRS.items():
        if field in ev:
            ce[attr] = json.dumps(ev[field], separators=(",", ":"))
    if "seq" in ev:  # informational only; the importer assigns its own
        ce["dayfileseq"] = ev["seq"]
        ce["dayfilerecordedat"] = ev["recorded_at"]
    return ce


def from_cloudevent(ce: dict[str, Any], now: str) -> dict[str, Any]:
    """Map a structured-mode CloudEvent to a dayfile event envelope; raise ValueError if invalid."""
    if not isinstance(ce, dict):
        raise ValueError("a CloudEvent must be a JSON object")
    if ce.get("specversion") != SPECVERSION:
        raise ValueError(f"unsupported specversion: {ce.get('specversion')!r}")
    for name in ("id", "source", "type"):
        if name not in ce:
            raise ValueError(f"missing required attribute: {name}")
        if not isinstance(ce[name], str) or not ce[name]:
            raise ValueError(f"attribute {name} must be a non-empty string")
    native = "dayfilespec" in ce
    if native:
        source = {"id": unquote(ce["source"])}
        if "dayfilesourceref" in ce:
            source["ref"] = ce["dayfilesourceref"]
        event_id = ce["id"]
    else:
        source, event_id = {"id": ce["source"], "ref": ce["id"]}, new_ulid()
    try:
        occurred = normalize(ce["time"]) if "time" in ce else now
    except ValueError as e:
        raise ValueError(f"time: {e}") from None
    ev: dict[str, Any] = {
        "specversion": ce.get("dayfilespec", "dayfile/1"),
        "id": event_id,
        "source": source,
        "occurred_at": occurred,
        "title": str(ce.get("dayfiletitle") or ce.get("subject") or ce["type"])[:300],
    }
    for attr, field in TEXT_ATTRS.items():
        if attr in ce and field != "title":
            ev[field] = ce[attr]
    for attr, field in LIST_ATTRS.items():
        if ce.get(attr):
            ev[field] = str(ce[attr]).split(",")
    if not native and (tag := _type_tag(ce["type"])):
        ev["tags"] = [*ev.get("tags", []), tag]
    if "dayfileurgency" in ce:
        ev["urgency"] = ce["dayfileurgency"]
    for attr, field in JSON_ATTRS.items():
        if attr in ce:
            try:
                ev[field] = json.loads(ce[attr])
            except (TypeError, ValueError):
                raise ValueError(f"{attr} is not valid JSON") from None
    if "data" in ce:
        ev["payload"] = _payload(ce["data"], ce.get("datacontenttype", "application/json"))
    elif "data_base64" in ce:
        ev["payload"] = {"data_base64": ce["data_base64"]}
    return ev


def _payload(data: Any, content_type: str) -> dict[str, Any]:
    is_json = content_type.split(";")[
        0
    ].strip().lower() == "application/json" or content_type.split(";")[0].strip().lower().endswith(
        "+json"
    )
    return data if is_json and isinstance(data, dict) else {"data": data}
