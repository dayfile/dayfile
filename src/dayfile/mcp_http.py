"""Hosted MCP endpoint: `/mcp` on every log's own address, so an assistant can connect by URL.

The caller's credential is a read token, optionally followed by `~` and a write token (reads need
the first, writes the second; a read-only connection has no second part). Tool calls run inside
this same app, as that caller, so every limit and permission of the ordinary API applies.
Sign-in happens at the authorization server named in `issuer` (dayfile.app), which hands out
such credentials; without an issuer only hand-made credentials work.
"""

import contextvars
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from dayfile import mcp_server

SEPARATOR = "~"  # not in a token's alphabet (hex, dot, base64url)
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "testserver")


@dataclass(frozen=True)
class Credentials:
    """The caller's tokens and the log's host, for the request being served."""

    read: str
    write: str | None
    host: str


CREDENTIALS: contextvars.ContextVar[Credentials | None] = contextvars.ContextVar(
    "dayfile_mcp_credentials", default=None
)


def split_credential(value: str) -> tuple[str, str | None]:
    """Split `read~write` into its parts; the write part is None for a read-only credential."""
    read, _, write = value.partition(SEPARATOR)
    return read, (write or None)


def origin_of(host: str) -> str:
    """The public origin of a host: https except for local development."""
    return ("http://" if host.split(":")[0] in LOCAL_HOSTS else "https://") + host


async def call_app(
    app: ASGIApp, method: str, host: str, path: str, headers: dict[str, str], body: Any
) -> tuple[int, bytes]:
    """Make one request to `app` in-process; return the status and the whole body."""
    target, _, query = path.partition("?")
    data = json.dumps(body).encode() if body is not None else b""
    sent = {**headers, "host": host, **({"content-type": "application/json"} if data else {})}
    sent["content-length"] = str(len(data))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": origin_of(host).split(":")[0],
        "path": target,
        "raw_path": target.encode(),
        "query_string": query.encode(),
        "headers": [(k.encode(), v.encode()) for k, v in sent.items()],
        "server": (host.split(":")[0], 443),
        "client": ("127.0.0.1", 0),
    }
    pending = [{"type": "http.request", "body": data, "more_body": False}]
    status, chunks = 500, []

    async def receive() -> dict[str, Any]:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status, b"".join(chunks)


def asgi_requester(app: ASGIApp) -> mcp_server.Request:
    """A requester that calls `app` in-process with the current caller's credentials."""

    async def call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
        creds = CREDENTIALS.get()
        if creds is None:
            raise mcp_server.DayfileError("not signed in")
        token = creds.read if method == "GET" else creds.write
        if token is None:
            raise mcp_server.DayfileError("this connection is read-only")
        headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
        status, raw = await call_app(app, method, creds.host, path, headers, body)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {"error": "unreadable answer"}

    return call


class Gate:
    """Checks the caller's credential, then hands the request to the MCP transport."""

    def __init__(
        self,
        inner: ASGIApp,
        resolve: Callable[[str], Any],
        authenticate: Callable[[Any, str, str], Any],
        clock: Callable[[], str],
        issuer: str | None,
    ) -> None:
        self.inner, self.resolve, self.authenticate = inner, resolve, authenticate
        self.clock, self.issuer = clock, issuer

    def challenge(self, host: str, error: str | None = None) -> Response:
        """401 whose header tells the client where to start signing in."""
        parts = ['Bearer realm="dayfile"']
        if self.issuer:
            parts.append(f'resource_metadata="{origin_of(host)}{METADATA_PATH}"')
        if error:
            parts.append(f'error="{error}"')
        return JSONResponse(
            {"error": "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": ", ".join(parts), "Cache-Control": "no-store"},
        )

    def tenant_or_refusal(self, host: str) -> Any:
        """The tenant for `host`, or the refusal to send."""
        tenant = self.resolve(host)
        if tenant is None:
            return JSONResponse({"error": "unknown tenant"}, status_code=404)
        if tenant.suspended:
            return JSONResponse({"error": "account suspended"}, status_code=403)
        return tenant

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        host = request.headers.get("host", "")
        tenant = self.tenant_or_refusal(host)
        if isinstance(tenant, Response):
            return await tenant(scope, receive, send)
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        credential = credential.strip() if scheme.lower() == "bearer" else ""
        if not credential:
            return await self.challenge(host)(scope, receive, send)
        read, write = split_credential(credential)
        principal = await run_in_threadpool(self.authenticate, tenant, read, self.clock())
        if principal is None or principal.writer_sources:
            return await self.challenge(host, "invalid_token")(scope, receive, send)
        marker = CREDENTIALS.set(Credentials(read, write, host))
        try:
            await self.inner(scope, receive, send)
        finally:
            CREDENTIALS.reset(marker)

    async def metadata(self, request: Request) -> Response:
        """RFC 9728 protected resource metadata: which server signs people in for this URL."""
        host = request.headers.get("host", "")
        tenant = self.tenant_or_refusal(host)
        if isinstance(tenant, Response):
            return tenant
        if not self.issuer:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {
                "resource": f"{origin_of(host)}/mcp",
                "authorization_servers": [self.issuer],
                "bearer_methods_supported": ["header"],
            },
            headers={"Cache-Control": "no-store"},
        )


def mount(
    app: FastAPI,
    resolve: Callable[[str], Any],
    authenticate: Callable[[Any, str, str], Any],
    clock: Callable[[], str],
    issuer: str | None,
) -> None:
    """Add `/mcp` and its discovery document to `app` (needs the `dayfile[mcp]` extra)."""
    from mcp.server.transport_security import TransportSecuritySettings

    server = mcp_server.build_server(asgi_requester(app))
    # The Host header is checked against the tenants, which is what this protection is for.
    inner = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    gate = Gate(inner, resolve, authenticate, clock, issuer)
    app.router.routes.append(Route("/mcp", gate))
    app.router.routes.append(Route(METADATA_PATH, gate.metadata, methods=["GET"]))
    started: Callable[[Any], Any] = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Any) -> Any:
        async with started(a), inner.router.lifespan_context(inner):
            yield

    app.router.lifespan_context = lifespan
