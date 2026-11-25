# dayfile spec, draft 0 (specversion `dayfile/1`)

## 1. Model

A dayfile instance holds one append-only **event log**. Producers (adapters, devices, agents, the user) append events. Consumers read **feeds**, which are filtered projections of the log. The server never edits an event. A state change (done, moved, deleted) is a new event that references an earlier one.

## 2. Event envelope

Machine-readable definition: `schema/event.schema.json`. Example events: `examples/events.jsonl`.

| Field | Req (filled: the server adds it to an unsigned post that omits it) | Meaning |
|---|---|---|
| `specversion` | filled | `"dayfile/1"`. The server fills it in for an unsigned post that omits it. Envelope revision, inside the signature. Adding an optional field does not bump it. Changing or removing a field's meaning does. |
| `id` | filled | ULID. The server makes one for an unsigned post that omits it; an author sends their own to generate offline or to sign (the id is inside the signature). |
| `source` | filled | `{id, ref?}`. Filled in with the write token's source when it has exactly one. `id` names the adapter or device. `ref` is the external identifier, used for idempotent ingest. |
| `occurred_at` | stored | When it happened in the world. The server uses the time of the post for an unsigned post that omits it. |
| `visible_at` | no | When the event appears in feeds. Defaults to `occurred_at`. A future value gives time-release. |
| `expires_at` | no | End of relevance. Renderers drop the event from rolling feeds after this. |
| `title` | yes | Plain-text headline. Every event has one. |
| `body` | no | Plain-text detail. |
| `tags` | no | Flat strings. Hierarchy by convention (`fleet/anomaly`). |
| `urgency` | no | 0 to 3. Routing hint for the notifier. Feeds ignore it. |
| `payload` | no | Object of the sender's own data. Stored and passed along, never validated; agree its shape with the reader. |
| `refs` | no | `[{rel, id}]`, with `rel` in `completes`, `supersedes`, `retracts`, `replies-to`. |
| `links` | no | `[{rel, href}]`. |
| `audience` | no | Access labels. Missing or empty means `["self"]`. |
| `sig` | no | `{kid, alg, value}`. Ed25519 over the RFC 8785 canonical form of the event without `sig`. |

Server-assigned, outside the envelope and outside the signature: `seq` (monotonic log position) and `recorded_at` (ingest time).

### Timestamps

UTC, second precision, always `YYYY-MM-DDTHH:MM:SSZ`. Ingest converts offsets to UTC, drops fractional seconds, and rejects ambiguous local times (no offset or `Z`) and date-only values.

### Unknown types

The server stores any well-formed event. Readers render `title` and `body` and ignore `payload`.

## 3. Lifecycle

| Relation | Effect on the target |
|---|---|
| `completes` | state `closed` |
| `supersedes` | state `superseded`; the new event is the current version |
| `retracts` | state `retracted`; renderers omit the target by default |
| `replies-to` | no state change; threading only |

State is computed at read time from the `lifecycle_edges` view in `schema.sql`. Precedence: retracted, closed, superseded, open. (The state was called `done` before; `status=done` is still accepted as `closed`.) The rows of the target never change.

**Todo.** The tag `todo` is the only thing that gives an event a state of its own. dayfile has no event `type`; the tag does that job, and the web post form explains it in a sentence.

**Status-less events.** Only an event tagged `todo` has a state of its own (`open` until closed). Any other event has **no status**: the `status` field is left out of the output, the web log shows no state chip, and the filter value `none` selects these events. Any event can still become `closed`, `superseded` or `retracted` through a lifecycle ref. The default filter (no `status=`) is `open`, `closed` and `none`; retracted and superseded are shown only when asked for.

**Inherited titles.** A `completes`, `supersedes` or `retracts` event sent without a `title` takes its target's title, so closing something needs only the ref. The target is used only when it shares an audience label with the new event (a writer never learns a title it could not already read); otherwise `title` is required as usual.

**Short ids.** In a `completes`, `supersedes` or `retracts` ref (and in the id of `POST /events/{id}/close` and `/retract`), `id` may be the last 6 to 25 characters of an event id instead of all 26: case is ignored and Crockford's look-alikes count (`O` is `0`, `I` and `L` are `1`). The server replaces it with the full id before anything is validated or stored, so the log, feeds and signatures only ever hold full ids. Only events sharing an audience label with the new event are searched, as for inherited titles. A tail that matches no event, or more than one, is a 422 ("no event ends in ..." or "more than one event ends in ...; give more characters"); nothing is guessed. Signed events are never rewritten, so a short id in one is invalid. The last six characters are the end of the ULID's random part (about a billion values), so a clash needs a very large log; schema 12 adds the index `ix_events_tail` on `substr(id, -6)` so the lookup does not scan the log. The web view shows the last six characters on each event; clicking one copies them, and Shift-click copies the full id.

- **Late targets.** A ref may name an id that has not arrived yet; the edge exists, and takes effect, once the target is in the log.
- **Release.** A lifecycle event changes its target's state only once the lifecycle event is itself released (`visible_at <= now`). A closing event dated next week does not complete the todo today. Expiry of the lifecycle event does not undo the change.
- **Authorization.** A `completes`, `supersedes` or `retracts` ref is effective only if the referencing event has the same `source.id` as the target, or shares at least one audience label with it. Refs from other sources with no common label are stored but have no effect. A self-reference has no effect. M4 replaces the label-overlap rule with the labels of the credential used at ingest; until then anyone who can POST can claim a label.
- **Not reversible in v0.** A lifecycle event that is itself retracted or superseded still applies to its target. Un-doing a closing event needs a design (open question 7).
- **Reversal.** A lifecycle edge counts only while the event that carries it is not itself retracted. Retracting a closing event (or any `completes`/`supersedes` event) re-opens its target, and retracting a retraction restores the original. This is evaluated with the clock, so a not-yet-released retraction changes nothing. In a retraction cycle (A retracts B, B retracts A) the earliest event (lowest `seq`) wins and the other is retracted. `db.refresh_live_edges` computes the edges in force into `temp.live_edges`; `query_events`, `events_between` and the notifier call it before using `STATE_CASE`. The old `event_state` view is gone (schema version 7).

Databases created by an earlier schema are upgraded when opened: `PRAGMA user_version` below 2 recreates the views (tables are untouched).

## 4. Feeds

### Query

`GET /feed.json` and `GET /feed.atom`, with filters:

| Param | Meaning |
|---|---|
| `tag=a&tag=b` | events having all listed tags (`tag_any=` for any) |
| `status=open` | filter on derived state (`open`, `closed`, `superseded`, `retracted`, `none`), repeatable. Default is `open`, `closed` and `none`: retracted and superseded events are hidden unless asked for |
| `within=7d` | `visible_at` in the last N (units `m`, `h`, `d`) |
| `since_seq=N` | events after log position N; `0` starts from the beginning |
| `released_after=T` | with `since_seq`: also return events at or below the cursor whose `visible_at` is after `T` (held-back events released since). `T` is any accepted timestamp; use `_dayfile.released_cursor` from the previous response |
| `limit=N` | page size, default 50; values above 200 are clamped to 200 |

Rules:

