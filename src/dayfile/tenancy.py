"""Tenancy: which database serves a request, and what limits apply to it."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dayfile import db, timefmt

SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
RESERVED = frozenset({"www", "api", "app", "admin", "mail", "status", "dashboard", "docs"})
DB_NAME = "dayfile.db"
META_NAME = "tenant.json"
TRASH = ".trash"


class TenancyError(ValueError):
    """A tenant operation was refused (bad name, unknown tenant, invalid limit)."""


@dataclass(frozen=True)
class Tenant:
    """One customer's database and limits; `None` limits mean the app-wide defaults."""

    id: str
    db_path: Path
    suspended: bool = False
    write_limit: tuple[int, int] | None = None  # (events, seconds) per write token
    max_events: int | None = None  # total events the log may hold
    max_bytes: int | None = None  # database size at which new events are refused


class Tenancy(Protocol):
    """Maps a request's Host header to a tenant."""

    def resolve(self, host: str) -> Tenant | None:
        """Return the tenant for `host`, or None if there is none."""

    def healthy(self) -> bool:
        """True if the backing store can be reached."""


class SingleTenant:
    """The self-hosted default: every host is served by one database."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = db_path

    def resolve(self, host: str) -> Tenant:
        """Return the one tenant; the path is read late so $DAYFILE_DB can change."""
        return Tenant("default", Path(self._path or os.environ.get("DAYFILE_DB", "dayfile.db")))

    def healthy(self) -> bool:
        """True if the database opens and answers a query."""
        conn = db.connect(self.resolve("").db_path)
        try:
            conn.execute("SELECT 1 FROM events LIMIT 1").fetchall()
        finally:
            conn.close()
        return True


class DirectoryTenancy:
    """One directory per tenant under `root`, named by the subdomain of `base_domain`.

    `acme.example.com` is served by `<root>/acme/dayfile.db`, with limits in
    `<root>/acme/tenant.json`. Tenants are created only through `create`, never by a request.
    """

    def __init__(self, root: str | Path, base_domain: str) -> None:
        self.root = Path(root)
        self.base_domain = base_domain.strip(".").lower()

    def _slug(self, host: str) -> str | None:
        name = host.split(":", 1)[0].lower()
        suffix = "." + self.base_domain
        slug = name[: -len(suffix)] if name.endswith(suffix) else ""
        return slug if SLUG.match(slug) else None

    def resolve(self, host: str) -> Tenant | None:
        """Return the tenant for a Host header, or None for unknown names."""
        slug = self._slug(host)
        return self._load(slug) if slug else None

    def get(self, slug: str) -> Tenant | None:
        """Return a tenant by name rather than by host."""
        return self._load(slug) if SLUG.match(slug) else None

    def healthy(self) -> bool:
        """True if the tenant root is a readable directory."""
        return self.root.is_dir() and os.access(self.root, os.R_OK | os.X_OK)

    def _load(self, slug: str) -> Tenant | None:
        path = self.root / slug / DB_NAME
        if slug in RESERVED or not path.is_file():
            return None
        try:
            meta = _read_meta(self.root / slug)
        except (ValueError, OSError):
            return Tenant(slug, path, suspended=True)  # unreadable limits fail closed
        return Tenant(
            slug,
            path,
            suspended=meta["suspended"],
            write_limit=meta["write_limit"],
            max_events=meta["max_events"],
            max_bytes=meta["max_bytes"],
        )

    def create(self, slug: str, **limits: Any) -> Tenant:
        """Make a tenant directory with an empty database; refuses bad or taken names."""
        if not SLUG.match(slug) or slug in RESERVED:
            raise TenancyError(f"invalid tenant name {slug!r}")
        directory = self.root / slug
        if directory.exists():
            raise TenancyError(f"tenant {slug!r} exists")
        _check_limits(limits)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700)
        db.connect(directory / DB_NAME).close()
        _write_meta(directory, {**_read_meta(directory), **limits})
        tenant = self._load(slug)
        assert tenant is not None
        return tenant

    def update(self, slug: str, **changes: Any) -> Tenant:
        """Change a tenant's suspension or limits; the data is never touched."""
        if self._load(slug) is None:
            raise TenancyError(f"no such tenant {slug!r}")
        _check_limits(changes)
        directory = self.root / slug
        try:
            current = _read_meta(directory)
        except (ValueError, OSError):
            current = _read_meta(directory, missing_ok=True)  # an update repairs a bad file
        _write_meta(directory, {**current, **changes})
        tenant = self._load(slug)
        assert tenant is not None
        return tenant

    def rename(self, slug: str, new: str) -> Tenant:
        """Move a tenant to a new name; refuses bad, reserved or taken names. Data is untouched."""
        if self._load(slug) is None:
            raise TenancyError(f"no such tenant {slug!r}")
        if not SLUG.match(new) or new in RESERVED:
            raise TenancyError(f"invalid tenant name {new!r}")
        target = self.root / new
        if target.exists():
            raise TenancyError(f"tenant {new!r} exists")
        (self.root / slug).rename(target)  # one atomic step on the same filesystem
        tenant = self._load(new)
        assert tenant is not None
        return tenant

    def list(self) -> list[str]:
        """Names of all live tenants, sorted."""
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if SLUG.match(p.name) and (p / DB_NAME).is_file()
        )

    def usage(self, slug: str) -> dict[str, int]:
        """Event count and bytes on disk (database plus its WAL files)."""
        tenant = self._load(slug)
        if tenant is None:
            raise TenancyError(f"no such tenant {slug!r}")
        conn = db.connect(tenant.db_path)
        try:
            events = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        finally:
            conn.close()
        return {"events": events, "bytes": stored_bytes(tenant)}

    def delete(self, slug: str) -> Path:
        """Move a tenant to `<root>/.trash/` (it frees the name; purging is a separate act)."""
        if self._load(slug) is None:
            raise TenancyError(f"no such tenant {slug!r}")
        stamp = timefmt.now().replace(":", "")
        target = self.root / TRASH / f"{slug}-{stamp}"
        target.parent.mkdir(mode=0o700, exist_ok=True)
        (self.root / slug).rename(target)
        return target

    def erase(self, slug: str) -> None:
        """Remove a tenant's files for good, with no trash copy (the account-deletion path)."""
        import shutil

        if self._load(slug) is None:
            raise TenancyError(f"no such tenant {slug!r}")
        shutil.rmtree(self.root / slug)


