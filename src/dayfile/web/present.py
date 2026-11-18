"""Turning stored events into things that are safe to show a person."""

import html
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from markupsafe import Markup

SAFE_SCHEMES = frozenset({"http", "https", "mailto"})
STAMP = re.compile(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d):\d\dZ")


def safe_href(href: Any) -> str | None:
    """Return `href` if it is a plain web or mail link, else None (text only, never a link)."""
    if not isinstance(href, str) or not href or len(href) > 2000:
        return None
    if any(ord(c) <= 32 or ord(c) == 127 for c in href):
        return None
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in SAFE_SCHEMES:
        return None
    if scheme != "mailto" and not parts.hostname:
        return None
    if scheme == "mailto" and not parts.path:
        return None
    return href


def stamp(value: Any) -> str:
    """Show a stored UTC timestamp as `2026-11-10 00:12 UTC`; anything else is shown as it is."""
    match = STAMP.fullmatch(value) if isinstance(value, str) else None
    return f"{match.group(1)} {match.group(2)} UTC" if match else str(value)


def payload_text(payload: Any, limit: int = 2000) -> str:
    """Pretty JSON for a payload, cut at `limit` characters."""
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "\n…"


def ago(value: Any, now: str) -> str:
    """Show a stored UTC timestamp relative to `now`: `3 h ago`, `yesterday`, or its date."""
    then = STAMP.fullmatch(value) if isinstance(value, str) else None
    if not then or not STAMP.fullmatch(now):
        return str(value)
    seconds = (_parse(now) - _parse(value)).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 172800:
        return "yesterday"
    if seconds < 30 * 86400:
        return f"{int(seconds // 86400)} d ago"
    return value[:10]


def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(
    r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])"
)
_ITEM = re.compile(r"(?:[-*+]|\d+\.)\s+(.*)")


def _inline(text: str) -> str:
    """Escape a run of text, then apply code, links, bold and italic; links must be web or mail."""
    safe = html.escape(text, quote=True)
    stash: list[str] = []

    def keep(markup: str) -> str:
        stash.append(markup)
        return f"\x00{len(stash) - 1}\x00"

    safe = _CODE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), safe)

    def link(m: re.Match[str]) -> str:
        href = safe_href(html.unescape(m.group(2)))
        if href is None:
            return m.group(0)
        return keep(
            f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer nofollow">'
            f"{m.group(1)}</a>"
        )

    safe = _LINK.sub(link, safe)
    safe = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", safe)
    safe = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", safe)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], safe)


def _kind(line: str) -> str:
    if re.fullmatch(r"#{1,3}\s+.*", line):
        return "heading"
    if line.startswith(">"):
        return "quote"
    if re.fullmatch(r"\d+\.\s+.*", line):
        return "ol"
    if re.fullmatch(r"[-*+]\s+.*", line):
        return "ul"
    return "text"


def _blocks(lines: list[str]) -> list[str]:
    """Group consecutive lines of one kind into a block, so a heading can sit right above text."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        kind = _kind(lines[i])
        j = i + 1
        while kind != "heading" and j < len(lines) and _kind(lines[j]) == kind:
            j += 1
        group = lines[i:j]
        i = j
        if kind == "heading":
            level = len(group[0]) - len(group[0].lstrip("#")) + 2  # h3 to h5: the card title is h3
            out.append(f"<h{level}>{_inline(group[0].lstrip('#').strip())}</h{level}>")
        elif kind == "quote":
            quoted = " ".join(line.lstrip(">").strip() for line in group)
            out.append(f"<blockquote>{_inline(quoted)}</blockquote>")
        elif kind in ("ul", "ol"):
            items = "".join(f"<li>{_inline(_ITEM.fullmatch(line).group(1))}</li>" for line in group)
            out.append(f"<{kind}>{items}</{kind}>")
        else:
            out.append("<p>" + "<br>".join(_inline(line) for line in group) + "</p>")
    return out


def markdown(text: Any) -> Markup:
    """Render a small, safe subset of Markdown: paragraphs, headings, lists, quotes, code, links.

    Everything is HTML-escaped first, so nothing a sender writes can add markup or script, and a
    link is made only for http, https or mailto addresses.
    """
    source = str(text or "").replace("\r\n", "\n")
    out: list[str] = []
    for index, part in enumerate(
        re.split(r"^```[^\n]*\n(.*?)^```[ \t]*$", source, flags=re.S | re.M)
    ):
        if index % 2:  # the inside of a fenced code block
            out.append(f"<pre><code>{html.escape(part.rstrip(chr(10)))}</code></pre>")
            continue
        for chunk in re.split(r"\n\s*\n", part.strip("\n")):
            lines = [line.rstrip() for line in chunk.split("\n") if line.strip()]
            out.extend(_blocks(lines))
    return Markup("".join(out))
