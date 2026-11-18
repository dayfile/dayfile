"""Admin API for a control plane (the web app): tenants and their tokens, under /internal/admin/.

Never routed by the proxy. Every call needs the shared secret read from a file; without the file
the routes do not exist. Token changes are recorded in the tenant's own log (tag dayfile-admin).
"""

import hmac
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from dayfile import db, feeds, heartbeats, timefmt
from dayfile.auth.tokens import LABEL, create_token, list_tokens, revoke_token
from dayfile.ingest.core import ingest_events
from dayfile.ratelimit import Window
from dayfile.tenancy import RESERVED, SLUG, DirectoryTenancy, TenancyError, Tenant
from dayfile.ulid import new_ulid

log = logging.getLogger("dayfile.admin")
PREFIX = "/internal/admin"
MAX_LIVE_TOKENS = 20
SHORT_LIVED = 3600  # tokens expiring this soon are session plumbing: uncapped and not logged
MIN_EXPIRY, MAX_EXPIRY = 60, 366 * 86400
NO_STORE = {"Cache-Control": "no-store"}


def read_secret(path: str | Path | None) -> str | None:
    """Return the shared secret from its file, or None if there is no usable file."""
    if not path:
        return None
    try:
        secret = Path(path).read_text().strip()
    except OSError:
        return None
    return secret or None


