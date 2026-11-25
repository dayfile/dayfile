-- dayfile storage schema (SQLite)
--
-- Connection setup (db.py must run these on every connection):
--   PRAGMA foreign_keys = ON;
--   PRAGMA journal_mode = WAL;
--
-- Timestamps are TEXT in one fixed format: YYYY-MM-DDTHH:MM:SSZ (UTC).
-- Normalize at ingest. The CHECK constraints below reject anything else,
-- which keeps string comparison equal to chronological comparison.

CREATE TABLE events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,      -- server-assigned log position
  id          TEXT NOT NULL UNIQUE,                   -- ULID, author-assigned
  source_id   TEXT NOT NULL,                          -- adapter or device id
  source_ref  TEXT,                                   -- external id, for idempotent ingest
  occurred_at TEXT NOT NULL
    CHECK (occurred_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
  visible_at  TEXT NOT NULL
    CHECK (visible_at  GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
  recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    CHECK (recorded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
  expires_at  TEXT
    CHECK (expires_at IS NULL OR expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
  urgency     INTEGER NOT NULL DEFAULT 0 CHECK (urgency BETWEEN 0 AND 3),
  title       TEXT NOT NULL,
  body        TEXT,
  payload     TEXT CHECK (payload IS NULL OR json_valid(payload)),
  links       TEXT CHECK (links   IS NULL OR json_valid(links)),
  sig         TEXT CHECK (sig     IS NULL OR json_valid(sig)),
  UNIQUE (source_id, source_ref)
);

CREATE TABLE event_tags (
  seq INTEGER NOT NULL REFERENCES events(seq),
  tag TEXT NOT NULL,
  PRIMARY KEY (seq, tag)
);

-- Audience labels. Ingest writes 'self' when the event carries none.
CREATE TABLE event_audience (
  seq   INTEGER NOT NULL REFERENCES events(seq),
  label TEXT NOT NULL,
  PRIMARY KEY (seq, label)
);

-- Lifecycle graph. rel is one of: completes, supersedes, retracts, replies-to.
CREATE TABLE event_refs (
  seq       INTEGER NOT NULL REFERENCES events(seq),
  rel       TEXT NOT NULL CHECK (rel IN ('completes', 'supersedes', 'retracts', 'replies-to')),
  target_id TEXT NOT NULL,                            -- may reference an id not yet ingested
  PRIMARY KEY (seq, rel, target_id)
);

CREATE INDEX ix_events_visible ON events (visible_at);
CREATE INDEX ix_tags_tag       ON event_tags (tag, seq);
CREATE INDEX ix_aud_label      ON event_audience (label, seq);
CREATE INDEX ix_refs_target    ON event_refs (target_id, rel);

-- Append-only enforcement.
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER tags_no_update BEFORE UPDATE ON event_tags
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER tags_no_delete BEFORE DELETE ON event_tags
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER aud_no_update BEFORE UPDATE ON event_audience
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER aud_no_delete BEFORE DELETE ON event_audience
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER refs_no_update BEFORE UPDATE ON event_refs
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER refs_no_delete BEFORE DELETE ON event_refs
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

-- BEGIN UPGRADABLE (re-applied by db.connect when user_version is older than the schema's).
-- Holds only objects that can be dropped or created idempotently: side tables written by
-- processes other than ingest, and views. Never put event tables or triggers here.
--
-- Notification delivery state. Mutable by design: the event log stays untouched.
-- A row is claimed ('pending') before publishing and marked 'sent' afterwards; a failed
-- publish releases the claim so the next tick retries, a crash leaves it (at-most-once).
CREATE TABLE IF NOT EXISTS deliveries (
  seq        INTEGER NOT NULL REFERENCES events(seq),
  channel    TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN ('pending', 'sent')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (seq, channel)
);

-- Trusted public keys for signature verification. Private keys never enter the database.
CREATE TABLE IF NOT EXISTS keys (
  kid        TEXT PRIMARY KEY,
  alg        TEXT NOT NULL DEFAULT 'ed25519' CHECK (alg = 'ed25519'),
  public_key TEXT NOT NULL,                           -- base64 of the 32 raw bytes
  note       TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Write credentials. A token with a row here may POST events, but only as its `source_id`s, and
-- only with audiences inside its own labels (token_audience). Write tokens cannot read feeds.
CREATE TABLE IF NOT EXISTS token_write (
  token_id  TEXT NOT NULL REFERENCES tokens(id),
  source_id TEXT NOT NULL,
  PRIMARY KEY (token_id, source_id)
);

-- Small key/value state for adapters (e.g. the IMAP UID cursor). Mutable by design.
CREATE TABLE IF NOT EXISTS adapter_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Where a channel started. History released before this point is never replayed.
CREATE TABLE IF NOT EXISTS notify_state (
  channel   TEXT PRIMARY KEY,
  floor_seq INTEGER NOT NULL,
  init_at   TEXT NOT NULL
);

--
-- Lifecycle edges: refs that may change their target's state. An edge exists only
-- once the target is in the log, so a ref to a late-arriving id takes effect when
-- the target appears. It is authorized when the referencing event has the same
-- source_id as the target, or shares an audience label with it. Edges carry the
-- referencing event's visible_at so readers can ignore edges that are not yet released.
-- Over HTTP the writer's source_id and audience are bound to its credential (token_write),
-- so these two conditions cannot be forged by a remote writer.
CREATE VIEW lifecycle_edges AS
SELECT r.target_id, r.rel, r.seq AS ref_seq, s.id AS ref_id, s.visible_at AS ref_visible_at
FROM event_refs r
JOIN events s ON s.seq = r.seq
JOIN events t ON t.id = r.target_id AND t.seq <> s.seq
WHERE r.rel IN ('completes', 'supersedes', 'retracts')
  AND (s.source_id = t.source_id
       OR EXISTS (SELECT 1 FROM event_audience sa
                  JOIN event_audience ta ON ta.label = sa.label
                  WHERE sa.seq = s.seq AND ta.seq = t.seq));

-- When each token last authenticated, updated at most hourly (see auth.tokens.verify_token).
CREATE TABLE IF NOT EXISTS token_use (
  token_id  TEXT PRIMARY KEY,
  last_used TEXT NOT NULL
);

-- Named feeds: a saved filter plus visibility. Mutable settings, not events. The name 'default' is
-- the unfiltered feed served at /feed.json. filter is a JSON list of [name, value] query pairs.
CREATE TABLE IF NOT EXISTS feeds (
  name       TEXT PRIMARY KEY,
  title      TEXT,
  filter     TEXT NOT NULL DEFAULT '[]',
  visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Heartbeats: how often a source id or a tag should be heard from. Settings, not events; see
-- heartbeats.py, which writes ordinary events when the cadence lapses.
CREATE TABLE IF NOT EXISTS heartbeats (
  name          TEXT PRIMARY KEY,
  every_seconds INTEGER NOT NULL CHECK (every_seconds BETWEEN 300 AND 7776000),
  source        TEXT,
  tag           TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  CHECK ((source IS NULL) != (tag IS NULL))
);

-- Derived state is computed by db.refresh_live_edges (which resolves retractions of lifecycle
-- events and cycles, with the clock) into temp.live_edges; see db.STATE_CASE.
-- Short ids: close and retract accept the last six or more characters of an event id, and this
-- index finds the event without scanning the log (see ingest.core.expand_short_refs).
CREATE INDEX IF NOT EXISTS ix_events_tail ON events (substr(id, -6));
-- END UPGRADABLE

PRAGMA user_version = 12;

-- Access tokens. Tokens are mutable records (revocation), unlike events.
-- Store only a hash of the secret. The scope is the set of audience labels.
CREATE TABLE tokens (
  id          TEXT PRIMARY KEY,                       -- public id, safe to log
  secret_hash TEXT NOT NULL,
  note        TEXT,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  expires_at  TEXT,
  revoked_at  TEXT
);

CREATE TABLE token_audience (
  token_id TEXT NOT NULL REFERENCES tokens(id),
  label    TEXT NOT NULL,
  PRIMARY KEY (token_id, label)
);
