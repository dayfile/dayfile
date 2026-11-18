"""Log filter that keeps URL-path tokens out of log output."""

import logging
import re
from typing import Any

TOKEN_PATH = re.compile(r"(/t/)[^/\s?#\"']+")


def redact(text: str) -> str:
    """Replace the token segment of `/t/<token>/...` paths."""
    return TOKEN_PATH.sub(r"\1[redacted]", text)


class RedactTokens(logging.Filter):
    """Rewrite message and arguments of each record so `/t/<token>/` never reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact in place and always keep the record."""
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._clean(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._clean(v) for k, v in record.args.items()}
        return True

    @staticmethod
    def _clean(value: Any) -> Any:
        return redact(value) if isinstance(value, str) else value


def install(*logger_names: str) -> None:
    """Attach the filter once to each named logger."""
    for name in logger_names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokens) for f in logger.filters):
            logger.addFilter(RedactTokens())
