"""Shared test helpers."""

import json
from urllib.parse import unquote

from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import create_token
from dayfile.db import connect

ALL_LABELS = ["self", "family", "public", "friends", "ops"]


def mint_write(db_path, sources, labels=ALL_LABELS):
    """Mint a write token for `sources` and return its value."""
    conn = connect(db_path)
    try:
        return create_token(conn, list(labels), write_sources=list(sources)).value
    finally:
        conn.close()


class DualClient(TestClient):
    """A TestClient that sends a read token on GET and a write token on POST /events.

    The write token is minted on demand for exactly the source ids found in the body, with
    every common label, so tests need not care which source they post as.
    """

    db_path = None
    read_auth: str | None = None
    _writers: dict[frozenset, str]

    def _write_token_for(self, body):
        sources = _sources(body)
        if sources not in self._writers:
            self._writers[sources] = mint_write(self.db_path, sources)
        return self._writers[sources]

    def request(self, method, url, **kwargs):  # type: ignore[override]
        headers = dict(kwargs.pop("headers", None) or {})
        if not any(k.lower() == "authorization" for k in headers):
            token = self.read_auth
            if method.upper() == "POST" and url.startswith("/events"):
                try:
                    body = kwargs.get("json")
                    if body is None:
                        body = json.loads(kwargs.get("content") or "{}")
                except ValueError:
                    body = {}  # deliberately malformed request bodies still need a credential
                token = self._write_token_for(body)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return super().request(method, url, headers=headers, **kwargs)


def _sources(body):
    items = body if isinstance(body, list) else [body]
    found = set()
    for x in items:
        src = x.get("source") if isinstance(x, dict) else None
        if isinstance(src, dict) and isinstance(src.get("id"), str):
            found.add(src["id"])
        elif isinstance(src, str):
            found.add(unquote(src))  # CloudEvents form
    return frozenset(found) or frozenset({"unknown"})


def authed_client(db_path, clock=None, labels=("self",)):
    """Return a client with a read token (given labels) and automatic write tokens."""
    app = create_app(db_path, clock=clock) if clock else create_app(db_path)
    conn = connect(db_path)
    client = DualClient(app)
    client.db_path = db_path
    client._writers = {}
    client.read_auth = create_token(conn, list(labels)).value
    conn.close()
    return client
