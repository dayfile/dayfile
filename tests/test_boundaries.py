"""Package boundaries: the core never depends on the web app, and the web app stays off storage."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "dayfile"
WEB = SRC / "web"
# What the web app may import from the core: constants and pure helpers, never storage or routes.
WEB_MAY_USE = {
    "dayfile.icons",
    "dayfile.ratelimit",
    "dayfile.tenancy",
    "dayfile.timefmt",
    "dayfile.ulid",
}
WEB_ONLY_LIBS = {"jinja2", "multipart", "python_multipart"}


def imports(path: Path) -> set[str]:
    """Return every module the file imports, as dotted names."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def core_files():
    return [p for p in SRC.rglob("*.py") if WEB not in p.parents]


def test_core_does_not_import_the_web_app_or_its_libraries():
    for path in core_files():
        bad = {
            m
            for m in imports(path)
            if m == "dayfile.web"
            or m.startswith("dayfile.web.")
            or m.split(".")[0] in WEB_ONLY_LIBS
        }
        assert not bad, f"{path.relative_to(SRC)} imports {sorted(bad)}"


def test_web_app_imports_only_allowed_core_modules():
    for path in WEB.rglob("*.py"):
        for m in imports(path):
            if m.startswith("dayfile.") and not m.startswith("dayfile.web"):
                assert any(m == a or m.startswith(a + ".") for a in WEB_MAY_USE), (
                    f"{path.relative_to(SRC)} imports {m}"
                )


def test_source_does_not_import_httpx():
    """httpx is a test-only dependency; the production image does not have it."""
    import pathlib

    for path in pathlib.Path("src").rglob("*.py"):
        text = path.read_text()
        assert "import httpx" not in text and "from httpx" not in text, path