def purge_trash(root: str | Path, older_than_days: int, now: str | None = None) -> list[str]:
    """Remove deleted tenants held in `<root>/.trash/` for at least `older_than_days`; return names.

    The age is read from the stamp in the directory name, so touching files cannot extend it.
    """
    import shutil

    trash = Path(root) / TRASH
    cutoff = timefmt.minus(now or timefmt.now(), older_than_days * 86400)
    gone: list[str] = []
    for entry in sorted(trash.iterdir()) if trash.is_dir() else []:
        found = re.search(r"-(\d{4}-\d\d-\d\dT)(\d\d)(\d\d)(\d\d)Z$", entry.name)
        if not found:
            continue  # not something we put there
        deleted_at = f"{found.group(1)}{found.group(2)}:{found.group(3)}:{found.group(4)}Z"
        if entry.is_dir() and deleted_at <= cutoff:
            shutil.rmtree(entry)
            gone.append(entry.name)
    return gone


def stored_bytes(tenant: Tenant) -> int:
    """Bytes on disk for the tenant's database and its WAL files."""
    return sum(p.stat().st_size for p in tenant.db_path.parent.glob(DB_NAME + "*"))


def count_events(tenant: Tenant) -> int:
    """How many events the tenant's log holds (seq is gapless because rows never go away)."""
    conn = db.connect(tenant.db_path)
    try:
        return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
    finally:
        conn.close()


def from_environment(db_path: str | Path | None) -> Tenancy:
    """Choose the tenancy: an explicit `db_path` wins, then $DAYFILE_TENANTS_DIR, else single."""
    root = os.environ.get("DAYFILE_TENANTS_DIR")
    if db_path is None and root:
        domain = os.environ.get("DAYFILE_BASE_DOMAIN")
        if not domain:
            raise ValueError("DAYFILE_TENANTS_DIR requires DAYFILE_BASE_DOMAIN")
        return DirectoryTenancy(root, domain)
    return SingleTenant(db_path)


_DEFAULTS: dict[str, Any] = {
    "suspended": False,
    "write_limit": None,
    "max_events": None,
    "max_bytes": None,
}


def _check_limits(values: dict[str, Any]) -> None:
    unknown = set(values) - set(_DEFAULTS)
    if unknown:
        raise TenancyError(f"unknown setting {sorted(unknown)[0]!r}")
    if "suspended" in values and not isinstance(values["suspended"], bool):
        raise TenancyError("suspended must be true or false")
    limit = values.get("write_limit")
    if limit is not None and not (
        len(limit) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in limit)
    ):
        raise TenancyError("write_limit must be two positive integers (events, seconds)")
    cap = values.get("max_events")
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 0):
        raise TenancyError("max_events must be a non-negative integer")
    size = values.get("max_bytes")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
        raise TenancyError("max_bytes must be a non-negative integer")


def _read_meta(directory: Path, missing_ok: bool = False) -> dict[str, Any]:
    path = directory / META_NAME
    if not path.exists():
        return dict(_DEFAULTS)
    if missing_ok:
        return dict(_DEFAULTS)
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("tenant.json must be an object")
    meta = {**_DEFAULTS, **data}
    try:
        _check_limits(meta)
    except TenancyError as e:
        raise ValueError(str(e)) from e
    if meta["write_limit"] is not None:
        meta["write_limit"] = (meta["write_limit"][0], meta["write_limit"][1])
    return meta


def _write_meta(directory: Path, meta: dict[str, Any]) -> None:
    """Write tenant.json atomically so a reader never sees half a file."""
    path = directory / META_NAME
    tmp = directory / (META_NAME + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def copy_database(src: Path, dest: Path) -> None:
    """Copy a live database with SQLite's online backup and verify the copy (mode 600)."""
    import sqlite3

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    tmp.unlink(missing_ok=True)
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("integrity check failed on the copy")
        finally:
            source.close()
            target.close()
        tmp.chmod(0o600)  # the log is private data
        tmp.rename(dest)
    except sqlite3.Error:
        tmp.unlink(missing_ok=True)
        raise
