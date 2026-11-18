# dayfile

A personal, append-only event log exposed as feed views (Atom, JSON Feed). Events arrive from many sources (email, SMS, webhooks, cron generators, devices, agents). The log is the source of truth. Feeds are projections of it, addressed by URL queries such as `/feed.json?tag=todo&status=open&within=7d`.

Operations (servers, DNS, monitoring, secrets, rebuild from zero, incidents) are in `docs/ops/README.md` (the operator's private handbook: kept out of git, so it exists only in the maintainer's checkout). Read `docs/SPEC.md` for the event model and `docs/ROADMAP.md` for the milestones. Build one milestone at a time.

## Stack (override here if you change it)

- Python 3.11 or newer (tested on 3.11 to 3.15), `uv` for env and deps
- FastAPI + uvicorn for HTTP
- stdlib `sqlite3`, no ORM; schema lives in `schema.sql`
- `jsonschema` for envelope validation, schema in `schema/event.schema.json`
- `cryptography` (Ed25519) and `rfc8785` (JSON canonicalization) for signing, from milestone M7
- `jinja2` and `python-multipart` for the web app (`src/dayfile/web`, M15), an optional extra: `pip install dayfile[web]`; htmx is vendored, not installed. Core code never imports `dayfile.web` (tests/test_boundaries.py)
- `pytest` for tests, `ruff` for lint and format
- Ask before adding any other dependency.

## Commands (create these as part of M0)

```
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run uvicorn dayfile.app:app --reload
```

## Planned layout

```
src/dayfile/
  app.py        FastAPI app and routes
  db.py         connection setup, schema load, queries
  models.py     event dataclasses and validation helpers
  timefmt.py    UTC normalization and the one allowed timestamp format
  ingest/       adapters: http, email (IMAP), sms-gateway, cli
  render/       atom.py, jsonfeed.py, query parsing
  auth/         tokens, audience resolution
  notify/       ntfy publisher, scheduler loop
  gen/          generators: weather, digests
tests/
schema.sql
schema/event.schema.json
examples/events.jsonl
docs/
```

## Invariants (do not break these; add a test for each)

1. **Append-only.** Events are never updated or deleted. Triggers in `schema.sql` enforce it. State changes are new events that point back through `refs`.
2. **One timestamp format.** UTC, `YYYY-MM-DDTHH:MM:SSZ`, normalized at ingest. The SQL CHECK constraints reject anything else. String order then equals time order.
3. **Paginate by `seq`**, never by timestamp. Late-arriving items with old `occurred_at` must not be skipped by readers.
4. **Time release.** A feed shows an event only when `visible_at <= now` and it is not past `expires_at`.
5. **Idempotent ingest.** `UNIQUE (source_id, source_ref)`. Re-delivering the same email or webhook is a no-op that returns the existing event.
6. **Plain-text fallback.** Every event has a `title`. Renderers work with `title` and `body` alone, ignoring `payload`.
7. **Deny by default.** A missing or empty `audience` means `["self"]`. A request's audience comes from its credential, never from the URL.
8. **No cross-credential cache leaks.** Any response whose content depends on the credential sends `Cache-Control: private`, `Vary: Authorization`, and an ETag that includes the caller's identity.
9. **Never log secrets.** Tokens are stored hashed. Log token ids, never token values.
10. **Signatures cover the canonical form** (RFC 8785) of the event minus `sig`. Server-assigned fields (`seq`, `recorded_at`) stay outside the signature.

## Known tradeoff

Feed readers rarely send custom headers, so v0 accepts a per-feed token in the URL path (`/t/<token>/feed.json`). Mitigations: store only a hash, make tokens expire, scope each token to a set of audience labels, support revocation, and support the `Authorization: Bearer` header for programmatic clients. Treat the URL-token path as a compatibility layer to replace later (see M8).

## Conventions

- There is no event `type`. The tag `todo` makes an event a to-do (open until closed). Housekeeping events carry the tag `dayfile-admin`.
- Event ids are ULIDs. For email, derive `source_ref` from a hash of `Message-ID`.
- Write the test first for any behavior listed in the invariants.
- Keep functions small and typed. Public functions get a one-line docstring.
- Commit per milestone step with a message that names the milestone (`M2: atom renderer`).

## Definition of done for a milestone

All acceptance criteria in `docs/ROADMAP.md` pass, `pytest` and `ruff` are clean, and `docs/SPEC.md` matches the behavior. Update the spec in the same commit as any behavior change.
