"""Reading a log name and a read token out of what a person typed or pasted."""

import re
from urllib.parse import urlsplit

from dayfile.tenancy import SLUG

TOKEN = re.compile(r"[0-9a-f]{12}\.[A-Za-z0-9_-]{20,64}")
FEED_PATH = re.compile(r"/t/([^/]+)/feed\.(?:json|atom)")


def parse_credentials(slug: str, token: str, base_domain: str) -> tuple[str, str] | None:
    """Return (log name, token) from the form fields, or None if either is not well formed.

    The token field may hold a whole feed URL (`https://<name>.<base>/t/<token>/feed.json`), which
    also names the log. Nothing returned here can contain a character that would change which host
    or path the web app later asks for.
    """
    slug, token, suffix = slug.strip().lower(), token.strip(), "." + base_domain.lower()
    if "/" in token or ":" in token:
        parsed = _feed_url(token, suffix)
        if parsed is None:
            return None
        url_slug, token = parsed
        if slug and slug.removesuffix(suffix) != url_slug:
            return None
        slug = url_slug
    slug = slug.removesuffix(suffix)
    if not SLUG.fullmatch(slug) or not TOKEN.fullmatch(token):
        return None
    return slug, token


def _feed_url(text: str, suffix: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.netloc.lower() != host:  # no userinfo, no port
        return None
    path = FEED_PATH.fullmatch(parts.path)
    if not host.endswith(suffix) or not path:
        return None
    return host.removesuffix(suffix), path.group(1)


SINGLE_LOG = "log"  # the one name a self-hosted server's only log goes by inside the web app
FEED_ANY = re.compile(r"/t/([^/]+)/(?:f/[^/]+\.|feed\.)(?:json|atom)")


def parse_single_token(text: str) -> tuple[str, str] | None:
    """Return (the fixed log name, token) from a bare token or any pasted feed URL, else None."""
    text = text.strip()
    if "/" in text or ":" in text:
        try:
            parts = urlsplit(text)
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or parts.username or parts.password:
            return None
        match = FEED_ANY.fullmatch(parts.path)
        text = match.group(1) if match else ""
    return (SINGLE_LOG, text) if TOKEN.fullmatch(text) else None
