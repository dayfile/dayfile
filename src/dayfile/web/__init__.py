"""The dayfile.app web app."""

from dayfile.web.app import create_web_app

app = create_web_app()

__all__ = ["app", "create_web_app"]
