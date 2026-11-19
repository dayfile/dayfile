"""Command line client: `dayfile add "title" ...` posts an event to the server."""

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from dayfile.tenancy import copy_database
from dayfile.timefmt import normalize, now
from dayfile.ulid import new_ulid

Poster = Callable[[str, list[dict[str, Any]]], tuple[int, Any]]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(prog="dayfile")
    parser.add_argument("--url", default=os.environ.get("DAYFILE_URL", "http://127.0.0.1:8000"))
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="append an event")
    add.add_argument("title")
    add.add_argument("--todo", action="store_true", help="tag it todo, so it can be closed")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--due", help="due date, stored in payload.due")
    add.add_argument("--visible-at", help="ISO 8601 with offset or Z; time-releases the event")
    add.add_argument("--expires-at", help="ISO 8601 with offset or Z")
    add.add_argument("--urgency", type=int, choices=range(4), default=0)
    add.add_argument("--body")
    add.add_argument("--audience", action="append", default=[])
    add.add_argument("--sign", metavar="KID", help="sign the event with this private key")
    add.add_argument(
        "--db",
        default=None,
        help="write straight to this database file instead of posting to --url (operator mode)",
    )
    add.add_argument(
        "--key-dir", default=None, help="private key directory (default: ~/.config/dayfile/keys)"
    )
    poll = sub.add_parser("poll-email", help="poll a Gmail folder and ingest into the database")
    poll.add_argument("--folder", default="SMS")
    poll.add_argument(
        "--interval", type=float, default=60, help="seconds between polls (and reconnects)"
    )
    poll.add_argument(
        "--no-idle", action="store_true", help="poll every --interval instead of using IMAP IDLE"
    )
    poll.add_argument("--once", action="store_true", help="poll once and exit")
    poll.add_argument("--db", default=os.environ.get("DAYFILE_DB", "dayfile.db"))
    notify = sub.add_parser("notify", help="publish urgent events to ntfy as they become visible")
    notify.add_argument(
        "--ntfy-url", default=os.environ.get("NTFY_URL"), help="topic URL (or $NTFY_URL)"
    )
    notify.add_argument("--interval", type=float, default=30, help="seconds between ticks")
    notify.add_argument("--once", action="store_true", help="tick once and exit")
    notify.add_argument("--db", default=os.environ.get("DAYFILE_DB", "dayfile.db"))
    gen = sub.add_parser("gen", help="run a generator (weather, digest)")
    gen_sub = gen.add_subparsers(dest="generator", required=True)
    wx = gen_sub.add_parser("weather", help="write today's weather.daily event")
    wx.add_argument("--lat", type=float, required=True)
    wx.add_argument("--lon", type=float, required=True)
    wx.add_argument("--day", help="YYYY-MM-DD (default: today, UTC)")
    wx.add_argument("--audience", default="self", help="comma-separated labels")
    dg = gen_sub.add_parser("digest", help="roll one UTC day into a digest.daily event")
    dg.add_argument("--day", help="YYYY-MM-DD (default: yesterday, UTC)")
    dg.add_argument("--audience", default="self", help="comma-separated labels")
    dg.add_argument("--summarizer", help="package.module:function (default: plain)")
    for p in (wx, dg):
        p.add_argument("--db", default=os.environ.get("DAYFILE_DB", "dayfile.db"))
    sub.add_parser(
        "mcp", help="run an MCP server on stdio (needs dayfile[mcp]; reads DAYFILE_URL/TOKEN)"
    )
    key = sub.add_parser("key", help="manage signing keys")
    key_sub = key.add_subparsers(dest="key_command", required=True)
    kc = key_sub.add_parser("create", help="generate an Ed25519 key pair")
    kc.add_argument("--kid")
    kc.add_argument("--note")
    kc.add_argument("--key-dir", default=None)
    key_sub.add_parser("list", help="list trusted public keys")
    ki = key_sub.add_parser("import", help="trust someone else's public key")
    ki.add_argument("--kid", required=True)
    ki.add_argument("--public", required=True, help="base64 of the 32-byte public key")
    ki.add_argument("--note")
    for p in key_sub.choices.values():
        p.add_argument("--db", default=os.environ.get("DAYFILE_DB", "dayfile.db"))
    backup = sub.add_parser("backup", help="write a consistent copy of the database")
    backup.add_argument("dest", help="file to create (must not exist)")
    backup.add_argument("--db", default=os.environ.get("DAYFILE_DB", "dayfile.db"))
    token = sub.add_parser("token", help="manage access tokens")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    create = token_sub.add_parser("create", help="mint a token and print its feed URL once")
    create.add_argument("--labels", required=True, help="comma-separated audience labels")
    create.add_argument("--expires", help="lifetime such as 30m, 12h or 30d")
    create.add_argument("--note")
    create.add_argument(
        "--write-source",
        help="make a write-only token that may POST events as these source.ids (comma-separated)",
    )
    token_sub.add_parser("list", help="list tokens (never shows secrets)")
    revoke = token_sub.add_parser("revoke", help="revoke a token by id")
    revoke.add_argument("token_id")
    for p in (create, token_sub.choices["list"], revoke):
        p.add_argument("--db", default=os.environ.get("DAYFILE_DB", "dayfile.db"))
    feed = sub.add_parser("feed", help="manage named feeds (saved filters, private or public)")
    feed_sub = feed.add_subparsers(dest="feed_command", required=True)
    feed_sub.add_parser("list", help="list feeds")
    setter = feed_sub.add_parser("set", help="create a feed or change the fields given")
    setter.add_argument("name", help="feed name; 'default' is the unfiltered /feed.json")
    setter.add_argument("--title")
    setter.add_argument("--tag", action="append", help="repeat to require several tags")
    setter.add_argument("--tag-any", action="append", help="repeat to allow any of several tags")
    setter.add_argument("--status", action="append")
    setter.add_argument("--within", help="for example 7d")
    vis = setter.add_mutually_exclusive_group()
    vis.add_argument("--public", action="store_true", help="anyone may read events labeled public")
    vis.add_argument("--private", action="store_true", help="a token is required again")
    drop_feed = feed_sub.add_parser("delete", help="remove a feed")
    drop_feed.add_argument("name")
    for p in (feed_sub.choices["list"], setter, drop_feed):
        p.add_argument("--db", default=os.environ.get("DAYFILE_DB", "dayfile.db"))
    beat = sub.add_parser("heartbeat", help="check heartbeats: write events for lapsed sources")
    beat_sub = beat.add_subparsers(dest="heartbeat_command", required=True)
    check = beat_sub.add_parser(
        "check", help="check every tenant (--root) or one database (--db) once"
    )
    check.add_argument("--root", default=os.environ.get("DAYFILE_TENANTS_DIR"))
    check.add_argument("--db", default=None)
    tenant = sub.add_parser("tenant", help="manage tenants of a multi-tenant deployment")
    tenant_sub = tenant.add_subparsers(dest="tenant_command", required=True)
    for name, text in (
        ("create", "make a tenant and print its database path"),
        ("limit", "change a tenant's limits ('none' clears one)"),
    ):
        p = tenant_sub.add_parser(name, help=text)
        p.add_argument("slug")
        p.add_argument("--max-events", help="most events the log may hold")
        p.add_argument("--max-bytes", help="database size (bytes) at which new events are refused")
        p.add_argument("--write-limit", help="EVENTS/SECONDS per write token, e.g. 600/60")
    tenant_sub.add_parser("list", help="list tenants")
    tenant_sub.add_parser("usage", help="print every tenant's limits and usage as JSON")
    stats = tenant_sub.add_parser(
        "stats", help="print aggregate counts across tenants as JSON (never log content)"
    )
    stats.add_argument(
        "--exclude", action="append", default=[], metavar="SLUG", help="leave out a tenant"
    )
    every = tenant_sub.add_parser(
        "backup-all", help="write a gzipped copy and settings file of every tenant into a directory"
    )
    every.add_argument("dest", help="directory to fill (created, mode 700)")
    for name, text in (
        ("show", "print a tenant's limits and usage as JSON"),
        ("suspend", "refuse all requests for a tenant; data is kept"),
        ("resume", "lift a suspension"),
        ("path", "print a tenant's database path (for --db)"),
    ):
        tenant_sub.add_parser(name, help=text).add_argument("slug")
    verify = tenant_sub.add_parser(
        "verify-backup", help="check a directory of backup-all output: integrity, rows, triggers"
    )
    verify.add_argument("dir", help="directory holding <slug>.db.gz files")
    purge = tenant_sub.add_parser(
        "purge", help="permanently remove deleted tenants held in .trash/ for at least N days"
    )
    purge.add_argument("--older-than-days", type=int, default=30, metavar="N")
    drop = tenant_sub.add_parser("delete", help="move a tenant to .trash/ (frees the name)")
    drop.add_argument("slug")
    drop.add_argument("--yes", action="store_true", help="confirm")
    for p in tenant_sub.choices.values():
        p.add_argument("--root", default=os.environ.get("DAYFILE_TENANTS_DIR"))
    args = parser.parse_args(argv)
    for flag in ("visible_at", "expires_at"):
        if value := getattr(args, flag, None):
            try:
                setattr(args, flag, normalize(value))
            except ValueError as e:
                parser.error(f"--{flag.replace('_', '-')}: {e}")
    return args


