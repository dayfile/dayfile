"""FastAPI app and routes."""

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from dayfile import admin as admin_mod
from dayfile import db, feeds, logsafe, signing, timefmt
from dayfile import tenancy as tenancy_mod
from dayfile.auth.tokens import Principal, verify_token
from dayfile.cloudevents import from_cloudevent, to_cloudevent
from dayfile.icons import ICONS, STATIC
from dayfile.ingest.core import ConflictError, ForbiddenError, IngestError, ingest_events
from dayfile.models import with_defaults
from dayfile.ratelimit import Held, Window
from dayfile.render.atom import render_atom
from dayfile.render.jsonfeed import render_json
from dayfile.render.query import FeedQuery, QueryError, parse_query
from dayfile.tenancy import DirectoryTenancy, Tenancy, Tenant
from dayfile.ulid import new_ulid

MEDIA_TYPES = {
    "json": "application/feed+json",
    "atom": "application/atom+xml",
    "ce": "application/cloudevents-batch+json",
}
CE_TYPES = ("application/cloudevents+json", "application/cloudevents-batch+json")
PRIVATE = {"Cache-Control": "private, no-cache", "Vary": "Authorization"}
PUBLIC = {"Cache-Control": "public, max-age=60", "Vary": "Authorization"}
PUBLIC_LABEL = frozenset({"public"})  # events labeled public are visible to every reader
ANONYMOUS = Principal("anonymous", PUBLIC_LABEL)