- Only events with `visible_at <= now` and not past `expires_at` are returned. An event is still shown at the instant `now == expires_at`.
- Unknown parameters, repeated scalar parameters (`within`, `since_seq`, `limit`) and malformed values return 400 with `{"error": ...}`.
- Order is `seq` ascending internally. Rendered feeds list newest first, and pagination links use `seq` cursors.
- **Without `since_seq`** the response is the newest `limit` matching events (what a feed reader wants) and has no `next_url`. **With `since_seq=N`** the response is the next `limit` matching events after N, still rendered newest first, and carries `next_url` (JSON Feed) or `<link rel="next">` (Atom) when more remain. `next_url` repeats the request with `since_seq` set to the highest `seq` returned and keeps every filter. A client that starts at `since_seq=0` and follows `next_url` sees each event exactly once, including late arrivals with an old `occurred_at`.
- `_dayfile.cursor` (JSON Feed) is the highest `seq` seen so far (never lower than the request's `since_seq`, even when the page holds only late releases), for clients that poll with `since_seq`.
- **Following with time release.** A follower stores two values from each response: `_dayfile.cursor` and `_dayfile.released_cursor` (the server's `now`). It polls `?since_seq=<cursor>&released_after=<released_cursor>`. Events released late (`seq` below the cursor, `visible_at` after the old `released_cursor`) then arrive exactly once. `next_url` carries both values, bumping `released_after` to the page's `now`, so a release between pages is neither lost nor repeated. Without `released_after` the old behavior holds. The audience, type, tag and status filters apply to late releases identically, and `released_after` reveals nothing about hidden events.
- Responses carry `Cache-Control: no-cache` and a strong `ETag`, the SHA-256 of the response body (so it differs per format, query and content). `If-None-Match` with a matching tag, or `*`, returns 304 with no body. M4 adds the caller's identity to the tag.
- Content types are `application/feed+json` and `application/atom+xml`.

### JSON API (M11)

The feeds are views; the native API returns events exactly as stored, plus derived fields. All endpoints take `Authorization: Bearer` only (never a path or query token), apply the same audience filtering, time release, `private` caching and per-caller ETags as the feeds, and answer 401/403 the same way.

| Endpoint | Returns |
|---|---|
| `GET /events` | `{"events": [...], "cursor": N, "released_cursor": T, "next": url or null}`; events in ascending `seq`, each the full envelope plus `seq`, `recorded_at`, `status` (and `sig_status` when signed). Takes every feed filter (`tag`, `tag_any`, `type`, `status`, `within`, `since_seq`, `released_after`, `limit`). Without `since_seq` it is the newest `limit` events (still ascending); with it, the next page, and `next` repeats the request with both cursors when more remain |
| `GET /events?since_seq=N&wait=S` | long-poll: if nothing new is visible, waits up to `S` seconds (0 to 30) for something to appear and returns an empty page on timeout. `wait` requires `since_seq`. A token revoked while waiting ends the wait with 401 |
| `GET /events/{id}` | one event with its `status`, including retracted and superseded ones (the default status filter does not apply to an exact id). Unknown, unreleased, expired and not-in-your-audience all return the same `404 {"error": "not found"}` |
| `GET /events/stream` | Server-Sent Events, one `event: event` frame per event, `data` the same JSON as `GET /events`. `id:` is `<seq>:<released_at>`; reconnect with `Last-Event-ID` (or `since_seq` and `released_after`) to resume without gaps or repeats, including late-released events. With no cursor it starts from now and replays nothing; `since_seq=0` replays everything. Sends `: keep-alive` comments, re-checks the credential on every poll (a revoked token ends the stream), and closes after 300 seconds so clients reconnect |

Browsers' `EventSource` cannot send an `Authorization` header; use `fetch` with a streaming body, or a non-browser client.

The ETag covers the caller, the request URL, the events and the cursor but not the server's clock, so conditional GET works even though every body embeds `released_cursor` (the server's `now`). A held event being released, or any status change, changes the tag. (Until M11 the JSON Feed and Atom ETags hashed the whole body and never matched; fixed.)

### Rendering map

| Event field | JSON Feed | Atom |
|---|---|---|
| `id` | `id` as `urn:dayfile:<id>` | `id` |
| `visible_at` | `date_published` | `published` |
| `recorded_at` | `date_modified` | `updated` |
| `title` | `title` | `title` |
| `body` | `content_text` (`""` when absent) | `content type="text"` (empty when absent) |
| `tags` | `tags` | `category` |
| `links` first `related` | `url` | `link rel="related"` |
| `payload`, `refs`, `urgency`, `sig`, `seq`, `status` (derived state at request time, not stored) | `_dayfile` object (absent fields omitted) | namespaced elements under `urn:dayfile:1`: `d:seq`, `d:status`, `d:urgency`, `d:payload` and `d:sig` as JSON text, one `d:ref rel= id=` per ref |

## 5. Access control

Goal: one URL space, per-credential content, no accounts for readers.

- A **token** carries a set of audience labels, an optional expiry, and can be revoked. The server stores a hash of the secret only.
- An event is visible to a request when the event's audience labels intersect the token's labels, or include the label `public`, which every reader has (M17). The request's labels come from the credential, never from the query string.
- Credentials: `Authorization: Bearer <token>` for programs, and `/t/<token>/feed.json` for feed readers that cannot send headers.
- Caching: authenticated responses send `Cache-Control: private` and `Vary: Authorization`. The ETag covers the caller's label set, so two callers never share a validator for different content.
- Token minting: `dayfile token create --labels family --expires 30d` prints the URL once.

### As built (M4)

- **Token format.** `<id>.<secret>`: a 12-hex public id and a 43-character URL-safe secret. Only `sha256(secret)` is stored; verification is constant-time and does a dummy hash for unknown ids. `dayfile token create --labels a,b [--expires 30d] [--note N]`, `token list` (never shows secrets) and `token revoke <id>`. The feed URLs are printed once.
- **Credentials.** `Authorization: Bearer <token>` is preferred; otherwise the `/t/<token>/feed.json` or `/t/<token>/feed.atom` path form. A token in the query string is not a credential. A malformed, unknown, expired or revoked token is a 401 indistinguishable from a missing one.
- **Deny by default.** No valid credential is 401 with `WWW-Authenticate: Bearer realm="dayfile"`, `Cache-Control: no-store` and `Vary: Authorization`.
- **Audience filter.** Applied in the query itself, so hidden events cannot affect the page, `has_more`, `next_url`, `_dayfile.cursor` or lifecycle fold. A token with no labels sees nothing. The `audience` query parameter is rejected.
- **Caching.** Every credentialed response sends `Cache-Control: private, no-cache` and `Vary: Authorization`. The ETag is a hash over the token id, its sorted labels and the body, so two holders never share a validator even with identical labels. A revoked token gets 401 even with a matching `If-None-Match`.
- **Logs.** `/t/<token>/` is rewritten to `/t/[redacted]/` in `uvicorn.access`, `uvicorn.error` and `dayfile` log records. Auth failures log the token id and a reason, never the value.
### Write credentials (M9)

- **Write tokens.** `dayfile token create --labels self,family --write-source adapter:phone,adapter:sms` mints a token that may `POST /events` and nothing else. Table `token_write(token_id, source_id)`; the token's labels (`token_audience`) cap what it may grant. A write token cannot read feeds (403) and a read token cannot write (403).
- **Bearer only.** Writes accept `Authorization: Bearer` only, never the path or query form. No or invalid credential is 401 (same challenge as reads).
- **Bound to the credential.** Every event's `source.id` must be one of the token's write sources, and its audience (after the `["self"]` default) must be a subset of the token's labels; otherwise 403 and the whole batch is rejected, nothing written. The 403 never says whether an id or `(source, ref)` already exists.
- **Lifecycle consequence.** Because a remote writer cannot forge `source.id` or widen the audience, the lifecycle authorization rule (same `source_id`, or shared audience label) is now enforced by the credential for HTTP writers. A `family`-only writer's tombstone for a `self`-only event has no effect.
- **Local writers.** `dayfile add --db PATH`, `poll-email`, `gen` and `notify` write to the database directly and are trusted as the operator. `dayfile add --url` sends `$DAYFILE_WRITE_TOKEN` as the bearer, trimmed of surrounding whitespace (the same for `dayfile mcp` and its two token variables).
- **Rate limit.** Each write token may post 600 events per 60 seconds (fixed window per token id, in memory, reset on restart); beyond that, 429 with `Retry-After` and nothing is written. Configurable through `create_app(write_limit=(events, seconds))`. Failed authentication is not counted.
- **Still open.** Write tokens are long-lived like read tokens.

### Discovery (experimental, M8)

A 401 carries two challenges: the standard `WWW-Authenticate: Bearer realm="dayfile"` and `WWW-Authenticate: Feed realm="dayfile", path="/t/{token}/"`, which tells a client that a token can be supplied as a path prefix. If the operator sets `$DAYFILE_AUTH_URI` (an http(s) URL), the challenge adds `authorization_uri="..."` and every feed response advertises it as `Link: <uri>; rel="authorization"`, `_dayfile.authorization_uri` (JSON Feed) and `<link rel="authorization">` (Atom). dayfile itself serves no page there. The `Feed` scheme and its parameters are a proposal, not a registered scheme.

Planned evolution (M8): attenuable tokens (Biscuit or macaroons) so a holder can derive a narrower token offline, a `WWW-Authenticate` challenge for discovery, and subscription handshakes for caching aggregators.

## 6. Ingest

Every adapter produces the same envelope. Adapters set `source.id` and `source.ref`. The server validates against the JSON Schema, normalizes timestamps, fills `visible_at` and `audience` defaults, inserts the event and its child rows in one transaction, and returns the stored event. A duplicate `(source.id, source.ref)` returns the existing event with status 200 and creates nothing.

### HTTP

`POST /events` takes one event or a JSON array and returns the stored event(s) with `seq` and `recorded_at`. Status 201 if any event was created, 200 if every event was a duplicate, 422 with `{"error": ...}` if any event is malformed (the whole request writes nothing), 409 if an `id` is already taken by a different `(source.id, source.ref)`. A missing or empty `audience` is stored as `["self"]`. **Quick post:** on the native JSON format only `title` is required from the caller. The server fills `specversion` (`dayfile/1`), `id` (a new ULID), and `occurred_at` (now), and no `type`, and `source.id` when the write token has exactly one source (with several, `source` must be given). Fields the caller supplies are kept. An event carrying `sig` is never completed, since the signature covers those fields. A quick post has no `source.ref`, so it cannot be de-duplicated: a retry creates a second event. Until M4 the endpoint has no authentication: bind it to localhost.

`POST /events/{id}/close` and `POST /events/{id}/retract` are shortcuts for closing a to-do and for retracting an event (retracting a closing event reopens its to-do). Each takes an optional JSON object with only a `title` and a `body` (the reason), appends the event `{"refs": [{"rel": "completes" (or "retracts"), "id": id}], ...}` through the same path as `POST /events` (same write token, audience, quota and rate rules, same filled-in fields), and returns that event with 201. Any other key, a body that is not an object, or an id that is neither a ULID nor the last 6 or more characters of one is 422; an unknown or unreadable target is 422 as for any ref. The assistant tool `dayfile_close_todo` takes an optional `note` (sent as the `body`), `dayfile close <id>` and `dayfile retract <id>`, each with `[--note TEXT] [--db FILE]`, do the same from the CLI, and `dayfile_retract_event` takes a `note` too, and the web Close button has an optional *Why?* field. History shows a closing step's `body`, and its `title` when it differs from the target's.

### CLI

`dayfile add "title" [--todo] [--tag t]... [--due YYYY-MM-DD] [--visible-at ISO] [--expires-at ISO] [--urgency 0-3] [--body B] [--audience L]...` generates a ULID, sets `source` to `adapter:cli` with `ref` equal to the id, and posts to `--url` (default `$DAYFILE_URL` or `http://127.0.0.1:8000`) with `$DAYFILE_WRITE_TOKEN` as the bearer, which must be a write token for `adapter:cli`. With `--db PATH` it instead writes straight into that database file and needs no token or server. `--due` is stored as `payload.due`. `--todo`, or `--due`, adds the tag `todo`.

### Email-in

`dayfile poll-email [--folder SMS] [--interval 60] [--no-idle] [--once] [--db PATH]` syncs Gmail over IMAP (read-only, `BODY.PEEK`) using `GMAIL_USER` and `GMAIL_APP_PASSWORD` from the environment or `.env`. By default it waits in IMAP IDLE (re-entered every 540 s, since Gmail drops idle sessions near 29 minutes) and syncs when the folder reports `EXISTS`/`EXPUNGE` or the wait ends, so SMS arrive within seconds. If the server refuses IDLE, or with `--no-idle`, it polls every `--interval`; `--interval` is also the reconnect delay after errors. The cursor `(UIDVALIDITY, last UID)` is saved in the `adapter_state` table after every sync, so a restart resumes where it stopped; if `UIDVALIDITY` changes the folder is rescanned from UID 0, which is safe because ingest is idempotent. IDLE is spoken over `imaplib`'s socket (imaplib gained `idle()` only in Python 3.14).

| Message | Event |
|---|---|
| `source` | `{id: "adapter:email-in", ref: sha256(Message-ID)}`; sha256 of the raw message if the header is missing |
| `Date` | `occurred_at` (current time if unparsable) |
| `Subject` | `title` (truncated to 300; `(no subject)` if empty) |
| text/plain part | `body` |
| subject prefix `todo:` (case-insensitive) | tag `todo`, prefix removed; otherwise no tag |

Google Voice forwards (sender at `txt.voice.google.com`) differ: the subject carries no content, so `title` is the first line of the SMS text, `body` is the whole text with the Voice link header and footer removed, `tags` is `["sms"]`, and `payload` is `{from_name, from_number}`. The `todo:` prefix is read from the SMS text.

### Local files and the bare address

A database file that `dayfile` creates is mode 0600 (owner only), since it holds private events and token hashes; an existing file keeps its mode. `GET /` on the core server redirects to `/docs` (307).

### CloudEvents (M8)

`GET /feed.cloudevents` (and `/t/<token>/feed.cloudevents`) exports the same filtered, audience-checked page as the other feeds, in the CloudEvents 1.0 JSON batch format (`application/cloudevents-batch+json`), with the next page in a `Link: <...>; rel="next"` header. `POST /events` also accepts `application/cloudevents+json` (one event) and `application/cloudevents-batch+json` (an array). Binary and other event formats are not supported.

| dayfile | CloudEvents |
|---|---|
| `id` | `id` |
| `source.id` (percent-encoded) | `source` |
| (none) | `type`: every export carries `app.dayfile.event` (CloudEvents requires a type; dayfile has none). A foreign event's `type` is imported as a tag (`com.acme.sensor` becomes `com-acme-sensor`). |
| `occurred_at` | `time` (a missing `time` becomes the import time) |
| `payload` | `data` with `datacontenttype: application/json`; non-object or non-JSON `data` is imported as `payload: {"data": ...}`, `data_base64` as `{"data_base64": ...}` |
| `source.ref`, `title`, `body`, `visible_at`, `expires_at`, `urgency` | `dayfilesourceref`, `dayfiletitle`, `dayfilebody`, `dayfilevisibleat`, `dayfileexpiresat`, `dayfileurgency` |
| `tags`, `audience` | `dayfiletags`, `dayfileaudience` (comma-delimited strings: extension values cannot be arrays) |
| `refs`, `links`, `sig` | `dayfilerefs`, `dayfilelinks`, `dayfilesig` (JSON-encoded strings) |
| `specversion` | `dayfilespec` (marks a dayfile-origin event) |
| `seq`, `recorded_at` | `dayfileseq`, `dayfilerecordedat` (export only, informational) |

Events carrying `dayfilespec` keep their identity on import (ULID and `source.ref`), so an export imported elsewhere round-trips exactly. Foreign events get a new ULID and use the pair (`source`, `id`) as `source.id` and `source.ref`, which is the CloudEvents definition of a unique event and makes re-delivery idempotent. A missing title falls back to `subject`, then `type`. Unknown extension attributes are dropped. Imported events still go through the normal validation and ingest rules.

### Generators

Generators write ordinary events back into the log; run them from cron. Both are idempotent per day through `source.ref` and skip their expensive step (the network fetch, the summarizer) when the day already exists.

- `dayfile gen weather --lat X --lon Y [--day YYYY-MM-DD] [--audience a,b]` writes a `weather.daily` event (`source: gen:weather`, `ref: <UTC date>`, visible now, expiring 24 hours later, tagged `weather`) from Open-Meteo (no key). `payload` is `{summary, high_f, low_f, precip_pct}`. The default audience is `["self"]`.
- `dayfile gen digest [--day YYYY-MM-DD] [--audience a,b] [--summarizer pkg.mod:func]` rolls the UTC day (by `visible_at`, released events only, expired ones included) into one `digest.daily` event (`source: gen:digest`, `ref: <day>:<sorted labels>`). It summarizes only events visible to the digest's own audience, so a digest never widens access, and never summarizes other digests. An empty day writes nothing. The default summarizer lists one line per event with its derived status; `--summarizer` takes any callable `(day, events) -> (title, body)`, which is where an LLM plugs in. `payload` is `{day, count, ids}`. A digest is written once; events released later the same day are not added to it, so run it after the day ends.

Planned adapters: SMS via an Android gateway app.

## 7. Notifications

Feeds are a poll channel. Urgent items need push. `dayfile notify --ntfy-url URL` (or `$NTFY_URL`; `--interval 30`, `--once`) runs a scheduler loop. On each tick it publishes every event that is **released** (`visible_at <= now`), **unexpired**, **open** (not retracted, done or superseded) and has `urgency >= 2`, and has not been delivered on that channel. Urgency 2 maps to ntfy priority 4 and urgency 3 to priority 5. The message is the `body`, falling back to the `title`; `tags` and the first `related` link become ntfy tags and click target. Header values are stripped of line breaks (RFC 2047 for non-ASCII), so an event title cannot inject headers. The topic URL is a secret and is never printed or logged.

Delivery state lives in the `deliveries` table (`seq`, `channel`, `status`, `created_at`), so the event log stays untouched. The scheduler **claims** a row (`pending`) in a transaction, publishes, then marks it `sent`. A publish failure deletes the claim so the next tick retries; a crash between claim and publish leaves it, so a restart never sends twice (at-most-once). `notify_state` records, per channel, the highest `seq` and the time at the first tick: events already released then are history and are never replayed, while events that arrive afterwards (even with an old `occurred_at`) or that were still waiting for release are delivered.

## 8. Signing

Authors may sign events with Ed25519. The signed bytes are the RFC 8785 canonical JSON of the event's **signing form**: the event after ingest normalization (UTC timestamps, `visible_at` and `audience` filled in), without `sig`, `seq`, `recorded_at` (and the derived `status` and `sig_status`). Fields equal to their default are omitted from the signing form: `urgency` 0, empty `tags`, `refs` and `links`, a null `expires_at`. That makes the form reproducible from a stored event, so anyone can verify it later. Array order matters. `sig` is `{kid, alg: "ed25519", value: base64(64-byte signature)}`.

- **Keys.** `dayfile key create [--kid K] [--key-dir DIR]` writes a private key (PKCS#8 PEM, mode 0600, in a 0700 directory, default `~/.config/dayfile/keys`; never overwritten) and registers the public key in the database's `keys` table. `dayfile key import --kid K --public BASE64` trusts someone else's key; `dayfile key list` shows ids and fingerprints. Key ids become file names and must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
- **Signing.** `dayfile add ... --sign KID` completes the event, signs it and posts it. Third-party signers must sign the completed form, not the form before defaults.
- **Ingest.** If `sig` is present and its `kid` is a trusted key, the signature must verify or the request fails with 422 and writes nothing. A `sig` with an unknown `kid` is stored unchanged.
- **Status.** Feeds expose `_dayfile.sig_status` (Atom `d:sig_status`) for signed events, computed at read time so registering a key later upgrades earlier events: `valid`, `invalid` (does not verify, or is malformed), `unknown-key`.
- **Guarantee.** Altering any signed field after signing fails verification; altering `recorded_at` or `seq` does not. Because those two sit outside the signature, the store can still reorder or withhold events without detection. A hash chain over `seq` closes that gap and is deferred.

## 9. Tenancy and limits (M12)

A deployment serves either one log (the default) or many, one SQLite file per tenant.

- **Single tenant.** Every host is served by the database in `$DAYFILE_DB` (or the `db_path` given to `create_app`). Nothing below applies.
- **Directory tenancy.** Setting `$DAYFILE_TENANTS_DIR` and `$DAYFILE_BASE_DOMAIN` serves `<slug>.<base-domain>` from `<root>/<slug>/dayfile.db`. The tenant comes from the `Host` header, never from a credential, and is resolved before authentication. A slug is 1 to 32 characters of `a-z`, `0-9` and `-`, starting and ending alphanumeric, and not one of a small reserved set (`www`, `api`, `app`, `admin`, `mail`, `status`, `dashboard`, `docs`). Tenants are created only by an operator (`dayfile tenant create`), never by a request.
- **Isolation.** Tokens, keys, cursors and events all live in the tenant's own file, so a credential minted for one tenant is unknown to every other. A request for an unknown host gets 404 `{"error": "unknown tenant"}`; the bare base domain is not a tenant.
- **`tenant.json`.** Optional file beside the database: `{"suspended": bool, "write_limit": [events, seconds] | null, "max_events": int | null}`. It is read on every request, so an operator or control plane can change it with no restart. An unreadable or invalid file fails closed as suspended.
- **Suspension** answers every request for the tenant, reads and writes, with 403 `{"error": "account suspended"}`. The data is untouched.
- **Quota.** A `POST /events` that would take the log past `max_events` is refused with 403 `{"error": "event quota exceeded"}` and writes nothing. Reads are unaffected. Redelivered duplicates count against the check even though they add nothing.
- **Storage quota.** `max_bytes` (settable like `max_events`, `dayfile tenant limit NAME --max-bytes N`) refuses a `POST /events` with 403 `{"error": "storage quota exceeded"}` once the database and its WAL files take that many bytes on disk. The check is made before the write, so a log can overshoot by one request (at most the body limit). Reads and export are unaffected. The free beta plan sets 25 MiB. Settings shows usage against it.
- **Write rate.** `write_limit` overrides the app default for that tenant, and windows are kept per `(tenant, token)`.
- **Read rate.** Each read token may make 300 requests per 60 s across `/events`, `/events/{id}`, the feeds and opening a stream (`read_limit`); more gets 429 `{"error": "read rate limit exceeded"}` with `Retry-After`. Conditional requests that return 304 count, since they still query. Requests that fail authentication spend nobody's budget, and the polling a stream or long-poll does internally is not charged again.
- **Held connections.** A token may hold at most 8 streams and long-polls (`wait` above 0) open at once (`held_limit`); more gets 429 `{"error": "too many open streams"}` or `"too many open long-polls"` with `Retry-After: 5`. A slot is released when the request ends, is revoked, or is dropped, and each lease also expires on its own (`wait + 5` s, `stream_seconds + 30` s) so a vanished client cannot hold a slot for good.
- **Scope of the limiters.** Budgets are in memory per process and reset on restart; they key on the tenant and token id, not on the client address, which is the reverse proxy's job.
- **Body size.** Any deployment refuses a `POST /events` body above 1 MiB (`max_body_bytes`) with 413, judged from `Content-Length` and again while streaming.
- **Deletion** moves the tenant directory to `<root>/.trash/<slug>-<time>/` and frees the name. Purging the trash is a separate, manual act, as are backups of it. `DELETE /tenants/{slug}?erase=true` removes the files at once with no trash copy; account deletion in the web app uses it.
- **Stats.** `dayfile tenant stats [--exclude SLUG]...` prints aggregate counts as JSON, never a name or any log content: `logs`, `unreadable`, `activated` (logs with at least one event from a user source), `multi_source` (two or more distinct user sources), and `week4` (`cohort`: logs whose first user event is at least 28 days old; `retained`: those of them with a user event in the last 7 days; `share`, or `null` with no cohort). Sources starting `dayfile:` (heartbeats) are the server's, not the user's, and are ignored. Times are `recorded_at`. Each database is opened read-only with an SQLite authorizer that allows reading only `events.seq`, `events.source_id` and `events.recorded_at`, so titles, bodies, payloads and tags cannot be read. The web app's `python -m dayfile.web.admin stats [--exclude SLUG]...` gives the account side: counts by status and sign-ups per UTC day for the last 30 days.
- **TLS hook.** `GET /internal/tls-ask?domain=<host>` returns 200 only for a live, unsuspended tenant hostname (404 otherwise, always an empty body), for a reverse proxy's on-demand certificate check. It is 404 for every host in single-tenant mode. A proxy must not expose `/internal/*` publicly.
- **`/usage`** (read token; private cache headers) returns `{events, max_events, bytes, max_bytes, write_limit}` for the caller's own log, with `null` for a limit that is not set. A write token is refused.
- **Backups.** `backup-tenants.sh` uploads every log's `<slug>.db.gz` and `<slug>.tenant.json`, and, when `ACCOUNTS_CMD` is set, the accounts database as `accounts.accounts.db.gz` (made by `python -m dayfile.web.admin backup`, which writes a verified gzipped online copy to stdout, because the web container cannot see the staging directory). `dayfile tenant verify-backup` checks both kinds; a failed accounts copy fails the run after the tenants are uploaded.
- **`/status`** (API; the public address is `https://dayfile.cloud/status`, also `www.`, where Caddy serves only this path and redirects the rest to the site) is the same check as `/healthz` with per-component detail: JSON `{status, components: {api, storage}, checked_at}`, 200 or 503, `no-store`. `status` is a reserved name, so no log can shadow it.
- **`/healthz`** needs no tenant: in directory mode it reports whether the tenant root is readable.
- **Icon.** `GET` or `HEAD /favicon.svg`, `/favicon.ico`, `/icon-512.png` and `/icon-64.png` return the dayfile icon on every host, with no credential and no tenant lookup (so also for a suspended tenant, and ahead of the 404 for an unknown one), `Cache-Control: public, max-age=86400`. The data endpoints stay closed. Feeds point at them: JSON Feed `icon` (512 px) and `favicon` (64 px), Atom `<icon>` (512 px), as absolute URLs on the feed's own host, never containing the token path.

Operator commands: `dayfile tenant create|limit|suspend|resume|delete|list|show|usage|path` (`--root` or `$DAYFILE_TENANTS_DIR`). `usage` prints every tenant's events, bytes on disk and limits as JSON for metering.

### Named feeds and public visibility (M17)

A log has a default feed, `/feed.json`, `/feed.atom`, `/feed.cloudevents` (also `/feed`, which is JSON Feed), and named feeds under `/f/<name>.json`, `.atom`, `.cloudevents` and `/f/<name>` (JSON Feed). With a path token: `/t/<token>/f/<name>...` and `/t/<token>/feed`. Names use `[a-z0-9-]`, 1 to 31 characters; `default` is the unfiltered default feed. A log holds at most 50 saved feeds (table `feeds`; the schema is at version 10).

A feed stores a title (at most 80 characters), a **filter** (any of `tag`, `tag_any`, `status`, `within`, validated like query parameters) and a **visibility**, `private` (default) or `public`. A named feed's filter is fixed: the request may add only paging parameters (`limit`, `since_seq`, `released_after`); any other parameter is 400. The default feed keeps its query parameters and has no stored filter, but its title and visibility can be saved under the name `default`.

- **Private** feeds need a read credential as before. An unknown name and a private feed both answer 401 to a caller with no credential, so existence is not revealed; with a valid read credential an unknown feed is 404.
- **Public** feeds answer an unauthenticated GET. An anonymous reader's audience is `{public}`: events appear only when they match the filter **and** their `audience` includes `public` (two locks). Responses carry `Cache-Control: public, max-age=60`, `Vary: Authorization` and an ETag over the anonymous identity. Only paging parameters are accepted. A request that carries a credential is never downgraded: a bad token is 401.
- **Credentialed readers** see their own labels **plus `public`** on every feed and on `/events`, `/events/{id}` and the stream: an event labeled `public` is visible to every reader. Write tokens are unaffected (they cannot read, and their labels still cap what they may grant).
- Anonymous reads share one budget per tenant, 120 per 60 seconds (`anon_read_limit`), because the proxy hides client addresses; a per-address limit needs the proxy's forwarded address to be trusted. Tokens keep their own budgets. Writes are never anonymous.
- Setting a feed's visibility is a control-plane act: `dayfile feed set NAME --public|--private`, or `PUT /internal/admin/tenants/{slug}/feeds/{name}` with `title`, `filter`, `visibility`. A change appends a `dayfile-admin` event ("Feed X published") to the log. `GET .../feeds` lists them; `DELETE .../feeds/{name}` removes one (the default cannot be deleted).

### Admin API (M16b)

A control plane (the web app) runs tenants through `/internal/admin/`, which the proxy never routes. It exists only when `$DAYFILE_ADMIN_SECRET_FILE` names a readable file and tenancy is per directory; every call needs `Authorization: Bearer <contents of that file>` (constant-time compare, 401 otherwise) and answers with `Cache-Control: no-store`. Mutating calls share a budget of 30 per minute (429 above it).

| Call | Effect |
|---|---|
| `PUT /tenants/{slug}` | create with optional `max_events`, `write_limit` `[events, seconds]` (201), or report the existing tenant unchanged (200) |
| `GET /tenants/{slug}` | slug, events, bytes, limits, suspended |
| `POST /tenants/{slug}/suspend` and `/resume`, `DELETE /tenants/{slug}` | lifecycle (204; delete moves data to `.trash/`) |
| `POST /tenants/{slug}/tokens` | body `kind` (`read` or `write`), `labels` (default `["self"]`), `write_source` (required for write), `expires_in` seconds (60 to 366 days), `note`; 201 with `id` and `token` (shown once) |
| `GET /tenants/{slug}/tokens` | id, kind, labels, sources, note, dates, `last_used`; never secrets |
| `DELETE /tenants/{slug}/tokens/{id}` | revoke (204, repeatable) |

A tenant may hold 20 live tokens; tokens that expire within an hour are session plumbing and neither count nor appear in the log. Every other creation and revocation appends a `dayfile-admin` event (source `dayfile`, tag `dayfile-admin`, no secrets) to the tenant's own log. Tokens record `last_used` (table `token_use`, schema version 8), updated at most once an hour.

## 10. Web app (M15)

`dayfile.web` is a separate FastAPI app (`uvicorn dayfile.web:app`) for the site at `dayfile.app`. It stores nothing and never opens a tenant file; the cloud profile runs it in its own container with no volumes. It reaches a log only through the public API, by a fixed internal address (`$DAYFILE_API_URL`) and a `Host: <name>.<$DAYFILE_BASE_DOMAIN>` header, so the only visitor-controlled part of that request is a tenant name already checked against the tenant-name pattern. Redirects from the API are not followed, answers are capped at 4 MiB, and calls time out after 10 seconds.

- **Pages.** `/` (landing; a visitor with a session cookie is redirected with 303 to `/account`, or to `/log` for a token session, `no-store`), `/docs` (the docs, opening with a diagram of log, tokens and feeds that the landing page also shows, then a concepts glossary; linked only from the footer, on every page, signed in or not), `/status` (a readable page; it asks the API's `/status` on each load and answers 503 when the API is not ok, and shows 24 hour, 7, 30 and 90 day uptime, a bar per day and the last ten incidents per monitor from UptimeRobot's read-only API (key in `$DAYFILE_UPTIMEROBOT_KEY_FILE`, `/run/secrets/uptimerobot_key`; cached five minutes; credited on the page, which links to their public status page given in `$DAYFILE_STATUS_PAGE_URL`). Without the key it shows an internal check, labeled as such, from `status.db` beside the accounts database (`$DAYFILE_STATUS_DB`), where the web app records one probe of the API a minute and keeps 90 days), `/terms`, `/privacy` (public content, cached by the browser only, `private, max-age=60` with `Vary: Cookie`, because the header shows Sign in, Your log or the account bar depending on the visitor's session; the account bar needs a live, finished account session, checked once per request, so a stale session cookie gets Sign in), `/login`, `/log`, `/log/new`, `/healthz`, `/robots.txt` (keeps `/log` and `/login` out of search), the icons, and `/static/`. Terms and privacy are drafts and say so. `GET /login` redirects (303) to `/log` for a visitor who is already signed in (an account session or a read-token cookie), except with `?expired=1` or `?connect=1`, which always show the form so a dead session cannot loop. A poll (`/log/new`) whose read session lapsed answers 401 with `HX-Redirect: /log` for a signed-in account (the log mints a fresh viewer token) and `/login?expired=1` otherwise.
- **Sign-in.** `POST /login` takes a log name and a read token, or a pasted feed URL (`https://<name>.<base>/t/<token>/feed.json|atom`, no port or userinfo, which also names the log). It calls `GET /events?limit=1`: 200 starts a session, 401 and 404 give the same "don't match" answer (401), 403 says the token cannot read (a write token), 429 and failures to reach the API are reported as such (429, 503). Attempts are limited to 30 per 60 seconds for the whole process, counted before the API is called.
- **Session.** One cookie, `__Host-dayfile` (`dayfile` when cookies are not secure, for local use), holding `<name>:<token>`; `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, no `Domain`, 8 hours. Its value is validated against the tenant-name and token patterns on every request. The token is never written into a page or a URL. `POST /login` and `POST /logout` must come from the site's own origin (`Origin` equal to `$DAYFILE_SITE_ORIGIN`, or `Sec-Fetch-Site: same-origin`), else 403. A token the API stops accepting (revoked, expired) clears the cookie and returns the visitor to `/login?expired=1`. A signed-in visitor who reaches `/log` from another site (a link click: `Sec-Fetch-Site` is not `same-origin` or `none`) carries the account session but not the `SameSite=Strict` log cookie. A GET never starts a viewer for them, so `/log` answers with a page holding one button that posts to `/account/view`; sending them to `/account` would loop, since that page sends an account with a log back to `/log`.
- **Viewer.** `GET /log` lists the newest events, newest first, through `GET /events` with `tag` (the schema's tag pattern), `status` (`open`, `closed`, `none`, `retracted`, or `all`; default: everything current) and `limit` (50, 100 or 200). An invalid filter is dropped and reported. `GET /log/new?after=<seq>&released=<time>` returns what arrived since, as an HTML fragment for htmx to swap in every 5 seconds, by calling `GET /events?since_seq=...&released_after=...`, so late-released events arrive too and each event arrives once. A session that ends mid-poll answers 401 with `HX-Redirect: /login?expired=1`; any other failure keeps the same cursors.
- **Untrusted content.** Event text comes from outside, so every field is output through auto-escaping templates; links become clickable only for `http`, `https` and `mailto` with no whitespace or control characters (anything else is shown as inert text); payloads are shown as escaped, truncated JSON.
- **Headers and scripts.** Every response carries `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`. Pages use no inline script or style and load nothing from other sites; the one script is a vendored, hash-checked htmx 2.0.4 (`src/dayfile/web/static/htmx.NOTICE`), configured not to use `eval` or inline styles. Signed-in pages are `Cache-Control: no-store`.
- **Not in M15.** Accounts, GitHub sign-in, invite codes, token management and billing (M16).

### Single-log mode (self-hosting)

With `DAYFILE_SINGLE_LOG=1` the web app serves one self-hosted log (the API runs without tenants at `$DAYFILE_API_URL`). Sign-in takes only a read token, or a pasted feed URL of any host (`/t/<token>/feed.json|atom` or a named feed), and the log goes by the fixed name `log` inside the app; no `Host` is meaningful to the API. `/` redirects to `/log` (or `/login`). Accounts, GitHub sign-in, posting from the page and the pages about the hosted service (`/demo`, `/pricing`, `/terms`, `/privacy`, `/docs`, `/status`) are absent (404; `/healthz` stays), and the footer drops their links. A POST is same-origin when `Origin` matches the request's own `Host` (there is no configured public address); the Fetch Metadata fallback is unchanged. `DAYFILE_INSECURE_COOKIES=1` drops the `Secure` flag and the `__Host-` prefix for plain http outside localhost. The viewer, its filters and the live fragment are otherwise the hosted ones.

### Accounts (M16c)

On when `$DAYFILE_ACCOUNTS_DB`, `$GITHUB_CLIENT_ID` and the file `$DAYFILE_GITHUB_SECRET_FILE` are all set; otherwise the routes do not exist and the login page offers only the read-token form. See `docs/design/m16-accounts.md`.

- **Flow.** `GET /auth/github` (a link, not a form post) makes a 256-bit state and a PKCE verifier, stores the state's hash, sets `__Host-dayfile-oauth` (`HttpOnly; Secure; SameSite=Lax; 10 minutes`) and redirects to GitHub with no scope (public id and login only). `GET /auth/github/callback` requires the cookie to equal the `state` parameter and the stored state to be unused and under ten minutes old, exchanges the code with the verifier, reads `id` and `login`, and discards GitHub's access token. It answers 200 with a small page that links onward, not a redirect (a `SameSite=Strict` cookie would be lost on a redirect chain that began on another site). Identity is the numeric GitHub id; the login is refreshed at each sign-in. GitHub is reached at two fixed hosts, with no redirects and capped responses.
- **Sessions.** `__Host-dayfile-session` (`HttpOnly; Secure; SameSite=Lax`) holds an opaque 256-bit id; only its hash is stored. Idle limit 8 hours, absolute 30 days; sign-out and suspension delete sessions on the server. A session is *pending* (a GitHub identity with no account yet) or full. The M15 read-token session (`__Host-dayfile`) is separate and unchanged.
- **Signup.** A pending identity accepts the terms at `/signup`, and also enters an invite code unless signup is open (`DAYFILE_SIGNUP=open`; the cloud compose file sets it, the default is `invite`). Codes are 12 base32 characters, stored only as a hash, single-use by default, redeemed with the account creation in one transaction. An unknown, used, expired or revoked code gives the same message. Attempts are limited to 5 per identity per 5 minutes and 20 per minute overall, and at most 60 accounts are created per hour. The accepted terms version and time are stored. The pending session is replaced by a full one.
- **Roadmap interest (launch discovery).** Behind `DAYFILE_DISCOVERY=on`, `/pricing` has a *Roadmap* section: three things that do not exist yet (alerts when something does not run, share a view with a client, team logs), each marked *not available yet*, as checkboxes in one form with one button. `POST /pricing/interest` (same origin, form post, works without script) takes the ticked `feature` values. Signed in, the set replaces the account's rows in `feature_interest` (one row per account and feature, no email; unchanged ones keep their time; ticking none clears them). Signed out, each ticked feature gets a row with its time, plus an address only when the consent box is ticked and the address is plausible (the same wording and version as the contact email; the same address for the same feature is one row); at least one box must be ticked; no cookie is set and no IP address is kept. It is limited in memory to 30 requests a minute for everyone. An account's rows are in the account details and deleted with the account; signed-out rows are not tied to anyone. Admin `stats` gives per feature counts split signed in and signed out (numbers only); `--list-emails` adds `interest_emails`. With the flag off the section is absent and the endpoint records nothing. `/pricing` is `no-store` when the flag is on, because the form shows the visitor's own choices.
- **Long bodies.** In the log reader a body of more than 700 characters or 12 lines is clamped to about 13 lines with a fade and a "Read more" / "Show less" control (a hidden checkbox and CSS, no script); the whole text is in the page and in the feeds. A payload is shown collapsed and cut at 2,000 characters.
- **Signed-in layout.** One username = one log = one address (named feeds are views of it; several logs per account is a later paid feature). The header shows `Log · Feeds · Tokens · Settings`; Sign out is a button on the Settings page. `/account` is the home: it redirects to `/log` once the username is chosen, else shows the form to choose it. `GET /log` for an account session with no viewer cookie makes a one-hour read token and sets the cookie (only when `Sec-Fetch-Site` is same-origin or none), redirecting to `/log?fresh=1`; a lapsed viewer token is remade once; a failure right after `fresh=1` ends at the sign-in page. `/account/settings` has, in order: *Account management* (the GitHub identity and sign out, the username, show the account details), *Log management* (usage, download the log, start a fresh log), *Contact and feedback* (the email and the plans answer, when the discovery flag is on or they exist) and, after a red rule, *Delete account*.
- **Posting from the site.** A Post button on the Log page opens a dialog (a small script, `/static/log.js`; without scripts the button is a link to the page `GET /log/post`) with the form for account sessions: `POST /log/post` (same origin) sends `title`, optional `body`, `tags` and `state` (none, or open, which makes the event a `todo`) to the session account's own log through the API with a short-lived (1 h) write token limited to the source `web`, kept in an HttpOnly cookie and remade when it lapses. The token is hidden from the token list like the viewer token.
- **Details as Markdown.** The web Log renders an event's `body` as a small, safe subset of Markdown: paragraphs (a single newline is a line break), `#` to `###` headings (shown as small headings under the title), `-` and `1.` lists, `>` quotes, fenced code, `**bold**`, `*italic*`, `` `code` `` and `[text](url)`. Everything is HTML-escaped first, so a sender cannot add markup or script, and only http, https and mailto links become links. Feeds and the API still carry the body as plain text. A card's footer reads `source: <id>, tags: <tag> <tag>` on one line (the tags part only when there are tags).
- **Export and delete (M16e).** Settings has *Download my log* (`POST /account/export`, same origin, 5 per 5 minutes): the web app asks the admin API (`GET /internal/admin/tenants/{slug}/export`) for a gzip of a consistent copy made with SQLite's online backup and checked with `PRAGMA integrity_check`, and returns it as an attachment, `no-store`. *Delete account* (`POST /account/delete`) needs the username typed exactly: it moves the tenant to `.trash/` through the admin API (404 counts as already done, so a retry after a half-finished delete works), then marks the account `deleted` (`deleted_at` set, slug cleared, the name held one day, sessions destroyed, an `account.delete` audit row), and shows a goodbye page. If moving the log fails, nothing else changes. The account row stays for the audit log but gives up its GitHub identity (id replaced by a negative placeholder, login by `deleted`), so the same GitHub account can sign up again as a new account; the username is held for a day. `dayfile tenant purge --older-than-days 30` removes `.trash/` entries by the stamp in their name; a weekly systemd timer runs it. Terms and privacy say: stopped at once, live copy deleted within 30 days, encrypted backups (14 days) age out within 14 days after that.
- **Sign-up question (launch discovery).** Off unless `DAYFILE_DISCOVERY=on` (hosted only; self-hosted instances never ask). The username step (`/account`) then shows an optional text area, *What are you planning to log?*; skipping it changes nothing. The answer is stored in `signup_answer` (one row per account: text, `answered_at`) only after the log is created; it is made plain text first (CRLF to LF, C0/C1 controls other than tab and newline and bidi overrides removed, trimmed) and must be at most 500 characters, else the step is refused before the name is taken and the text is shown again. Settings shows it under *Contact and feedback*, with the email, as *What you plan to log*; `POST /account/answer` (same origin) replaces it, and a blank one removes it. With the flag off, Settings shows the section only if an answer exists, and the endpoint can only remove. *Show my account details* on Settings opens a box with the account's own JSON (formatted, with a Copy button that needs script and is hidden without it; there is no download endpoint), holding the account's GitHub id and login, username, sign-up and terms times, the answer and the contact email. Deleting the account deletes the answer in the same transaction. `python -m dayfile.web.admin stats` counts accounts with an answer; `--list-answers` adds the texts and times, with no account attached.
- **Contact email (launch discovery).** Behind the same `DAYFILE_DISCOVERY=on`. The username step also offers *Email (optional)* and an unticked box: *The developer may email me about dayfile: service changes and one message asking how it's going.* The address is stored only with the box ticked; an address without the tick, or one that is not plausible (one `@`, a dotted domain, no spaces or control characters, at most 254 characters), refuses the step before the name is taken and shows the form again; a tick without an address stores nothing. The user types it; the GitHub scope is not widened. `contact_email` holds one row per account: the address (domain lower-cased), the consent wording's version (`2026-11-email-1`, bumped whenever the words change) and when consent was given. Settings shows it and when consent was given; `POST /account/email` (same origin) replaces it, again only with the box ticked, and `remove=yes` deletes it, which withdraws consent; with the flag off it can only remove. The account details include it, and deleting the account deletes it in the same transaction. Admin `stats` counts consenting addresses of active accounts (`with_contact_email`); `--list-emails` adds the addresses, their consent version and time. Nothing sends email.
- **Example log.** `GET /demo` (public) renders a fixed set of sample events, dated relative to now, through the same card template, with the tag and status filters working on them; it reads no tenant data. An account's empty `/log` shows a three-step first-event panel that posts to `/account/tokens` for a write token.
- **Housekeeping events.** The records the service writes to a log (token created, feed published, rename; tag `dayfile-admin`) are kept as an audit trail but left out of every feed, read and the web Log unless asked for with `tag=dayfile-admin` (and out of daily digests); an exact-id lookup still finds them.
- **Closing events.** In an account session each open event on the Log page has a Close button, and the History panel holds the Retract button. They post `POST /log/event/{id}/close` or `/retract` (same origin, ULID-shaped id, nothing else accepted), which writes a new event with no title (inherited from the original) and a `completes` or `retracts` ref to the original, using the same `web` write token as posting. The ref takes effect under the normal authorization rule (same source, or a shared audience label such as `self`). A read-token session shows no buttons. The web log does not show the closing events themselves (any event with a `completes`, `retracts` or `supersedes` ref): the original's own card carries the state, and retracting a closing event makes the original show its earlier state again. The API and feeds still list them. **History.** A card with closing events has a collapsible History listing the chain (a card also has one, with just its Retract this event button, when nothing has closed it yet, and that is the only place to retract an event): each step (closed, replaced, retracted) with its time and short id, struck through with "undone" when a later step has reversed it, and, for an account session, an Undo button that retracts that step; steps that undo a step nest beneath it. The status on the card is the API's derived status, which already follows the whole chain, so undoing an undo restores the earlier result. The chain is built from one extra read of up to 200 events newer than the oldest card shown, so on a very busy log older steps may be missing; the fragment that adds new events by polling does not carry history until the page is reloaded.
- **Account pages.** `POST /account/logout` requires the same origin. Every account page is `no-store` under the same CSP; `/auth`, `/signup` and `/account` are disallowed in `robots.txt`.
- **Dashboard (M16d).** A signed-in account without a log is asked for a name at `/account` (`POST /account/slug`): 3 to 32 characters, `[a-z0-9-]`, not reserved (platform, mail and infrastructure words), unique, and changeable later (see Renaming). The name is claimed in `accounts.db`, then the tenant is created through the admin API with the free-beta plan (50,000 events, 60 writes per 60 seconds); if creation fails the claim is released. `/account/tokens` lists live tokens (never secrets), creates a read or write token (a write token's source defaults to the log name; expiry 30, 90 or 365 days; shown once on a `no-store` page) and revokes with a confirmation page. `/account/feeds` lists, creates, edits (the default feed too) and deletes feeds and sets each private or public. `POST /account/view` makes a one-hour read token for the viewer. Every call goes to the admin API for **the session account's own log**, never a name from the URL or form, so an account cannot reach another's tokens or feeds. Every POST needs the same origin. Tokens whose note is "dashboard viewer" are hidden from the list.
- **Heartbeats (schema 11).** A heartbeat (`heartbeats` table: name, `every_seconds` 300 to 90 days, and exactly one of a `source` id or a `tag`) is a setting, like a feed. `dayfile heartbeat check` (every minute by a systemd timer; `--root`/`DAYFILE_TENANTS_DIR` for all live tenants, or `--db`) compares each with the log, using `recorded_at` of the newest visible matching event (or the heartbeat's `updated_at` if none). Past the interval plus a grace of a tenth of it (at least a minute) it appends one event per lapse: source `dayfile:heartbeat`, `source.ref` `name@baseline` (so repeated checks are no-ops), tags `todo` and `heartbeat`, urgency 2, payload `{heartbeat, expected_every, last_seen}`. When the source is heard from again, or the heartbeat is deleted, it appends a `completes` event for each open one. Tag heartbeats ignore the heartbeat's own events. Admin API: `GET /tenants/{slug}/heartbeats`, `PUT|DELETE /tenants/{slug}/heartbeats/{name}` (body `{every, source|tag}`); web: the Heartbeats tab of the Feeds page (`/account/feeds/heartbeats`). Notification is the log itself: any reader, feed or MCP client sees an open to-do tagged `heartbeat`; `dayfile notify` pushes it where ntfy is set up (urgency 2). Writes bypass the event quota.
- **Full logs and starting fresh.** From 90% of `max_events` or `max_bytes` the Log page shows a notice; at the limit a post fails with "Your log is full" and points to Settings. *Start a fresh log* (`POST /account/reset`, same origin, username typed, 3 per 5 minutes) erases the log's files through the admin API (`DELETE ...?erase=true`) and creates it again, empty, with the plan's limits under the same name; every token and feed goes with it, and the viewer and post cookies are cleared. A failed second step is safe to retry.
- **Renaming.** `POST /account/rename` (same origin, an "I understand" box) changes the username: the new name is validated like a first choice and must be free (no live account, no existing tenant, not held by someone else), then the tenant directory is renamed through the admin API (`POST .../rename`, 409 if taken) in one atomic step and the move is logged in the log's own feed. Tokens keep working on the new address; the old address answers 404 at once and is reserved for the former owner for **one day** (`slug_hold`), who may take it back. At most three renames per 30 days. If the move fails the change is undone. A short hold means an abandoned address can be claimed by someone else after a day, so published feed URLs should not be reused.
- **Data.** The web container's only mounts are its accounts directory (read-write) and a read-only secrets directory; it still has no tenant files.

### Assistants: MCP (`dayfile mcp`, `/mcp`)

Two ways to let an AI assistant read and write a log, both exposing the same five tools: `dayfile_list_events`, `dayfile_get_event`, `dayfile_add_event`, `dayfile_close_todo` and `dayfile_retract_event`. The tools only call the ordinary API as the caller, so every permission, limit and quota applies unchanged, and nothing is deleted (closing and retracting append events).

- **Local.** `dayfile mcp` (extra `dayfile[mcp]`) speaks MCP on stdio and calls the API at `$DAYFILE_URL` with a read token in `$DAYFILE_READ_TOKEN` and a write token in `$DAYFILE_WRITE_TOKEN`. Leave the write token out for a read-only assistant.
- **Hosted.** With `DAYFILE_MCP=1` every log serves MCP (streamable HTTP, stateless, JSON responses) at `https://<username>.dayfile.cloud/mcp`. The credential is `Authorization: Bearer <read>~<write>`: a read token, then `~` and a write token. The second part is optional; without it writes are refused ("this connection is read-only"). `~` is not in a token's alphabet. The read part must be a live read token (a write-only token is a 401). Tool calls run in-process as the caller. A missing or bad credential is a 401 whose `WWW-Authenticate` carries `resource_metadata`, the URL of `/.well-known/oauth-protected-resource/mcp` (RFC 9728), which names `DAYFILE_OAUTH_ISSUER` as the authorization server; without an issuer only hand-made credentials work.
- **One address for everyone.** On a multi-log server the bare base domain also serves `/mcp` and its metadata (`https://dayfile.cloud/mcp`; the proxy routes only those two paths there). The bare domain names no log, so the credential does: `Authorization: Bearer <username>:<read>~<write>`. The username (`[a-z0-9-]`, no `:` appears in a token) picks the log, and the token must still authenticate in that log's own database; an unknown or reserved username, or a token from another log, is the same 401 `invalid_token`, and a suspended log is 403. Tool calls then run in-process as the caller against that log's address. A per-log address accepts the same credential if the username is its own log, and still accepts a bare `<read>~<write>`. A single-log server has no such address.

**Authorization server (dayfile.app).** OAuth 2.1 for public clients, so an assistant can connect with a "Connect" button instead of pasted tokens.

- `GET /.well-known/oauth-authorization-server` (RFC 8414): authorization code with S256 PKCE (required), `none` client authentication, refresh tokens, `iss` on responses (RFC 9207).
- `POST /oauth/register` (RFC 7591): public clients only. Redirect URIs must be https, or http on a loopback address (any port); no fragments, credentials or odd hosts. Limited to 60 registrations an hour and 5,000 clients in all; unused clients expire after 30 days.
- `GET /oauth/authorize`: needs a signed-in account with a log; anyone else is sent to sign in and returned afterwards (cookie `dayfile-return`, 10 minutes). It shows who is asking, which log, where the browser returns, and a choice of read and write or read only. The page's `form-action` allows only the client's redirect origin. An unregistered client or a redirect it did not register never redirects. A `resource` parameter, if sent, must be this account's own `/mcp` URL or the one address for everyone (`https://<base domain>/mcp`). The credential issued by `/oauth/token` is `<username>:<read>~<write>` (the write part only for read and write), so it works at both addresses.
- `POST /oauth/token`: `authorization_code` (the code lives 5 minutes, works once whatever the outcome, and is bound to its client, redirect URI and PKCE verifier) and `refresh_token`. The answer holds a credential valid for an hour (a read token, plus a write token if allowed, both ordinary short-lived tokens in the log with a note starting `connected app:`) and a refresh token.
- **Refresh tokens rotate.** Each use issues a new one and revokes the previous credential. Using a replaced refresh token again ends the whole connection and revokes its tokens. A refresh token slides for 60 days and the connection ends after a year. A deleted or suspended account cannot refresh.
- Connections appear under Tokens as "Connected assistants" with a Disconnect button, which revokes their tokens at once; deleting the account removes them. State lives in the accounts database (`mcp_client`, `mcp_code`, `mcp_grant`; secrets only as hashes).

## 11. Open questions

1. **Audience negation.** Flat labels cannot say "everyone except X". Decide before the first shared deployment.
2. **Encrypted items.** Whether `tags` and `visible_at` stay in the clear when `title`, `body`, and `payload` are encrypted per audience.
3. **CloudEvents mapping.** Offered as export and ingest for the JSON structured and batch formats (see CloudEvents above). Open: binary mode, `dataschema`, and whether unknown extension attributes should be kept somewhere instead of dropped.
4. **Hash chain** for tamper-evident ordering.
5. **Schema registry** for payload types, versus opaque payloads with convention.
6. ~~**Time release versus `seq` cursors.**~~ Resolved with `released_after` / `_dayfile.released_cursor` (see Query). The Atom feed carries `<d:cursor>` and `<d:released_cursor>` (namespace `urn:dayfile:1`) as well as `next` links. A follower that loses its `released_cursor` can restart from `since_seq=0`.
7. ~~**Reversing a lifecycle event.**~~ Decided: retracting re-opens, and the earliest event wins a cycle (see Lifecycle).