def build_event(args: argparse.Namespace) -> dict[str, Any]:
    """Build an event envelope from parsed `add` arguments."""
    event_id = new_ulid()
    ev: dict[str, Any] = {
        "specversion": "dayfile/1",
        "id": event_id,
        "source": {"id": "adapter:cli", "ref": event_id},
        "occurred_at": now(),
        "title": args.title,
    }
    optional = {
        "visible_at": args.visible_at,
        "expires_at": args.expires_at,
        "body": args.body,
        "tags": [*args.tag, "todo"]
        if (args.todo or args.due) and "todo" not in args.tag
        else args.tag,
        "audience": args.audience,
        "urgency": args.urgency,
        "payload": {"due": args.due} if args.due else None,
    }
    ev.update({k: v for k, v in optional.items() if v})
    return ev


def env_token(name: str) -> str | None:
    """Return the token in environment variable `name`, trimmed, or None if it is empty."""
    return (os.environ.get(name) or "").strip() or None


def post_events(url: str, events: list[dict[str, Any]]) -> tuple[int, Any]:
    """POST events as JSON, bearing $DAYFILE_WRITE_TOKEN if set; return status and body."""
    headers = {"Content-Type": "application/json"}
    if token := env_token("DAYFILE_WRITE_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, json.dumps(events).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def _heartbeat(args: argparse.Namespace) -> int:
    """Run the heartbeat check once over one database or every tenant."""
    from dayfile import db, heartbeats, timefmt
    from dayfile.tenancy import DirectoryTenancy

    now = timefmt.now()
    if args.db:
        conn = db.connect(args.db)
        try:
            found = {"": heartbeats.check(conn, now)}
        finally:
            conn.close()
    elif args.root:
        found = heartbeats.check_all(DirectoryTenancy(args.root, ""), now)
    else:
        print("dayfile: set --root, DAYFILE_TENANTS_DIR or --db", file=sys.stderr)
        return 1
    for slug, titles in found.items():
        for title in titles:
            print(f"{slug + ': ' if slug else ''}{title}")
    return 0


def _mcp() -> int:
    """Run the MCP server, which lives behind an optional extra."""
    try:
        from dayfile import mcp_server

        return mcp_server.main()
    except ModuleNotFoundError as e:
        if e.name and e.name.split(".")[0] == "mcp":
            print("dayfile: the MCP server needs `pip install dayfile[mcp]`", file=sys.stderr)
            return 1
        raise


def main(
    argv: Sequence[str] | None = None,
    post: Poster = post_events,
    publish: Callable[[str, dict[str, Any]], None] | None = None,
) -> int:
    """Entry point; returns the process exit code."""
    args = parse_args(argv)
    if args.command == "mcp":
        return _mcp()
    if args.command == "heartbeat":
        return _heartbeat(args)
    if args.command == "poll-email":
        return _poll_email(args)
    if args.command == "backup":
        return _backup(args)
    if args.command == "tenant":
        return _tenant(args)
    if args.command == "token":
        return _token(args)
    if args.command == "feed":
        return _feed(args)
    if args.command == "gen":
        return _gen(args)
    if args.command == "key":
        return _key(args)
    if args.command == "notify":
        return _notify(args, publish)
    event = build_event(args)
    if args.sign:
        from pathlib import Path

        from dayfile import signing

        try:
            key_dir = Path(args.key_dir) if args.key_dir else signing.default_key_dir()
            event = signing.sign_event(
                event, args.sign, signing.load_private_key(key_dir, args.sign)
            )
        except ValueError as e:
            print(f"dayfile: {e}", file=sys.stderr)
            return 1
    if args.db:
        return _add_direct(args.db, event)
    try:
        status, body = post(args.url.rstrip("/") + "/events", [event])
    except OSError as e:
        print(f"dayfile: cannot reach {args.url}: {e}", file=sys.stderr)
        return 1
    if status >= 400:
        print(f"dayfile: {status}: {body.get('error', body)}", file=sys.stderr)
        return 1
    stored = body[0] if isinstance(body, list) else body
    print(f"{stored['id']} seq {stored['seq']}")
    return 0


def _add_direct(db_path: str, event: dict[str, Any]) -> int:
    """Store one event in the local database, trusting the operator."""
    from dayfile import db
    from dayfile.ingest.core import IngestError, ingest_events

    conn = db.connect(db_path)
    try:
        stored = ingest_events(conn, [event])[0].event
    except IngestError as e:
        print(f"dayfile: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"{stored['id']} seq {stored['seq']}")
    return 0


def _key(args: argparse.Namespace) -> int:
    from pathlib import Path

    from dayfile import db, signing

    conn = db.connect(args.db)
    try:
        if args.key_command == "create":
            key_dir = Path(args.key_dir) if args.key_dir else signing.default_key_dir()
            kid = signing.create_key(conn, key_dir, args.kid, args.note)
            print(f"created key {kid}; private key in {key_dir / (kid + '.pem')}")
        elif args.key_command == "import":
            signing.import_public_key(conn, args.kid, args.public, args.note)
            print(f"imported key {args.kid}")
        else:
            for k in signing.list_keys(conn):
                print(
                    f"{k['kid']}  {k['fingerprint']}  {k['created_at']}  {k['note'] or ''}".rstrip()
                )
    except ValueError as e:
        print(f"dayfile: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _gen(args: argparse.Namespace) -> int:
    from dayfile import db
    from dayfile.gen import digest, weather
    from dayfile.timefmt import minus

    audience = [x.strip() for x in args.audience.split(",")]
    now_ts = now()
    conn = db.connect(args.db)
    try:
        if args.generator == "weather":
            day = args.day or now_ts[:10]
            res = weather.generate(
                conn, lambda: weather.open_meteo(args.lat, args.lon), now_ts, audience, day
            )
        else:
            day = args.day or minus(now_ts, 86400)[:10]
            summarizer = (
                digest.load_summarizer(args.summarizer)
                if args.summarizer
                else digest.plain_summarizer
            )
            res = digest.generate(conn, day, now_ts, audience, summarizer)
            if res is None:
                print(f"no events on {day}")
                return 0
    except (ValueError, OSError) as e:
        print(f"dayfile: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(res.event["title"] if res.created else f"already generated: {res.event['title']}")
    return 0


def _notify(args: argparse.Namespace, publish: Callable[[str, dict[str, Any]], None] | None) -> int:
    from dayfile import db
    from dayfile.notify import ntfy, scheduler

    if not args.ntfy_url:
        print("dayfile: set --ntfy-url or $NTFY_URL", file=sys.stderr)
        return 1
    send = publish or ntfy.publish
    logging.basicConfig(level=logging.INFO)
    conn = db.connect(args.db)
    try:
        scheduler.run(conn, lambda ev: send(args.ntfy_url, ev), args.interval, once=args.once)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
    return 0


def _feed(args: argparse.Namespace) -> int:
    from dayfile import db, feeds

    conn = db.connect(args.db)
    try:
        if args.feed_command == "list":
            for f in feeds.list_feeds(conn):
                query = "&".join(f"{k}={v}" for k, v in f["filter"]) or "everything"
                print(f"{f['name']:<20} {f['visibility']:<8} {query}  {f['title'] or ''}".rstrip())
        elif args.feed_command == "delete":
            if not feeds.delete_feed(conn, args.name):
                print(f"dayfile: no feed {args.name!r}", file=sys.stderr)
                return 1
        else:
            spec = {
                key: values
                for key, values in (
                    ("tag", args.tag),
                    ("tag_any", args.tag_any),
                    ("status", args.status),
                    ("within", args.within),
                )
                if values
            }
            visibility = "public" if args.public else "private" if args.private else None
            saved = feeds.save_feed(
                conn, args.name, now(), title=args.title, filter=spec or None, visibility=visibility
            )
            path = "/feed.json" if saved["name"] == feeds.DEFAULT else f"/f/{saved['name']}.json"
            print(f"{saved['name']}: {saved['visibility']}, served at {path}")
    except feeds.FeedError as e:
        print(f"dayfile: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _token(args: argparse.Namespace) -> int:
    from dayfile import db
    from dayfile.auth import tokens
    from dayfile.timefmt import now as clock_now
    from dayfile.timefmt import parse_duration, plus

    conn = db.connect(args.db)
    try:
        now_ts = clock_now()
        if args.token_command == "create":
            labels = [x.strip() for x in args.labels.split(",")]
            expires = plus(now_ts, parse_duration(args.expires)) if args.expires else None
            issued = tokens.create_token(
                conn,
                labels,
                expires_at=expires,
                note=args.note,
                now=now_ts,
                write_sources=[x.strip() for x in args.write_source.split(",")]
                if args.write_source
                else None,
            )
            if args.write_source:
                post_url = f"{args.url.rstrip('/')}/events"
                print(f"{issued.value}\n(POST {post_url} with Authorization: Bearer)")
            else:
                base = f"{args.url.rstrip('/')}/t/{issued.value}"
                print(f"{base}/feed.json\n{base}/feed.atom")
            print(f"token {issued.id}: shown once, store it now", file=sys.stderr)
        elif args.token_command == "list":
            for t in tokens.list_tokens(conn):
                state = (
                    "revoked" if t["revoked_at"]
                    else "expired" if t["expires_at"] and t["expires_at"] <= now_ts
                    else "active"
                )  # fmt: skip
                writes = "write:" + ",".join(t["write_sources"]) if t["write_sources"] else ""
                print(
                    f"{t['id']}  {state:<8} {','.join(t['labels']):<20}"
                    f" expires {t['expires_at'] or 'never':<20}"
                    f" {writes} {t['note'] or ''}".rstrip()
                )
        elif not tokens.revoke_token(conn, args.token_id, now_ts):
            print(f"dayfile: no live token {args.token_id!r}", file=sys.stderr)
            return 1
        else:
            print(f"revoked {args.token_id}")
    except ValueError as e:
        print(f"dayfile: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _limit_args(args: argparse.Namespace) -> dict[str, Any]:
    """Translate --max-events and --write-limit into tenant settings ('none' clears)."""
    out: dict[str, Any] = {}
    if args.max_events is not None:
        out["max_events"] = None if args.max_events == "none" else _int(args.max_events)
    if args.max_bytes is not None:
        out["max_bytes"] = None if args.max_bytes == "none" else _int(args.max_bytes)
    if args.write_limit is not None:
        if args.write_limit == "none":
            out["write_limit"] = None
        else:
            events, _, seconds = args.write_limit.partition("/")
            out["write_limit"] = (_int(events), _int(seconds))
    return out


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"not a whole number: {text!r}") from None


def _tenant_row(tenancy: Any, slug: str) -> dict[str, Any]:
    """One tenant's settings and usage, as printed by `tenant show` and `tenant usage`."""
    found = tenancy.get(slug)
    return {
        "id": found.id,
        "suspended": found.suspended,
        "write_limit": found.write_limit,
        "max_events": found.max_events,
        "max_bytes": found.max_bytes,
        **tenancy.usage(slug),
    }


def _tenant(args: argparse.Namespace) -> int:
    from dayfile.tenancy import DirectoryTenancy

    cmd = args.tenant_command
    if cmd == "verify-backup":
        return _verify_backup(args.dir)
    if not args.root:
        print("dayfile: set --root or DAYFILE_TENANTS_DIR", file=sys.stderr)
        return 1
    tenancy = DirectoryTenancy(args.root, os.environ.get("DAYFILE_BASE_DOMAIN", ""))
    try:
        if cmd == "create":
            print(tenancy.create(args.slug, **_limit_args(args)).db_path)
        elif cmd == "limit":
            tenancy.update(args.slug, **_limit_args(args))
        elif cmd == "list":
            for name in tenancy.list():
                print(name)
        elif cmd == "usage":
            print(json.dumps([_tenant_row(tenancy, name) for name in tenancy.list()], indent=2))
        elif cmd == "stats":
            from dayfile import stats, timefmt

            paths = {
                name: found.db_path
                for name in tenancy.list()
                if name not in args.exclude and (found := tenancy.get(name))
            }
            print(json.dumps(stats.summarize(paths, timefmt.now()), indent=2))
        elif cmd == "backup-all":
            return _backup_all(tenancy, args.dest)
        elif cmd == "purge":
            from dayfile.tenancy import purge_trash

            for name in purge_trash(args.root, args.older_than_days):
                print(f"purged {name}")
        elif cmd in ("suspend", "resume"):
            tenancy.update(args.slug, suspended=cmd == "suspend")
        elif cmd == "delete":
            if not args.yes:
                print(
                    "dayfile: delete moves the tenant's data to .trash; pass --yes", file=sys.stderr
                )
                return 1
            print(tenancy.delete(args.slug))
        else:
            found = tenancy.get(args.slug)
            if found is None:
                raise ValueError(f"no such tenant {args.slug!r}")
            if cmd == "path":
                print(found.db_path)
            else:
                print(json.dumps(_tenant_row(tenancy, args.slug), indent=2))
    except ValueError as e:  # TenancyError is a ValueError
        print(f"dayfile: {e}", file=sys.stderr)
        return 1
    return 0


def _backup(args: argparse.Namespace) -> int:
    """Copy the live database with SQLite's online backup (safe while the server runs)."""
    import sqlite3

    src, dest = Path(args.db), Path(args.dest)
    if not src.exists():
        print(f"dayfile: no database at {src}", file=sys.stderr)
        return 1
    if dest.exists():
        print(f"dayfile: {dest} exists; refusing to overwrite", file=sys.stderr)
        return 1
    try:
        copy_database(src, dest)
    except sqlite3.Error as e:
        print(f"dayfile: backup failed: {e}", file=sys.stderr)
        return 1
    print(f"backed up {src} to {dest}")
    return 0


def _verify_backup(directory: str) -> int:
    """Open every `<slug>.db.gz` in a directory and report whether each is a sound log."""
    import gzip
    import shutil
    import sqlite3
    import tempfile

    files = sorted(Path(directory).glob("*.db.gz"))
    if not files:
        print(f"dayfile: no *.db.gz files in {directory}", file=sys.stderr)
        return 1
    bad = 0
    for packed in files:
        if packed.name.endswith(".accounts.db.gz"):
            bad += _verify_accounts_backup(packed)
            continue
        slug = packed.name.removesuffix(".db.gz")
        problems: list[str] = []
        events, last = 0, 0
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / "restored.db"
            try:
                with gzip.open(packed, "rb") as src, copy.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
                try:
                    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        problems.append("integrity check failed")
                    events, last = conn.execute(
                        "SELECT count(*), COALESCE(max(seq), 0) FROM events"
                    ).fetchone()
                    guards = conn.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type = 'trigger'"
                        " AND (name LIKE '%no_update' OR name LIKE '%no_delete')"
                    ).fetchone()[0]
                    if guards < 8:
                        problems.append(f"only {guards} append-only triggers (expected 8)")
                finally:
                    conn.close()
            except (OSError, EOFError, gzip.BadGzipFile, sqlite3.Error) as e:
                problems.append(str(e))
        meta = packed.with_name(f"{slug}.tenant.json")
        try:
            json.loads(meta.read_text())
        except (OSError, ValueError):
            problems.append("tenant.json missing or unreadable (limits would be lost)")
        status = "ok" if not problems else "FAILED: " + "; ".join(problems)
        print(f"{slug}: {events} events, last seq {last}: {status}")
        bad += bool(problems)
    return 1 if bad else 0


def _verify_accounts_backup(packed: Path) -> int:
    """Check a gzipped copy of the accounts database; print one line and return 1 if it is bad."""
    import gzip
    import shutil
    import sqlite3
    import tempfile

    problems: list[str] = []
    accounts = 0
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "restored.db"
        try:
            with gzip.open(packed, "rb") as src, copy.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            try:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    problems.append("integrity check failed")
                accounts = conn.execute("SELECT count(*) FROM account").fetchone()[0]
            finally:
                conn.close()
        except (OSError, EOFError, gzip.BadGzipFile, sqlite3.Error) as e:
            problems.append(str(e))
    status = "ok" if not problems else "FAILED: " + "; ".join(problems)
    print(f"accounts database: {accounts} accounts: {status}")
    return 1 if problems else 0


def _backup_all(tenancy: Any, dest: str) -> int:
    """Write `<slug>.db.gz` and `<slug>.tenant.json` per tenant; a failure never stops the rest."""
    import gzip
    import shutil
    import sqlite3

    slugs = tenancy.list()
    if not slugs:
        print("dayfile: no tenants to back up", file=sys.stderr)
        return 1
    out = Path(dest)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    failed = 0
    for slug in slugs:
        copy = out / f"{slug}.db"
        try:
            tenant = tenancy.get(slug)
            copy_database(tenant.db_path, copy)
            with copy.open("rb") as raw, gzip.open(out / f"{slug}.db.gz", "wb") as packed:
                shutil.copyfileobj(raw, packed)
            (out / f"{slug}.db.gz").chmod(0o600)
            meta = tenant.db_path.parent / "tenant.json"
            if meta.is_file():
                shutil.copyfile(meta, out / f"{slug}.tenant.json")
                (out / f"{slug}.tenant.json").chmod(0o600)
        except (sqlite3.Error, OSError, AttributeError) as e:
            print(f"dayfile: backup of tenant {slug} failed: {e}", file=sys.stderr)
            (out / f"{slug}.db.gz").unlink(missing_ok=True)
            failed = 1
        finally:
            copy.unlink(missing_ok=True)
    return failed


def _poll_email(args: argparse.Namespace) -> int:
    from dayfile import db
    from dayfile.ingest import email_in

    logging.basicConfig(level=logging.INFO)
    conn = db.connect(args.db)
    try:
        email_in.run(
            conn,
            args.folder,
            args.interval,
            once=args.once,
            idle_timeout=None if args.no_idle else 540,
        )
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
