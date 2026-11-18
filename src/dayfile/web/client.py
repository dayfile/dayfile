"""How the web app reaches the dayfile API: one fixed address, a validated tenant name."""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

MAX_BODY = 4 * 1024 * 1024


@dataclass(frozen=True)
class ApiResult:
    """What the API answered; `status` 0 means it could not be reached or answered badly."""

    status: int
    body: Any = None


class Fetch(Protocol):
    """Reads `GET /events` for one tenant with its read token."""

    def __call__(self, slug: str, token: str, params: list[tuple[str, str]]) -> ApiResult:
        """Return the API's answer, never raising for a network or HTTP failure."""


class Post(Protocol):
    """Writes one event to a tenant with its write token."""

    def __call__(self, slug: str, token: str, event: dict[str, Any]) -> ApiResult:
        """Return the API's answer, never raising for a network or HTTP failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect is an answer, not somewhere to go next


def http_fetch(api_url: str, base_domain: str, timeout: float = 10.0) -> Fetch:
    """Build a fetcher that sends every request to `api_url`, naming the tenant by Host header.

    The address never comes from a person: only the tenant name does, and it has already been
    validated, so a request cannot be steered to another host.
    """
    root = api_url.rstrip("/")
    opener = urllib.request.build_opener(_NoRedirect)

    def fetch(slug: str, token: str, params: list[tuple[str, str]]) -> ApiResult:
        request = urllib.request.Request(
            f"{root}/events?{urllib.parse.urlencode(params)}",
            headers={
                "Host": f"{slug}.{base_domain}",
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                status, raw = response.status, response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(MAX_BODY + 1)
        except (urllib.error.URLError, OSError):
            return ApiResult(0)
        if len(raw) > MAX_BODY:
            return ApiResult(0)
        try:
            return ApiResult(status, json.loads(raw))
        except ValueError:
            return ApiResult(status)

    return fetch


def http_post(api_url: str, base_domain: str, timeout: float = 10.0) -> Post:
    """Build a poster for `POST /events`, addressed the same fixed way as `http_fetch`."""
    root = api_url.rstrip("/")
    opener = urllib.request.build_opener(_NoRedirect)

    def post(slug: str, token: str, event: dict[str, Any]) -> ApiResult:
        request = urllib.request.Request(
            f"{root}/events",
            data=json.dumps(event).encode(),
            method="POST",
            headers={
                "Host": f"{slug}.{base_domain}",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                status, raw = response.status, response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(MAX_BODY + 1)
        except (urllib.error.URLError, OSError):
            return ApiResult(0)
        try:
            return ApiResult(status, json.loads(raw[:MAX_BODY]))
        except ValueError:
            return ApiResult(status)

    return post


def http_status(api_url: str, timeout: float = 5.0) -> Callable[[], str]:
    """Build a probe of the API's `/status` that returns "ok", "unavailable" or "unreachable"."""
    url = api_url.rstrip("/") + "/status"
    opener = urllib.request.build_opener(_NoRedirect)

    def probe() -> str:
        try:
            with opener.open(urllib.request.Request(url), timeout=timeout) as response:
                body = json.loads(response.read(MAX_BODY))
        except urllib.error.HTTPError:
            return "unavailable"
        except (urllib.error.URLError, OSError, ValueError):
            return "unreachable"
        return "ok" if isinstance(body, dict) and body.get("status") == "ok" else "unavailable"

    return probe
