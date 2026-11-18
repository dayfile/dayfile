#!/usr/bin/env python3
"""Add the DNS hardening records to both zones on DigitalOcean; nothing that exists is changed.

Run it yourself, on your own machine (it asks for a DigitalOcean API token at a hidden prompt):

    python3 deploy/cloud/harden-dns.py            # show the plan, change nothing
    python3 deploy/cloud/harden-dns.py --apply    # create the missing records

It only ever creates records. A record that already exists is left alone, and a different record in
a place we would use is reported as a conflict and skipped. Use a token with only the domain scopes
and a short expiry, and delete it afterwards. The token is never printed or stored.
"""

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

API = "https://api.digitalocean.com/v2"
TTL = 3600
CONTACT = "mailto:team@dayfile.app"
# Certificate authorities allowed to issue: Let's Encrypt (what issues now), plus the ZeroSSL
# fallback Caddy may use (its CAA name is sectigo.com; zerossl.com is also listed). No wildcards.
CAA = [
    ("issue", "letsencrypt.org."),  # DigitalOcean wants a fully qualified name here
    ("issue", "sectigo.com."),
    ("issue", "zerossl.com."),
    ("issuewild", ";"),
    ("iodef", CONTACT),
]
Record = dict[str, Any]


class ApiError(Exception):
    """DigitalOcean refused a request; `args[0]` says why (its own message, never the token)."""


def caa_records() -> list[Record]:
    """The CAA records for a zone apex."""
    return [
        {"type": "CAA", "name": "@", "data": value, "flags": 0, "tag": tag} for tag, value in CAA
    ]


def wanted_caa() -> list[Record]:
    """The CAA records we manage, for telling them from anyone else's."""
    return caa_records()


def wanted() -> dict[str, list[Record]]:
    """What each zone should have. dayfile.cloud sends no mail; dayfile.app's mail is left alone.

    There is no null MX: DigitalOcean's API refuses it (a hostname is required). SPF `-all` and
    DMARC `p=reject` already say that the domain sends no mail.
    """
    return {
        "dayfile.cloud": [
            {"type": "TXT", "name": "@", "data": "v=spf1 -all"},
            {"type": "TXT", "name": "_dmarc", "data": "v=DMARC1; p=reject"},
            *caa_records(),
        ],
        "dayfile.app": caa_records(),
    }


def same(a: Record, b: Record) -> bool:
    """True if two records say the same thing."""
    keys = ("type", "name", "data", "tag")
    return all(a.get(k) == b.get(k) for k in keys)


def conflict(want: Record, existing: list[Record]) -> str | None:
    """A reason this record should not be added, if the zone already says something else there."""
    here = [r for r in existing if r["type"] == want["type"] and r["name"] == want["name"]]
    if want["type"] == "CAA":
        ours = wanted_caa()
        foreign = [r for r in here if not any(same(r, w) for w in ours)]
        return "a CAA record we did not make exists; review it by hand" if foreign else None
    if want["type"] == "TXT" and want["name"] == "@":
        here = [r for r in here if str(r["data"]).lower().startswith("v=spf1")]
    return f"already set to {here[0]['data']!r}" if here else None


def plan(zone: str, existing: list[Record]) -> tuple[list[Record], list[str]]:
    """The records to create in a zone, and notes on what was skipped and why."""
    create: list[Record] = []
    notes: list[str] = []
    skip_caa = False
    for want in wanted()[zone]:
        label = f"{want['type']} {want['name']} {want.get('tag', '')} {want['data']}".replace(
            "  ", " "
        )
        if any(same(want, r) for r in existing):
            notes.append(f"  present   {label}")
        elif want["type"] == "CAA" and skip_caa:
            notes.append(f"  skipped   {label}")
        elif reason := conflict(want, existing):
            skip_caa = skip_caa or want["type"] == "CAA"
            notes.append(f"  CONFLICT  {label}: {reason}")
        else:
            create.append(want)
            notes.append(f"  add       {label}")
    return create, notes


Send = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


def sender(token: str) -> Send:
    """A function that calls the DigitalOcean API with the token (sent, never shown)."""

    def send(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read()).get("message", "")
            except ValueError:
                why = ""
            raise ApiError(f"HTTP {e.code} {why}".strip()) from None
        except (urllib.error.URLError, OSError) as e:
            raise SystemExit(f"could not reach DigitalOcean: {e}") from None

    return send


def existing_records(send: Send, zone: str) -> list[Record]:
    """Every record in the zone."""
    found: list[Record] = []
    page = 1
    while True:
        got = send("GET", f"/domains/{zone}/records?per_page=200&page={page}", None)
        found += got.get("domain_records", [])
        if len(got.get("domain_records", [])) < 200:
            return found
        page += 1


def run(send: Send, apply: bool, out: Callable[[str], None] = print) -> int:
    """Plan (and with `apply`, create) the records; return 1 if anything conflicted."""
    conflicts = 0
    for zone in wanted():
        create, notes = plan(zone, existing_records(send, zone))
        out(zone)
        for line in notes:
            out(line)
            conflicts += "CONFLICT" in line
        if apply:
            made = 0
            for record in create:
                try:
                    send("POST", f"/domains/{zone}/records", {**record, "ttl": TTL})
                    made += 1
                except ApiError as e:
                    out(f"  FAILED    {record['type']} {record['name']} {record['data']}: {e}")
                    conflicts += 1
            out(f"  created {made} of {len(create)} record(s)")
        else:
            out(f"  would create {len(create)} record(s)")
    if not apply:
        out("Nothing was changed. Run again with --apply to create them.")
    return 1 if conflicts else 0


def main(argv: list[str] | None = None) -> int:
    """Ask for the token, then plan or apply."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="create the missing records")
    args = parser.parse_args(argv)
    token = getpass.getpass("DigitalOcean API token (hidden): ").strip()
    if not token:
        print("no token given", file=sys.stderr)
        return 2
    return run(sender(token), args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