def install(
    app: FastAPI,
    tenancy: DirectoryTenancy,
    secret: str,
    clock: Callable[[], str],
    budget: Window,
    limit: tuple[int, int],
) -> None:
    """Register the admin routes on `app`."""

    def refuse(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status, headers=NO_STORE)

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(value.encode(), secret.encode())

    def guard(request: Request, mutating: bool) -> JSONResponse | None:
        if not authorized(request):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={**NO_STORE, "WWW-Authenticate": 'Bearer realm="dayfile-admin"'},
            )
        if mutating and (wait := budget.charge("admin", 1, *limit)):
            return JSONResponse(
                {"error": "admin rate limit exceeded"},
                status_code=429,
                headers={**NO_STORE, "Retry-After": str(wait)},
            )
        return None

    def known(slug: str) -> Tenant | None:
        return tenancy.get(slug) if SLUG.fullmatch(slug) else None

    async def body_of(request: Request) -> dict[str, Any] | None:
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def describe(tenant: Tenant) -> dict[str, Any]:
        usage = tenancy.usage(tenant.id)
        return {
            "slug": tenant.id,
            "suspended": tenant.suspended,
            "max_events": tenant.max_events,
            "max_bytes": tenant.max_bytes,
            "write_limit": list(tenant.write_limit) if tenant.write_limit else None,
            **usage,
        }

    def log_event(tenant: Tenant, action: str, title: str, **payload: Any) -> None:
        conn = db.connect(tenant.db_path)
        try:
            ingest_events(
                conn,
                [
                    {
                        "specversion": "dayfile/1",
                        "id": new_ulid(),
                        "source": {"id": "dayfile"},
                        "occurred_at": clock(),
                        "title": title,
                        "tags": ["dayfile-admin"],
                        "payload": {"action": action, **payload},
                    }
                ],
            )
        finally:
            conn.close()

    @app.put(PREFIX + "/tenants/{slug}")
    async def put_tenant(request: Request, slug: str) -> Response:
        if denied := guard(request, mutating=True):
            return denied
        body = await body_of(request)
        if body is None:
            return refuse(422, "body must be a JSON object")
        existing = known(slug)
        if existing:
            return JSONResponse({**describe(existing), "created": False}, headers=NO_STORE)
        if not SLUG.fullmatch(slug) or slug in RESERVED:
            return refuse(422, "invalid tenant name")
        limits = {k: body[k] for k in ("max_events", "max_bytes", "write_limit") if k in body}
        if isinstance(limits.get("write_limit"), list):
            limits["write_limit"] = tuple(limits["write_limit"])
        try:
            tenant = await run_in_threadpool(lambda: tenancy.create(slug, **limits))
        except TenancyError as e:
            return refuse(422, str(e))
        out = await run_in_threadpool(describe, tenant)
        return JSONResponse({**out, "created": True}, status_code=201, headers=NO_STORE)

    @app.get(PREFIX + "/tenants/{slug}")
    async def get_tenant(request: Request, slug: str) -> Response:
        if denied := guard(request, mutating=False):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")
        return JSONResponse(await run_in_threadpool(describe, tenant), headers=NO_STORE)

    @app.get(PREFIX + "/tenants/{slug}/export")
    async def export_tenant(request: Request, slug: str) -> Response:
        """A gzip of a consistent, integrity-checked copy of the tenant's database."""
        if denied := guard(request, mutating=False):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")

        def make() -> bytes:
            import gzip
            import tempfile

            from dayfile.cli import copy_database

            with tempfile.TemporaryDirectory() as scratch:
                copy = Path(scratch) / "dayfile.db"
                copy_database(tenant.db_path, copy)
                return gzip.compress(copy.read_bytes(), compresslevel=6)

        body = await run_in_threadpool(make)
        log.info("export: tenant=%s bytes=%d", tenant.id, len(body))
        return Response(body, media_type="application/gzip", headers=NO_STORE)

    @app.delete(PREFIX + "/tenants/{slug}")
    async def delete_tenant(request: Request, slug: str) -> Response:
        if denied := guard(request, mutating=True):
            return denied
        if known(slug) is None:
            return refuse(404, "no such tenant")
        erase = request.query_params.get("erase") == "true"
        await run_in_threadpool(tenancy.erase if erase else tenancy.delete, slug)
        return Response(status_code=204, headers=NO_STORE)

    @app.put(PREFIX + "/tenants/{slug}/feeds/{name}")
    async def put_tenant_feed(request: Request, slug: str, name: str) -> Response:
        if denied := guard(request, mutating=True):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")
        spec = await body_of(request)
        if spec is None or set(spec) - {"title", "filter", "visibility"}:
            return refuse(422, "body takes title, filter and visibility")
        if "filter" in spec and not isinstance(spec["filter"], dict):
            return refuse(422, 'filter must be an object like {"tag": ["work"]}')

        def save() -> tuple[dict[str, Any], str | None]:
            conn = db.connect(tenant.db_path)
            try:
                before = (feeds.get_feed(conn, name) or {}).get("visibility")
                saved = feeds.save_feed(
                    conn,
                    name,
                    clock(),
                    title=spec.get("title"),
                    filter=spec.get("filter"),
                    visibility=spec.get("visibility"),
                )
                return saved, before
            finally:
                conn.close()

        try:
            saved, before = await run_in_threadpool(save)
        except feeds.FeedError as e:
            return refuse(422, str(e))
        if saved["visibility"] != (before or "private"):
            verb = "published" if saved["visibility"] == "public" else "made private"
            await run_in_threadpool(
                log_event, tenant, f"feed.{saved['visibility']}", f"Feed {name} {verb}", feed=name
            )
        return JSONResponse(saved, headers=NO_STORE)

    @app.get(PREFIX + "/tenants/{slug}/feeds")
    async def get_tenant_feeds(request: Request, slug: str) -> Response:
        if denied := guard(request, mutating=False):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")

        def rows() -> list[dict[str, Any]]:
            conn = db.connect(tenant.db_path)
            try:
                saved = {f["name"]: f for f in feeds.list_feeds(conn)}
                return [saved.get(feeds.DEFAULT) or feeds.get_feed(conn, feeds.DEFAULT)] + [
                    f for n, f in saved.items() if n != feeds.DEFAULT
                ]
            finally:
                conn.close()

        return JSONResponse(await run_in_threadpool(rows), headers=NO_STORE)

    @app.delete(PREFIX + "/tenants/{slug}/feeds/{name}")
    async def delete_tenant_feed(request: Request, slug: str, name: str) -> Response:
        if denied := guard(request, mutating=True):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")

        def remove() -> None:
            conn = db.connect(tenant.db_path)
            try:
                feeds.delete_feed(conn, name)
            finally:
                conn.close()

        try:
            await run_in_threadpool(remove)
        except feeds.FeedError as e:
            return refuse(422, str(e))
        return Response(status_code=204, headers=NO_STORE)

    @app.get(PREFIX + "/tenants/{slug}/heartbeats")
    async def get_tenant_heartbeats(request: Request, slug: str) -> Response:
        if denied := guard(request, mutating=False):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")

        def rows() -> list[dict[str, Any]]:
            conn = db.connect(tenant.db_path)
            try:
                return heartbeats.list_heartbeats(conn)
            finally:
                conn.close()

        return JSONResponse(await run_in_threadpool(rows), headers=NO_STORE)

    @app.put(PREFIX + "/tenants/{slug}/heartbeats/{name}")
    async def put_tenant_heartbeat(request: Request, slug: str, name: str) -> Response:
        if denied := guard(request, mutating=True):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")
        spec = await body_of(request)
        if spec is None or set(spec) - {"every", "source", "tag"}:
            return refuse(422, "body takes every, and one of source or tag")

        def save() -> dict[str, Any]:
            conn = db.connect(tenant.db_path)
            try:
                return heartbeats.save_heartbeat(
                    conn, name, clock(), spec.get("every"), spec.get("source"), spec.get("tag")
                )
            finally:
                conn.close()

        try:
            saved = await run_in_threadpool(save)
        except heartbeats.HeartbeatError as e:
            return refuse(422, str(e))
        return JSONResponse(saved, headers=NO_STORE)

    @app.delete(PREFIX + "/tenants/{slug}/heartbeats/{name}")
    async def delete_tenant_heartbeat(request: Request, slug: str, name: str) -> Response:
        if denied := guard(request, mutating=True):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")

        def remove() -> None:
            conn = db.connect(tenant.db_path)
            try:
                heartbeats.delete_heartbeat(conn, name, clock())
            finally:
                conn.close()

        await run_in_threadpool(remove)
        return Response(status_code=204, headers=NO_STORE)

    @app.post(PREFIX + "/tenants/{slug}/rename")
    async def rename_tenant(request: Request, slug: str) -> Response:
        if denied := guard(request, mutating=True):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")
        spec = await body_of(request)
        new = spec.get("to") if spec else None
        if not isinstance(new, str) or not SLUG.fullmatch(new) or new in RESERVED:
            return refuse(422, "to must be a valid, unreserved tenant name")
        if known(new) is not None or (tenancy.root / new).exists():
            return refuse(409, "that name is taken")
        try:
            moved = await run_in_threadpool(tenancy.rename, slug, new)
        except TenancyError as e:
            return refuse(409, str(e))
        await run_in_threadpool(
            log_event, moved, "tenant.rename", f"Username changed from {slug} to {new}", old=slug
        )
        return JSONResponse({"slug": new}, headers=NO_STORE)

    def lifecycle_route(action: str) -> None:
        @app.post(PREFIX + "/tenants/{slug}/" + action)
        async def lifecycle(request: Request, slug: str) -> Response:
            if denied := guard(request, mutating=True):
                return denied
            if known(slug) is None:
                return refuse(404, "no such tenant")
            await run_in_threadpool(lambda: tenancy.update(slug, suspended=action == "suspend"))
            return Response(status_code=204, headers=NO_STORE)

    for name in ("suspend", "resume"):
        lifecycle_route(name)

    def mint(tenant: Tenant, spec: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        kind, labels = spec.get("kind"), spec.get("labels", ["self"])
        expires_in, note = spec.get("expires_in"), spec.get("note")
        source = spec.get("write_source")
        if kind not in ("read", "write"):
            return 422, {"error": "kind must be read or write"}
        if (
            not isinstance(labels, list)
            or not labels
            or not all(isinstance(x, str) and LABEL.match(x) for x in labels)
        ):
            return 422, {"error": "labels must be names like self or family"}
        if kind == "write" and not (isinstance(source, str) and source.strip()):
            return 422, {"error": "a write token needs a write_source"}
        if expires_in is not None and not (
            isinstance(expires_in, int)
            and not isinstance(expires_in, bool)
            and MIN_EXPIRY <= expires_in <= MAX_EXPIRY
        ):
            return 422, {"error": "expires_in must be 60 to 31622400 seconds"}
        if note is not None and not (isinstance(note, str) and len(note) <= 80):
            return 422, {"error": "note must be text of at most 80 characters"}
        short = expires_in is not None and expires_in <= SHORT_LIVED
        conn = db.connect(tenant.db_path)
        try:
            now = clock()
            if not short and _live_tokens(conn, now) >= MAX_LIVE_TOKENS:
                return 409, {"error": f"at most {MAX_LIVE_TOKENS} live tokens"}
            issued = create_token(
                conn,
                labels,
                expires_at=timefmt.plus(now, expires_in) if expires_in else None,
                note=note,
                now=now,
                write_sources=[source] if kind == "write" else None,
            )
        finally:
            conn.close()
        log.info("token created: tenant=%s id=%s kind=%s", tenant.id, issued.id, kind)
        if not short:
            log_event(
                tenant, "token.create", f"Token {issued.id} created ({kind})", token=issued.id
            )
        return 201, {"id": issued.id, "token": issued.value, "kind": kind}

    @app.post(PREFIX + "/tenants/{slug}/tokens")
    async def create_tenant_token(request: Request, slug: str) -> Response:
        if denied := guard(request, mutating=True):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")
        spec = await body_of(request)
        if spec is None:
            return refuse(422, "body must be a JSON object")
        status, out = await run_in_threadpool(mint, tenant, spec)
        return JSONResponse(out, status_code=status, headers=NO_STORE)

    @app.get(PREFIX + "/tenants/{slug}/tokens")
    async def get_tenant_tokens(request: Request, slug: str) -> Response:
        if denied := guard(request, mutating=False):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")

        def rows() -> list[dict[str, Any]]:
            conn = db.connect(tenant.db_path)
            try:
                return [
                    {**t, "kind": "write" if t["write_sources"] else "read"}
                    for t in list_tokens(conn)
                ]
            finally:
                conn.close()

        return JSONResponse(await run_in_threadpool(rows), headers=NO_STORE)

    @app.delete(PREFIX + "/tenants/{slug}/tokens/{token_id}")
    async def revoke_tenant_token(request: Request, slug: str, token_id: str) -> Response:
        if denied := guard(request, mutating=True):
            return denied
        tenant = known(slug)
        if tenant is None:
            return refuse(404, "no such tenant")

        def revoke() -> bool:
            conn = db.connect(tenant.db_path)
            try:
                return revoke_token(conn, token_id, clock())
            finally:
                conn.close()

        if await run_in_threadpool(revoke):
            log.info("token revoked: tenant=%s id=%s", tenant.id, token_id)
            await run_in_threadpool(
                log_event, tenant, "token.revoke", f"Token {token_id} revoked", token=token_id
            )
        return Response(status_code=204, headers=NO_STORE)


def _live_tokens(conn: Any, now: str) -> int:
    """Count tokens that are neither revoked nor expired and last longer than a session."""
    return conn.execute(
        "SELECT count(*) FROM tokens WHERE revoked_at IS NULL"
        " AND (expires_at IS NULL OR (expires_at > ? AND expires_at > ?))",
        (now, timefmt.plus(now, SHORT_LIVED)),
    ).fetchone()[0]
