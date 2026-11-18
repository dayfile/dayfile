"""The dayfile icon files, shared by the API and the web app."""

from pathlib import Path

STATIC = Path(__file__).parent / "static"
ICONS = {
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/icon-64.png": ("icon-64.png", "image/png"),
}
