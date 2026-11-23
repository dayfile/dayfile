"""MCP server (stdio) that lets an AI assistant read and write a dayfile log over its API."""

import asyncio
import functools
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

Request = Callable[[str, str, Any], Awaitable[tuple[int, Any]]]  # method, path and query, body

TOKEN_VARS = ("DAYFILE_READ_TOKEN", "DAYFILE_WRITE_TOKEN")

INSTRUCTIONS = (
    "dayfile is an append-only event log. Events are never edited: to finish a to-do, close it; "
    "to-dos are events tagged `todo`. Read with list_events, write with add_event. "
    "A silent source shows up as an open to-do tagged `heartbeat`; list_events with "
    "tags=['heartbeat'] and status='open' says what has gone quiet."
)


class DayfileError(Exception):
    """The log refused or could not be reached; the message goes back to the assistant."""


def _version() -> str:
    """The installed dayfile version, for the MCP handshake."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("dayfile")
    except PackageNotFoundError:
        return ""


def http_request(url: str, read_token: str | None, write_token: str | None) -> Request:
    """Return a requester for the dayfile API at `url`: reads use one token, writes the other."""

    def blocking(method: str, path: str, body: Any) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        token = read_token if method == "GET" else write_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url.rstrip("/") + path, data, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.load(e)
            except ValueError:
                return e.code, {"error": e.reason}
        except OSError as e:
            raise DayfileError(f"cannot reach {url}: {e}") from e

    async def call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
        return await asyncio.to_thread(blocking, method, path, body)

    return call


def _ok(status: int, body: Any) -> Any:
    if status >= 400:
        message = body.get("error", body) if isinstance(body, dict) else body
        hint = (
            " (reads need DAYFILE_READ_TOKEN, writes DAYFILE_WRITE_TOKEN)"
            if status in (401, 403)
            else ""
        )
        raise DayfileError(f"{status}: {message}{hint}")
    return body


async def list_events(
    request: Request,
    tags: list[str],
    status: str | None,
    within: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Newest events first, filtered by tags (all must match), status and recency."""
    query = [("tag", t) for t in tags[:5]] + [("limit", str(max(1, min(limit, 100))))]
    if status:
        query.append(("status", status))
    if within:
        query.append(("within", within))
    body = _ok(*await request("GET", "/events?" + urllib.parse.urlencode(query), None))
    return body["events"]


async def get_event(request: Request, event_id: str) -> dict[str, Any]:
    """One event by id."""
    return _ok(*await request("GET", "/events/" + urllib.parse.quote(event_id, safe=""), None))


async def add_event(
    request: Request,
    title: str,
    body: str | None,
    tags: list[str],
    todo: bool,
    urgency: int,
    due: str | None,
) -> dict[str, Any]:
    """Append an event; `todo` or `due` tags it as a to-do."""
    event: dict[str, Any] = {"specversion": "dayfile/1", "title": title}
    all_tags = [*tags, "todo"] if (todo or due) and "todo" not in tags else tags
    optional = {
        "body": body,
        "tags": all_tags,
        "urgency": urgency,
        "payload": {"due": due} if due else None,
    }
    event.update({k: v for k, v in optional.items() if v})
    stored = _ok(*await request("POST", "/events", [event]))
    return stored[0] if isinstance(stored, list) else stored


async def _refer(
    request: Request, rel: str, event_id: str, note: str | None = None
) -> dict[str, Any]:
    """Append an event that points at `event_id`; its title is inherited from the target."""
    event: dict[str, Any] = {"specversion": "dayfile/1", "refs": [{"rel": rel, "id": event_id}]}
    if note:
        event["body"] = note
    stored = _ok(*await request("POST", "/events", [event]))
    return stored[0] if isinstance(stored, list) else stored


async def close_todo(request: Request, event_id: str, note: str | None = None) -> dict[str, Any]:
    """Close a to-do by appending an event that completes it; `note` says why."""
    return await _refer(request, "completes", event_id, note)


async def retract_event(request: Request, event_id: str) -> dict[str, Any]:
    """Retract an event (or undo a close) by appending an event that retracts it."""
    return await _refer(request, "retracts", event_id)


def build_server(request: Request) -> Any:
    """Create the MCP server with its five tools bound to `request`."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = MCPServer("dayfile", instructions=INSTRUCTIONS, version=_version())

    def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Show the assistant a refusal's message instead of a generic crash."""

        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except DayfileError as e:
                raise ToolError(str(e)) from e

        return run

    read_only = ToolAnnotations(readOnlyHint=True)

    @server.tool(annotations=read_only)
    @guarded
    async def dayfile_list_events(
        tags: list[str] | None = None,
        status: str | None = None,
        within: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List events, newest first. `tags`: all must match (max 5). `status`: open, closed,
        superseded, retracted or none (to-dos are open until closed). `within`: e.g. 7d or 12h."""
        return await list_events(request, tags or [], status, within, limit)

    @server.tool(annotations=read_only)
    @guarded
    async def dayfile_get_event(id: str) -> dict[str, Any]:
        """Get one event by its id."""
        return await get_event(request, id)

    @server.tool()
    @guarded
    async def dayfile_add_event(
        title: str,
        body: str | None = None,
        tags: list[str] | None = None,
        todo: bool = False,
        urgency: int = 0,
        due: str | None = None,
    ) -> dict[str, Any]:
        """Append an event to the log. Set `todo` for something to be done later, `due` (a date
        such as 2026-12-01) to give it a deadline, `urgency` 0 to 3 for how loudly to notify."""
        return await add_event(request, title, body, tags or [], todo, urgency, due)

    @server.tool()
    @guarded
    async def dayfile_close_todo(id: str, note: str | None = None) -> dict[str, Any]:
        """Close an open to-do by id. Nothing is deleted; a closing event is appended.
        `note` says why it was closed and is shown with the closing step."""
        return await close_todo(request, id, note)

    @server.tool()
    @guarded
    async def dayfile_retract_event(id: str) -> dict[str, Any]:
        """Retract an event that was a mistake. Nothing is deleted; a retracting event is
        appended. Retracting a closing event reopens its target."""
        return await retract_event(request, id)

    return server


def main() -> int:
    """Run on stdio, configured by DAYFILE_URL, DAYFILE_READ_TOKEN and DAYFILE_WRITE_TOKEN."""
    url = os.environ.get("DAYFILE_URL", "http://127.0.0.1:8000")
    read, write = (os.environ.get(n, "").strip() or None for n in TOKEN_VARS)
    request = http_request(url, read, write)
    build_server(request).run("stdio")
    return 0
