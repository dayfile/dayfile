# Roadmap

Build in order. Each milestone ends with passing tests and an updated `docs/SPEC.md`.

## Status

| Milestone | State |
|---|---|
| M0 to M7 | Built, tested, and documented in the spec. |
| M17 | Named feeds and public feeds: saved filters at `/f/<name>`, per-feed private or public visibility, anonymous reads of public feeds limited to events labeled `public`, `dayfile feed`, admin API routes. Built. |
| M16 | Accounts for the web app (design only, see `docs/design/m16-accounts.md`): sign-in with GitHub OAuth, invite codes, an admin API in `dayfile`, a dashboard to create and revoke tokens and see usage, export and delete. Slices 16a to 16f; 16a (accounts store, invites, operator CLI), 16b (admin API) 16c (GitHub sign-in, sessions, invite signup) and 16d (dashboard: log name, tokens, feeds) are built. Billing is out of scope. |
| M15 | Web app at `dayfile.app` (`dayfile.web`): landing, docs, draft terms and privacy, and a read-only log viewer that signs in with a read token. A separate container with no access to tenant files; it reads through the public API. |
| M14 | Abuse limits: per-token read rate limit and cap on held streams and long-polls (expiring leases). |
| M13 | Deployment profiles: `deploy/selfhost` and `deploy/cloud` (wildcard-style tenant TLS, Litestream, tenant backups), `docs/CLOUD.md`. Nothing deployed. |
| M12 | Tenancy seam: one database per tenant by subdomain, suspension, quotas, per-tenant write limit, body-size limit, `dayfile tenant`. |
| M11 | Native JSON API: `GET /events`, `GET /events/{id}`, long-poll, SSE stream; conditional-GET fix. |
| M10 | Operations: `/healthz`, `dayfile backup`, Docker and Caddy deployment, AGPL-3.0 licensing. |
| M9e | Atom cursors and a per-token write rate limit. |
| M9d | Reversible lifecycle: retracting a completion re-opens its target; cycles resolved by earliest event (open question 7 closed). |
| M9c | IMAP IDLE and a persisted UID cursor for email-in (verified read-only against live Gmail). |
| M9b | Release cursor: seq followers no longer miss time-released events (spec §3 Query, open question 6 closed). |
| M9a | Write credentials for `POST /events` (see spec §5). |
| M8 | Partly built: CloudEvents import and export, and the `WWW-Authenticate: Feed` challenge with a `rel="authorization"` link. |

Not built in M8, and why:
- **Biscuit or macaroon tokens.** Needs a new dependency; CLAUDE.md requires asking first.
- **WebSub-style hub.** Needs outbound delivery with retries and subscriber verification; a design of its own.
- **Encrypted items and a hash chain.** Blocked on spec open questions 2 and 4.

## M0: Skeleton

- `pyproject.toml` (uv), package `src/dayfile`, pytest and ruff configured.
- `db.py` opens SQLite, sets `foreign_keys=ON` and WAL, loads `schema.sql` if the database is empty.
- `timefmt.py` normalizes any ISO 8601 input to `YYYY-MM-DDTHH:MM:SSZ` and rejects naive local times.
- `models.py` validates an event dict against `schema/event.schema.json`.

Acceptance:
- Every line of `examples/events.jsonl` validates.
- Tests prove the append-only triggers reject UPDATE and DELETE on all four event tables.
- Tests prove the CHECK constraints reject a timestamp with an offset.

## M1: Ingest

- `POST /events` accepts one event or a JSON array. Fills defaults (`visible_at`, `audience`), validates, inserts atomically, returns stored events with `seq`.
- Idempotency on `(source.id, source.ref)`.
- CLI: `dayfile add "title" --tag admin --due 2026-11-14 --visible-at ...` generates a ULID and posts it.
- Email-in: IMAP poller. Subject becomes `title`, body becomes `body`, `source.ref` is a hash of `Message-ID`. A subject prefix `todo:` adds the tag `todo`. This path also receives SMS when Google Voice forwards texts to email.

Acceptance: ingesting `examples/events.jsonl` twice yields 8 rows. A malformed event returns 422 with the schema error and writes nothing.

## M2: Feed rendering

- `render/jsonfeed.py` and `render/atom.py` following the map in the spec.
- Query parsing for `tag`, `tag_any`, `status`, `within`, `since_seq`, `limit`.
- Time release and expiry. `seq` cursor pagination. ETag with conditional GET.

Acceptance:
- An event with a future `visible_at` is absent now and present after the clock passes it (inject the clock in tests).
- Output passes a JSON Feed validator and an Atom validator.
- A client that follows `next_url` cursors sees every event exactly once, including late-arriving events with old `occurred_at`.

## M3: Lifecycle

- Derived state from the `event_state` view. `status=` filter. Default hides retracted and superseded.
- Lifecycle authorization: same `source.id` or an authorized audience label.
- Refs to ids that arrive later take effect when the target appears.

Acceptance: the closing, retracting and superseding examples change feed output as the spec describes, and the underlying rows never change.

## M4: Access control

- `tokens` and `token_audience` tables in use. `dayfile token create|list|revoke`.
- Bearer header and `/t/<token>/` path credentials.
- Audience filter: event labels intersect token labels. No credential means the `self`-only deny: reject with 401.
- `Cache-Control: private`, `Vary: Authorization`, ETag that includes the label set.

Acceptance:
- A `family` token never sees `self`-only events, in feed output, counts, cursors, or error messages.
- Two tokens with different labels get different ETags for the same URL.
- Expired and revoked tokens return 401.
- Token values never appear in logs (test by capturing log output).

## M5: Push notifications

- Scheduler loop: events that became visible since the last tick with `urgency >= 2` publish to an ntfy topic.
- Delivery state in a `deliveries` table. Restarting the process never re-sends.

Acceptance: a time-released urgent todo triggers exactly one publish at its `visible_at`.

## M6: Generators

- `gen/weather.py` produces a `weather.daily` event each morning, idempotent per day through `source.ref`.
- `gen/digest.py` rolls a day of events into one `digest.daily` event. The summarizer is pluggable so an LLM can fill it in. The output is an ordinary event, written back to the log.

## M7: Signing

- Key management (`dayfile key create`), Ed25519 signing, RFC 8785 canonicalization.
- Verify on ingest when `sig` is present. Expose verification status in `_dayfile`.

Acceptance: altering any signed field after signing fails verification, and altering `recorded_at` does not.

## M8: Protocol extensions (research track)

- Biscuit or macaroon tokens with offline attenuation.
- `WWW-Authenticate: Feed` discovery challenge and a `rel="authorization"` feed link.
- CloudEvents import and export mapping.
- WebSub-style hub for push to aggregators.
- Encrypted items and a hash chain, after the open questions in the spec are settled.

## Kickoff prompt for Claude Code

```
Read CLAUDE.md, docs/SPEC.md, and docs/ROADMAP.md. Implement milestone M0 only.
Write the tests first, run them, and show me the failing output before the
implementation. When M0 passes, stop and summarize what changed.
```
