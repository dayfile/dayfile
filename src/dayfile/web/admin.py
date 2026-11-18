"""Operator commands for the accounts database: python -m dayfile.web.admin invite|account ..."""

import argparse
import gzip
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from dayfile import timefmt
from dayfile.tenancy import copy_database
from dayfile.web.accounts import store

DEFAULT_DB = "/accounts/accounts.db"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    p = argparse.ArgumentParser(prog="dayfile.web.admin", description=__doc__)
    p.add_argument("--db", default=os.environ.get("DAYFILE_ACCOUNTS_DB", DEFAULT_DB))
    sub = p.add_subparsers(dest="group", required=True)
    invite = sub.add_parser("invite", help="manage invite codes").add_subparsers(
        dest="action", required=True
    )
    create = invite.add_parser("create", help="make a code and print it once")
    create.add_argument("--note", required=True, help="who or what the code is for")
    create.add_argument("--uses", type=int, default=1)
    create.add_argument("--expires", help="30m, 12h or 14d from now (default: never)")
    invite.add_parser("list", help="show invites (never codes)")
    revoke = invite.add_parser("revoke", help="stop a code from working")
    revoke.add_argument("id")
    account = sub.add_parser("account", help="manage accounts").add_subparsers(
        dest="action", required=True
    )
    account.add_parser("list", help="show accounts")
    for name in ("suspend", "resume"):
        account.add_parser(name, help=f"{name} an account").add_argument("login")
    stats = sub.add_parser("stats", help="print account counts as JSON (numbers only)")
    stats.add_argument(
        "--exclude", action="append", default=[], metavar="SLUG", help="leave out an account"
    )
    stats.add_argument(
        "--list-answers", action="store_true", help="add the sign-up answers (personal data)"
    )
    stats.add_argument(
        "--list-emails", action="store_true", help="add consenting addresses (personal data)"
    )
    sub.add_parser(
        "backup", help="write a verified, gzipped copy of the accounts database to stdout"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Run one command; return the exit status."""
    args = build_parser().parse_args(argv)
    now = timefmt.now()
    conn = store.connect(args.db)
    try:
        if args.group == "backup":
            with tempfile.TemporaryDirectory() as scratch:
                copy = Path(scratch) / "accounts.db"
                copy_database(Path(args.db), copy)
                with (
                    copy.open("rb") as raw,
                    gzip.GzipFile(fileobj=sys.stdout.buffer, mode="wb") as out,
                ):
                    shutil.copyfileobj(raw, out)
        elif args.group == "stats":
            found = store.account_stats(conn, now, frozenset(args.exclude))
            if args.list_answers:
                found["signup_answers"] = store.list_answers(conn)
            if args.list_emails:
                found["contact_emails"] = store.list_emails(conn)
                found["interest_emails"] = store.interest_emails(conn)
            print(json.dumps(found, indent=2))
        elif (args.group, args.action) == ("invite", "create"):
            if args.uses < 1:
                print("dayfile: --uses must be at least 1", file=sys.stderr)
                return 2
            expires = None
            if args.expires:
                try:
                    expires = timefmt.plus(now, timefmt.parse_duration(args.expires))
                except ValueError as e:
                    print(f"dayfile: {e}", file=sys.stderr)
                    return 2
            print(store.create_invite(conn, args.note, now, args.uses, expires))
        elif (args.group, args.action) == ("invite", "list"):
            for row in store.list_invites(conn):
                print(json.dumps(row))
        elif (args.group, args.action) == ("invite", "revoke"):
            if not store.revoke_invite(conn, args.id, now):
                print("dayfile: no such live invite", file=sys.stderr)
                return 1
        elif (args.group, args.action) == ("account", "list"):
            for row in store.list_accounts(conn):
                print(json.dumps(row))
        else:
            status = "suspended" if args.action == "suspend" else "active"
            if not store.set_status(conn, args.login, status, now):
                print(f"dayfile: no account for {args.login!r}", file=sys.stderr)
                return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
