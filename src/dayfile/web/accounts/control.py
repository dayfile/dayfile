"""The web app's only way to run tenants: the core's admin API, over the internal network.

Every call needs the shared secret (a file), uses a fixed internal address, refuses redirects and
caps the response. Tenant names are validated before they reach a URL.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dayfile.web.client import MAX_BODY, ApiResult, _NoRedirect

Send = Callable[[str, str, Any], ApiResult]
EXPORT_MAX = 64 * 1024 * 1024  # the plan caps a log at 25 MiB; this leaves room
PLAN = {"max_events": 50_000, "max_bytes": 25 * 1024 * 1024, "write_limit": [60, 60]}  # free beta


def http_send(api_url: str, secret: str, timeout: float = 10.0) -> Send:
    """Send admin requests to `api_url` with the shared secret."""
    root = api_url.rstrip("/")
    opener = urllib.request.build_opener(_NoRedirect)

    def send(method: str, path: str, body: Any = None) -> ApiResult:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            root + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                if response.headers.get_content_type() == "application/gzip":
                    raw = response.read(EXPORT_MAX + 1)  # a downloaded copy, kept as bytes
                    return (
                        ApiResult(response.status, raw) if len(raw) <= EXPORT_MAX else ApiResult(0)
                    )
                status, raw = response.status, response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(MAX_BODY + 1)
        except (urllib.error.URLError, OSError):
            return ApiResult(0)
        if len(raw) > MAX_BODY:
            return ApiResult(0)
        try:
            return ApiResult(status, json.loads(raw) if raw else None)
        except ValueError:
            return ApiResult(status)

    return send


def from_environment(api_url: str, secret_file: str | None) -> Send | None:
    """Build the sender from the secret file, or None when there is no usable secret."""
    try:
        secret = Path(secret_file).read_text().strip() if secret_file else ""
    except OSError:
        secret = ""
    return http_send(api_url, secret) if secret else None


class Control:
    """Typed calls to the admin API for one tenant at a time."""

    def __init__(self, send: Send) -> None:
        self._send = send

    @staticmethod
    def _path(slug: str, *rest: str) -> str:
        parts = [urllib.parse.quote(p, safe="") for p in (slug, *rest)]
        return "/internal/admin/tenants/" + "/".join(parts)

    def create_tenant(self, slug: str) -> ApiResult:
        """Make the tenant with the plan's limits (idempotent)."""
        return self._send("PUT", self._path(slug), dict(PLAN))

    def rename(self, slug: str, new: str) -> ApiResult:
        """Move the tenant to a new name."""
        return self._send("POST", self._path(slug, "rename"), {"to": new})

    def tenant(self, slug: str) -> ApiResult:
        """Usage and limits."""
        return self._send("GET", self._path(slug), None)

    def export(self, slug: str) -> ApiResult:
        """A gzip of a consistent copy of the log's database (the body is bytes)."""
        return self._send("GET", self._path(slug, "export"), None)

    def delete_tenant(self, slug: str) -> ApiResult:
        """Erase the log's files at once; there is no trash copy and no way back."""
        return self._send("DELETE", self._path(slug) + "?erase=true", None)

    def reset(self, slug: str) -> ApiResult:
        """Erase the log and make it again, empty, with the plan's limits (a retry is safe)."""
        gone = self.delete_tenant(slug)
        if gone.status not in (204, 404):  # 404: an earlier try already erased it
            return gone
        return self.create_tenant(slug)

    def tokens(self, slug: str) -> ApiResult:
        """Every token, never its secret."""
        return self._send("GET", self._path(slug, "tokens"), None)

    def mint(self, slug: str, spec: dict[str, Any]) -> ApiResult:
        """Mint a token; the secret is in the answer once."""
        return self._send("POST", self._path(slug, "tokens"), spec)

    def revoke(self, slug: str, token_id: str) -> ApiResult:
        """Revoke a token."""
        return self._send("DELETE", self._path(slug, "tokens", token_id), None)

    def feeds(self, slug: str) -> ApiResult:
        """The default feed and every named feed."""
        return self._send("GET", self._path(slug, "feeds"), None)

    def save_feed(self, slug: str, name: str, spec: dict[str, Any]) -> ApiResult:
        """Create a feed or change the fields given."""
        return self._send("PUT", self._path(slug, "feeds", name), spec)

    def heartbeats(self, slug: str) -> ApiResult:
        """Every heartbeat."""
        return self._send("GET", self._path(slug, "heartbeats"), None)

    def save_heartbeat(self, slug: str, name: str, spec: dict[str, Any]) -> ApiResult:
        """Create or replace a heartbeat."""
        return self._send("PUT", self._path(slug, "heartbeats", name), spec)

    def delete_heartbeat(self, slug: str, name: str) -> ApiResult:
        """Remove a heartbeat."""
        return self._send("DELETE", self._path(slug, "heartbeats", name), None)

    def delete_feed(self, slug: str, name: str) -> ApiResult:
        """Remove a feed."""
        return self._send("DELETE", self._path(slug, "feeds", name), None)