def create_app(
    db_path: str | Path | None = None,
    clock: Callable[[], str] = timefmt.now,
    auth_uri: str | None = None,
    write_limit: tuple[int, int] = (600, 60),
    monotonic: Callable[[], float] = time.monotonic,
    poll_interval: float = 0.5,
    stream_seconds: float = 300,
    tenancy: Tenancy | None = None,
    max_body_bytes: int = 1024 * 1024,
    read_limit: tuple[int, int] = (300, 60),
    held_limit: int = 8,
    admin_secret_file: str | Path | None = None,
    admin_limit: tuple[int, int] = (30, 60),
    anon_read_limit: tuple[int, int] = (120, 60),
    mcp: bool | None = None,
    oauth_issuer: str | None = None,
) -> FastAPI:
    """Build the app; the database path defaults to $DAYFILE_DB or ./dayfile.db.

    `tenancy` maps the Host header to a database and limits. The default serves every host
    from the one database; $DAYFILE_TENANTS_DIR with $DAYFILE_BASE_DOMAIN selects one database
    per subdomain. An explicit `db_path` always means a single database.
    `max_body_bytes` caps a POST /events body (413 above it).
    `admin_secret_file` (or $DAYFILE_ADMIN_SECRET_FILE) turns on the control-plane API under
    /internal/admin/ for a DirectoryTenancy; `admin_limit` is its (writes, seconds) budget.
    `anon_read_limit` is (requests, seconds) shared by all anonymous readers of one tenant's
    public feeds.
    `mcp` (or DAYFILE_MCP=1) serves the MCP endpoint at /mcp (needs `dayfile[mcp]`);
    `oauth_issuer` (or $DAYFILE_OAUTH_ISSUER) names the server that signs assistants in.
    `read_limit` is (requests, seconds) per read token; `held_limit` is how many streams and
    long-polls one token may hold open at once. Both answer 429 with Retry-After.

    `write_limit` is (events, seconds): each write token may post that many events per window
    (a fixed window per token id, kept in memory) before getting 429.
    `poll_interval` is how often long-polls and streams look for new events; a stream ends
    after `stream_seconds` and clients reconnect with Last-Event-ID.
    `auth_uri` (or $DAYFILE_AUTH_URI) is advertised to unauthenticated clients and in feeds as
    the place to obtain access.
    """
    auth_uri = auth_uri or os.environ.get("DAYFILE_AUTH_URI") or None
    if auth_uri and not re.fullmatch(r"https?://[^\s\"<>]+", auth_uri):
        raise ValueError("auth_uri must be an http(s) URL without spaces, quotes or brackets")
    tenancy = tenancy or tenancy_mod.from_environment(db_path)
    app = FastAPI(title="dayfile")
    logsafe.install("uvicorn.access", "uvicorn.error", "dayfile")

    budgets = Window(monotonic)
    held = Held(monotonic)

    def charge(tenant: Tenant, token_id: str, count: int) -> int:
        """Spend `count` events from the token's write window; return 0 or the seconds to wait."""
        limit, span = tenant.write_limit or write_limit
        return budgets.charge(("write", tenant.id, token_id), count, limit, span)

    def throttle(tenant: Tenant, principal) -> Response | None:
        """Spend one read from the caller's budget, or return the 429."""
        limit, span = read_limit
        if wait := budgets.charge(("read", tenant.id, principal.token_id), 1, limit, span):
            return _error(
                429,
                "read rate limit exceeded",
                {"Retry-After": str(wait), "Cache-Control": "no-store"},
            )
        return None

    def gate(request: Request) -> Tenant | Response:
        """Resolve the tenant for this request, or return the refusal."""
        tenant = tenancy.resolve(request.headers.get("host", ""))
        headers = {"Cache-Control": "no-store"}
        if tenant is None:
            return _error(404, "unknown tenant", headers)
        if tenant.suspended:
            return _error(403, "account suspended", headers)
        return tenant

    def store(tenant: Tenant, events: list[Any], writer):
        conn = db.connect(tenant.db_path)
        try:
            return ingest_events(conn, events, writer)
        finally:
            conn.close()

    def authenticate(tenant: Tenant, credential: str, now: str):
        conn = db.connect(tenant.db_path)
        try:
            return verify_token(conn, credential, now)
        finally:
            conn.close()

    def fetch(tenant: Tenant, q, now, labels):
        conn = db.connect(tenant.db_path)
        try:
            events, has_more = db.query_events(conn, q, now, labels)
            signing.annotate(conn, events)
            return events, has_more
        finally:
            conn.close()

    async def api_auth(request: Request, tenant: Tenant, now: str):
        """Authenticate a Bearer-only API request; return a Principal or an error Response."""
        credential = _bearer(request)
        found = (
            await run_in_threadpool(authenticate, tenant, credential, now) if credential else None
        )
        principal = found
        if principal is None:
            return _unauthorized(auth_uri)
        if principal.writer_sources:
            return _error(403, "write-only credential", {"Cache-Control": "no-store"})
        return principal

    def load_feed(tenant: Tenant, name: str):
        conn = db.connect(tenant.db_path)
        try:
            return feeds.get_feed(conn, name) if feeds.NAME.match(name) else None
        finally:
            conn.close()

    async def feed(
        request: Request, fmt: str, path_token: str | None = None, name: str = feeds.DEFAULT
    ) -> Response:
        tenant = gate(request)
        if isinstance(tenant, Response):
            return tenant
        now = clock()
        definition = await run_in_threadpool(load_feed, tenant, name)
        credential = _bearer(request) or path_token
        principal = None
        if credential:
            principal = await run_in_threadpool(authenticate, tenant, credential, now)
        anonymous = False
        if principal is None:
            public = definition is not None and definition["visibility"] == "public"
            if credential or not public:
                return _unauthorized(auth_uri)  # a private or missing feed looks the same
            principal, anonymous = ANONYMOUS, True
        elif principal.writer_sources:
            return _error(403, "write-only credential", {"Cache-Control": "no-store"})
        elif definition is None:
            return _error(404, "no such feed", {"Cache-Control": "no-store"})
        if anonymous:
            limit, span = anon_read_limit
            if wait := budgets.charge(("anon", tenant.id), 1, limit, span):
                return _error(
                    429,
                    "read rate limit exceeded",
                    {"Retry-After": str(wait), "Cache-Control": "no-store"},
                )
        elif refused := throttle(tenant, principal):
            return refused
        try:
            pairs = list(request.query_params.multi_items())
            if name != feeds.DEFAULT and any(k not in feeds.PAGING for k, _ in pairs):
                raise QueryError("a named feed takes only paging parameters (limit, since_seq)")
            if anonymous and any(k not in feeds.PAGING for k, _ in pairs):
                raise QueryError("a public feed takes only paging parameters (limit, since_seq)")
            q = parse_query([(k, v) for k, v in definition["filter"]] + pairs)
        except QueryError as e:
            return _error(400, str(e), PRIVATE)
        labels = principal.labels | PUBLIC_LABEL
        events, has_more = await run_in_threadpool(fetch, tenant, q, now, labels)
        cursor = max([q.since_seq or 0, *(e["seq"] for e in events)])  # late releases sit lower
        feed_url = str(request.url)
        next_url = None
        if has_more:
            next_url = str(request.url.include_query_params(since_seq=cursor, released_after=now))
        display = events[::-1]  # newest first
        if fmt == "json":
            doc = render_json(
                display, feed_url, next_url, cursor, auth_uri, now, definition["title"] or "dayfile"
            )
            body = json.dumps(doc).encode()
        elif fmt == "atom":
            body = render_atom(
                display, feed_url, next_url, auth_uri, cursor, now, definition["title"] or "dayfile"
            )
        else:
            body = json.dumps([to_cloudevent(e) for e in display]).encode()
        etag = _etag(principal, fmt, request, events, cursor, has_more)
        headers = {"ETag": etag, **(PUBLIC if anonymous else PRIVATE)}
        links = [
            f'<{u}>; rel="{rel}"'
            for u, rel in ((next_url, "next"), (auth_uri, "authorization"))
            if u
        ]
        if links:
            headers["Link"] = ", ".join(links)
        if _matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type=MEDIA_TYPES[fmt], headers=headers)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        """Send a browser that opens the bare server address to the API docs."""
        return RedirectResponse("/docs", status_code=307)

    @app.get("/usage")
    async def usage(request: Request) -> Response:
        """How full this log is, for the holder of a read token; null limits mean unlimited."""
        tenant = gate(request)
        if isinstance(tenant, Response):
            return tenant
        who = await api_auth(request, tenant, clock())
        if isinstance(who, Response):
            return who
        if refused := throttle(tenant, who):
            return refused
        events = await run_in_threadpool(tenancy_mod.count_events, tenant)
        size = await run_in_threadpool(tenancy_mod.stored_bytes, tenant)
        return JSONResponse(
            {
                "events": events,
                "max_events": tenant.max_events,
                "bytes": size,
                "max_bytes": tenant.max_bytes,
                "write_limit": list(tenant.write_limit) if tenant.write_limit else None,
            },
            headers=PRIVATE,
        )

    @app.get("/events")
    async def list_events(request: Request) -> Response:
        tenant = gate(request)
        if isinstance(tenant, Response):
            return tenant
        who = await api_auth(request, tenant, clock())
        if isinstance(who, Response):
            return who
        if refused := throttle(tenant, who):
            return refused
        try:
            pairs = request.query_params.multi_items()
            wait = _wait_seconds([v for k, v in pairs if k == "wait"])
            q = parse_query([(k, v) for k, v in pairs if k != "wait"])
            if wait and q.since_seq is None:
                raise QueryError("wait requires since_seq")
        except QueryError as e:
            return _error(400, str(e), PRIVATE)
        lease = None
        if wait:
            lease = held.acquire((tenant.id, who.token_id), held_limit, wait + 5)
            if lease is None:
                return _busy("too many open long-polls")
        deadline = time.monotonic() + wait
        try:
            while True:
                now = clock()
                events, has_more = await run_in_threadpool(
                    fetch, tenant, q, now, who.labels | PUBLIC_LABEL
                )
                if events or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_interval)
                who = await api_auth(request, tenant, clock())  # a revoked token stops waiting
                if isinstance(who, Response):
                    return who
        finally:
            held.release(lease)
        cursor = max([q.since_seq or 0, *(e["seq"] for e in events)])
        next_url = None
        if has_more:
            url = request.url.remove_query_params("wait")
            next_url = str(url.include_query_params(since_seq=cursor, released_after=now))
        body = {"events": events, "cursor": cursor, "released_cursor": now, "next": next_url}
        return _json_response(request, who, "events", body, events, cursor, has_more)

    @app.get("/events/stream")
    async def stream_events(request: Request) -> Response:
        tenant = gate(request)
        if isinstance(tenant, Response):
            return tenant
        who = await api_auth(request, tenant, clock())
        if isinstance(who, Response):
            return who
        if refused := throttle(tenant, who):
            return refused
        try:
            q = _stream_query(request)
        except QueryError as e:
            return _error(400, str(e), PRIVATE)
        if q.since_seq is None:  # no cursor: start from now, replaying nothing
            tail, _ = await run_in_threadpool(fetch, tenant, q, clock(), who.labels | PUBLIC_LABEL)
            q.since_seq = max((e["seq"] for e in tail), default=0)
            q.released_after = clock()

        lease = held.acquire((tenant.id, who.token_id), held_limit, stream_seconds + 30)
        if lease is None:
            return _busy("too many open streams")

        async def frames():
            nonlocal who
            try:
                cursor, released = q.since_seq or 0, q.released_after
                end = time.monotonic() + stream_seconds
                last_beat = time.monotonic()
                while time.monotonic() < end and not await request.is_disconnected():
                    now = clock()
                    batch = FeedQuery(
                        **{**q.__dict__, "since_seq": cursor, "released_after": released}
                    )
                    events, more = await run_in_threadpool(
                        fetch, tenant, batch, now, who.labels | PUBLIC_LABEL
                    )
                    for e in events:
                        cursor = max(cursor, e["seq"])
                        last_beat = time.monotonic()
                        yield f"id: {cursor}:{now}\nevent: event\ndata: {json.dumps(e)}\n\n"
                    released = now
                    if more:
                        continue
                    if time.monotonic() - last_beat > 15:
                        last_beat = time.monotonic()
                        yield ": keep-alive\n\n"
                    await asyncio.sleep(poll_interval)
                    refreshed = await api_auth(request, tenant, clock())
                    if isinstance(refreshed, Response):
                        return  # revoked or expired mid-stream
                    who = refreshed
            finally:
                held.release(lease)

        headers = {"Cache-Control": "private, no-cache", "Vary": "Authorization"}
        headers["X-Accel-Buffering"] = "no"  # keep nginx from buffering the stream
        return StreamingResponse(frames(), media_type="text/event-stream", headers=headers)

    @app.get("/events/{event_id}")
    async def get_event(request: Request, event_id: str) -> Response:
        tenant = gate(request)
        if isinstance(tenant, Response):
            return tenant
        now = clock()
        who = await api_auth(request, tenant, now)
        if isinstance(who, Response):
            return who
        if refused := throttle(tenant, who):
            return refused
        events, _ = await run_in_threadpool(
            fetch, tenant, FeedQuery(event_id=event_id), now, who.labels | PUBLIC_LABEL
        )
        if not events:
            return _error(404, "not found", PRIVATE)
        return _json_response(request, who, "event", events[0], events, events[0]["seq"], False)

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    async def healthz() -> JSONResponse:
        """Liveness plus database reachability; public and reveals nothing else."""

        headers = {"Cache-Control": "no-store"}
        try:
            ok = await run_in_threadpool(tenancy.healthy)
        except Exception:
            ok = False
        if not ok:
            return JSONResponse({"status": "unavailable"}, status_code=503, headers=headers)
        return JSONResponse({"status": "ok"}, headers=headers)

    @app.api_route("/status", methods=["GET", "HEAD"])
    async def status() -> JSONResponse:
        """The same check as `/healthz`, named per component, for people and status pages."""

        try:
            ok = await run_in_threadpool(tenancy.healthy)
        except Exception:
            ok = False
        state = "ok" if ok else "unavailable"
        return JSONResponse(
            {"status": state, "components": {"api": "ok", "storage": state}, "checked_at": clock()},
            status_code=200 if ok else 503,
            headers={"Cache-Control": "no-store"},
        )

    def icon_route(path: str, name: str, media_type: str) -> None:
        body = (STATIC / name).read_bytes()

        @app.api_route(path, methods=["GET", "HEAD"], include_in_schema=False)
        async def icon() -> Response:
            """The dayfile icon: public, the same on every host, and not tied to a credential."""
            return Response(
                body, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"}
            )

    for path, (name, media_type) in ICONS.items():
        icon_route(path, name, media_type)

    secret = admin_mod.read_secret(admin_secret_file or os.environ.get("DAYFILE_ADMIN_SECRET_FILE"))
    if secret and isinstance(tenancy, DirectoryTenancy):
        admin_mod.install(app, tenancy, secret, clock, Window(monotonic), admin_limit)

    @app.get("/internal/tls-ask")
    async def tls_ask(domain: str = "") -> Response:
        """Tell a reverse proxy whether it may obtain a certificate for `domain`.

        Only live tenant hostnames qualify. The proxy must not expose this path publicly.
        """
        known = False
        if isinstance(tenancy, DirectoryTenancy):
            tenant = await run_in_threadpool(tenancy.resolve, domain)
            known = tenant is not None and not tenant.suspended
        return Response(status_code=200 if known else 404, headers={"Cache-Control": "no-store"})

    @app.get("/feed.json")
    async def feed_json(request: Request) -> Response:
        return await feed(request, "json")

    @app.get("/feed.atom")
    async def feed_atom(request: Request) -> Response:
        return await feed(request, "atom")

    @app.get("/feed.cloudevents")
    async def feed_ce(request: Request) -> Response:
        return await feed(request, "ce")

    @app.get("/feed")
    async def feed_bare(request: Request) -> Response:
        return await feed(request, "json")

    @app.get("/f/{name}.json")
    async def named_json(request: Request, name: str) -> Response:
        return await feed(request, "json", name=name)

    @app.get("/f/{name}.atom")
    async def named_atom(request: Request, name: str) -> Response:
        return await feed(request, "atom", name=name)

    @app.get("/f/{name}.cloudevents")
    async def named_ce(request: Request, name: str) -> Response:
        return await feed(request, "ce", name=name)

    @app.get("/f/{name}")
    async def named_bare(request: Request, name: str) -> Response:
        return await feed(request, "json", name=name)

    @app.get("/t/{token}/feed")
    async def feed_bare_token(request: Request, token: str) -> Response:
        return await feed(request, "json", token)

    @app.get("/t/{token}/f/{name}.json")
    async def named_json_token(request: Request, token: str, name: str) -> Response:
        return await feed(request, "json", token, name)

    @app.get("/t/{token}/f/{name}.atom")
    async def named_atom_token(request: Request, token: str, name: str) -> Response:
        return await feed(request, "atom", token, name)

    @app.get("/t/{token}/f/{name}.cloudevents")
    async def named_ce_token(request: Request, token: str, name: str) -> Response:
        return await feed(request, "ce", token, name)

    @app.get("/t/{token}/f/{name}")
    async def named_bare_token(request: Request, token: str, name: str) -> Response:
        return await feed(request, "json", token, name)

    @app.get("/t/{token}/feed.cloudevents")
    async def feed_ce_token(request: Request, token: str) -> Response:
        return await feed(request, "ce", token)

    @app.get("/t/{token}/feed.json")
    async def feed_json_token(request: Request, token: str) -> Response:
        return await feed(request, "json", token)

    @app.get("/t/{token}/feed.atom")
    async def feed_atom_token(request: Request, token: str) -> Response:
        return await feed(request, "atom", token)

    @app.post("/events")
    async def post_events(request: Request) -> JSONResponse:
        return await accept_events(request, None)

    @app.post("/events/{event_id}/close")
    async def close_event(request: Request, event_id: str) -> JSONResponse:
        """Close a to-do: appends an event that completes it, with an optional title and body."""
        return await accept_events(request, event_id)

    async def accept_events(request: Request, close_id: str | None) -> JSONResponse:
        tenant = gate(request)
        if isinstance(tenant, Response):
            return tenant
        credential = _bearer(request)  # writes never take a path or query credential
        writer = None
        if credential:
            writer = await run_in_threadpool(authenticate, tenant, credential, clock())
        if writer is None:
            return _unauthorized(auth_uri)
        if not writer.writer_sources:
            return _error(403, "read-only credential", {"Cache-Control": "no-store"})
        try:
            raw = await _read_body(request, max_body_bytes)
        except _TooLarge:
            return _error(413, f"request body too large (limit {max_body_bytes} bytes)")
        try:
            body = json.loads(raw) if raw.strip() or close_id is None else {}
        except ValueError:
            return _error(422, "body is not valid JSON")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if close_id is not None:
            try:
                body, ctype = _closing_event(close_id, body), ""
            except ValueError as e:
                return _error(422, str(e))
        if ctype in CE_TYPES:
            if (ctype == CE_TYPES[1]) != isinstance(body, list):
                return _error(
                    422, f"{ctype} expects {'an array' if ctype == CE_TYPES[1] else 'one object'}"
                )
            try:
                events = [
                    from_cloudevent(x, clock())
                    for x in (body if isinstance(body, list) else [body])
                ]
            except ValueError as e:
                return _error(422, f"cloudevent: {e}")
        else:
            events = [
                with_defaults(x, clock(), writer.writer_sources, new_ulid())
                for x in (body if isinstance(body, list) else [body])
            ]
        if not events:
            return _error(422, "no events supplied")
        if tenant.max_events is not None:
            used = await run_in_threadpool(tenancy_mod.count_events, tenant)
            if used + len(events) > tenant.max_events:
                return _error(403, "event quota exceeded", {"Cache-Control": "no-store"})
        if tenant.max_bytes is not None:
            size = await run_in_threadpool(tenancy_mod.stored_bytes, tenant)
            if size >= tenant.max_bytes:
                return _error(403, "storage quota exceeded", {"Cache-Control": "no-store"})
        if wait := charge(tenant, writer.token_id, len(events)):
            return _error(429, "write rate limit exceeded", {"Retry-After": str(wait)})
        try:
            results = await run_in_threadpool(store, tenant, events, writer)
        except IngestError as e:
            return _error(422, str(e))
        except ForbiddenError as e:
            return _error(403, str(e))
        except ConflictError as e:
            return _error(409, str(e))
        stored = [r.event for r in results]
        status = 201 if any(r.created for r in results) else 200
        return JSONResponse(stored if isinstance(body, list) else stored[0], status_code=status)

    if mcp if mcp is not None else os.environ.get("DAYFILE_MCP") == "1":
        try:
            from dayfile import mcp_http
        except ModuleNotFoundError as e:
            raise RuntimeError("the MCP endpoint needs `pip install dayfile[mcp]`") from e
        issuer = oauth_issuer or os.environ.get("DAYFILE_OAUTH_ISSUER") or None
        main = (tenancy.base_domain, tenancy.get) if isinstance(tenancy, DirectoryTenancy) else None
        mcp_http.mount(app, tenancy.resolve, authenticate, clock, issuer, main)
    return app


