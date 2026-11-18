"""Sign in with GitHub (OAuth authorization code with PKCE); only the public id and login are read.

The GitHub access token exists for one request and is never stored or logged. Both hosts are
fixed, redirects are refused, and the response size is capped.
"""

import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
MAX_BODY = 1024 * 1024

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class GitHubError(Exception):
    """GitHub refused or did not answer; the message never contains a secret."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def urllib_transport(timeout: float = 10.0) -> Transport:
    """The real transport: urllib, no redirects, a response size cap."""
    opener = urllib.request.build_opener(_NoRedirect)

    def send(method: str, url: str, headers: dict[str, str], body: bytes | None):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read(MAX_BODY + 1)[:MAX_BODY]
        except urllib.error.HTTPError as e:
            return e.code, e.read(MAX_BODY + 1)[:MAX_BODY]
        except (urllib.error.URLError, OSError) as e:
            raise GitHubError("could not reach GitHub") from e

    return send


def challenge(verifier: str) -> str:
    """The PKCE S256 challenge for a verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class GitHubOAuth:
    """One GitHub OAuth application: builds the sign-in link and identifies the returning user."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: Transport | None = None,
    ) -> None:
        self.client_id, self._secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self._send = transport or urllib_transport()

    def authorize_url(self, state: str, verifier: str) -> str:
        """Where to send the browser. No scope is requested: public id and login only."""
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "code_challenge": challenge(verifier),
                "code_challenge_method": "S256",
                "allow_signup": "true",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    def identify(self, code: str, verifier: str) -> tuple[int, str]:
        """Trade the one-time code for the user's numeric id and login; keep nothing else."""
        form = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self._secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
            }
        ).encode()
        status, raw = self._send(
            "POST",
            TOKEN_URL,
            {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            form,
        )
        token = _json(raw).get("access_token") if status == 200 else None
        if not isinstance(token, str) or not token:
            raise GitHubError("GitHub did not accept the sign-in")
        status, raw = self._send(
            "GET",
            USER_URL,
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "dayfile-app",
            },
            None,
        )
        user = _json(raw) if status == 200 else {}
        gid, login = user.get("id"), user.get("login")
        if not isinstance(gid, int) or isinstance(gid, bool) or not isinstance(login, str):
            raise GitHubError("GitHub did not return an account")
        return gid, login[:100]


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