def _closing_event(event_id: str, body: Any) -> dict[str, Any]:
    """The event that completes `event_id`, from an optional title and body."""
    if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", event_id):
        raise ValueError("not an event id")
    if not isinstance(body, dict) or set(body) - {"title", "body"}:
        raise ValueError("send a JSON object with only a title and a body, or nothing")
    return {"refs": [{"rel": "completes", "id": event_id}], **body}


class _TooLarge(Exception):
    """The request body exceeded the configured limit."""


async def _read_body(request: Request, limit: int) -> bytes:
    """Read the body, refusing early on a declared length and while streaming otherwise."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _TooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _etag(principal, fmt: str, request: Request, events, cursor: int, has_more: bool) -> str:
    """Validator over who is asking and what they would see, not over server time.

    The body embeds the server's `now` (released_cursor), so hashing it would make every
    response look new. Content, cursor and caller are what matter: a held event being
    released, or a status change, alters `events` and therefore the tag.
    """
    identity = "\0".join([principal.token_id, *sorted(principal.labels)])
    content = json.dumps(events, sort_keys=True, separators=(",", ":"))
    material = "\0".join([identity, fmt, str(request.url), str(cursor), str(has_more), content])
    return '"' + hashlib.sha256(material.encode()).hexdigest()[:32] + '"'


def _json_response(request, principal, kind, body, events, cursor, has_more) -> Response:
    """Serialize an API body with the caller-specific ETag, honouring If-None-Match."""
    etag = _etag(principal, kind, request, events, cursor, has_more)
    headers = {"ETag": etag, **PRIVATE}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)


def _wait_seconds(values: list[str]) -> int:
    """Parse the optional long-poll `wait` parameter (0 to 30 seconds)."""
    if not values:
        return 0
    if len(values) > 1 or not re.fullmatch(r"[0-9]{1,3}", values[0]) or int(values[0]) > 30:
        raise QueryError("wait must be a whole number of seconds from 0 to 30")
    return int(values[0])


def _stream_query(request: Request) -> FeedQuery:
    """Filters for /events/stream; Last-Event-ID (`seq:released_at`) resumes a dropped stream."""
    q = parse_query(request.query_params.multi_items())
    resume = request.headers.get("last-event-id", "")
    if resume and q.since_seq is None:
        seq, _, released = resume.partition(":")
        try:
            q.since_seq = int(seq) if re.fullmatch(r"[0-9]+", seq) else None
            q.released_after = timefmt.normalize(released) if released else None
        except ValueError:
            q.since_seq = q.released_after = None  # an unusable id starts from now
    q.limit = 200
    return q


def _matches(header: str | None, etag: str) -> bool:
    """True if an If-None-Match header names this ETag (weak comparison) or `*`."""
    if not header:
        return False
    return any(t.strip().removeprefix("W/") in (etag, "*") for t in header.split(","))


def _bearer(request: Request) -> str | None:
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    return credential.strip() if scheme.lower() == "bearer" and credential.strip() else None


def _unauthorized(auth_uri: str | None) -> JSONResponse:
    """401 with a standard Bearer challenge and an experimental `Feed` discovery challenge."""
    response = _error(
        401, "authentication required", {"Cache-Control": "no-store", "Vary": "Authorization"}
    )
    feed = 'Feed realm="dayfile", path="/t/{token}/"'
    if auth_uri:
        feed += f', authorization_uri="{auth_uri}"'
    response.headers.append("WWW-Authenticate", 'Bearer realm="dayfile"')
    response.headers.append("WWW-Authenticate", feed)
    return response


def _busy(message: str) -> JSONResponse:
    """429 for a caller already holding as many open requests as it may."""
    return _error(429, message, {"Retry-After": "5", "Cache-Control": "no-store"})


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


app = create_app()
