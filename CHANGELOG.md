# Changelog

## 0.2.2

- Short ids: close and retract take the last six (or more) characters of an event id, as `dayfile close pyz7d2` or `POST /events/pyz7d2/close`, in the API, CLI and assistant tools. Case is ignored; a tail that matches no event or more than one is refused. A new index (schema 12) keeps the lookup fast.
- Web view: click an event's ID badge (or press Enter on it) to copy its short id, Shift-click for the full id.
- Web view: the page now follows the log live more faithfully. "Nothing here yet." goes away when the first event arrives, and when something is closed, replaced or retracted the page reloads so the card shows its new status.
- README: a short clip of the web view at the top.

## 0.2.1

- Closing a to-do, or retracting a mistaken event, can say why: `POST /events/{id}/close` and `/retract` with an optional `title` and `body`, `dayfile close` and `dayfile retract` with `--note`, an optional `note` on the assistant's `dayfile_close_todo` and `dayfile_retract_event`, and an optional *Why?* field on the web Close button. History shows the note.
- Docs: only `title` is needed to post; the server fills in the rest. The self-host README now goes from trying it, to running it by hand, to systemd and Docker, and explains `DAYFILE_DOMAIN`.
- `try-web-view.sh` no longer adds events by itself; it prints the command to add one.

## 0.2.0

- Self-hosting: an optional read-only web view of your one log. Set `DAYFILE_SINGLE_LOG=1` on the web app (`pip install "dayfile[web]"`, then `uvicorn dayfile.web:app`); sign in with a read token. The Docker setup in `deploy/selfhost` starts it behind Caddy, and `dayfile-web.service` runs it under systemd. `DAYFILE_INSECURE_COOKIES=1` is for plain http outside localhost. `deploy/selfhost/try-web-view.sh` tries it in one command with sample events and removes everything on exit.

## 0.1.5

- Docker: the container trusts the proxy in front of it, so feed links (`feed_url`, the Atom self link) say `https://` behind Caddy instead of `http://`.
- Self-hosting: the systemd units run from a virtualenv at `~/.local/share/dayfile/venv` and no longer need a checkout; `deploy/selfhost/compose.proxy.yaml` runs dayfile alone on `127.0.0.1:8000` for people with their own reverse proxy; the README explains `DAYFILE_AUTH_URI`.

## 0.1.4

- A database file that dayfile creates is mode 0600 (owner only); an existing file keeps its mode.
- `GET /` on the server redirects to `/docs` instead of returning 404.
- `dayfile mcp` reports its version in the MCP handshake.
- README and the docs page: connect Claude and Claude Code through `https://dayfile.cloud/mcp`, the CloudEvents feed, and a note that the SSE stream sends only new events.

## 0.1.3

- The CLI and `dayfile mcp` trim whitespace from `DAYFILE_WRITE_TOKEN` and `DAYFILE_READ_TOKEN`; a trailing line break no longer turns the request body into garbage.
- The sign-in page redirects a signed-in visitor to the log, and a lapsed viewer token while polling goes back to the log rather than the sign-in page.
- The sign-in and sign-up pages no longer show a "Sign in" link in the header.
- Terminology: username everywhere a user sees it; README "Two ways to run it" simplified.

## 0.1.2

- README: the contributor steps moved to CONTRIBUTING.md; the README is about using dayfile.

## 0.1.1

- README quick start: `dayfile add` needs `--db` to write straight into the local log.

## 0.1.0 (first public release)

- A deleted account lets go of its GitHub identity, so the same GitHub account can sign up again as a new, empty account (before: "This account can't sign in", with no way back without an operator).
- Backup: a failed run pings `$BACKUP_PING_URL/fail` (healthchecks.io style). The heartbeat timer can ping `$HEARTBEAT_PING_URL` after each good run.
- Heartbeats: say how often a source or tag should report (`24h`, `7d`) and dayfile writes an open `heartbeat` to-do when it goes quiet, closing it when the source returns. Schema version 11 (a `heartbeats` table, added to existing databases on connect). `dayfile heartbeat check`, run every minute by `deploy/cloud/systemd/dayfile-heartbeat.timer`; admin API and a Heartbeats tab on the Feeds page.
- A log at or near its event or storage limit says so on the Log page, and a refused post points to Settings. New *Start a fresh log* in Settings erases the log and recreates it empty under the same name (the way out for an append-only log that is full). The "account deleted" page no longer mixes two text sizes.
- Deleting an account erases the log at once (no 30-day trash); only encrypted backups remain, and they age out within 14 days. The operator command `dayfile tenant delete` still moves to `.trash/`. Admin API: `DELETE /tenants/{slug}?erase=true`.
- Hosted MCP: every log serves MCP at `https://<username>.dayfile.cloud/mcp` and dayfile.app is an OAuth 2.1 authorization server (dynamic client registration, S256 PKCE, rotating refresh tokens), so Claude can connect from Customize, Connectors with a Connect button. Choose read and write or read only when approving; manage and disconnect under Tokens. New `DAYFILE_MCP` and `DAYFILE_OAUTH_ISSUER`; the image now includes the `mcp` extra.
- MCP: a retract tool.
- `DAYFILE_TOKEN` is now `DAYFILE_WRITE_TOKEN` (for `dayfile add` and `dayfile mcp`).
- The log reader clamps long event bodies behind "Read more".
- The nightly backup now includes the accounts database (`accounts.accounts.db.gz`); `dayfile tenant verify-backup` checks it.
- `GET /usage` on a log: events and bytes used against its limits, for a read token.
- The status page shows uptime, daily bars and incidents measured independently by UptimeRobot (credited), falling back to a labeled internal check. `deploy/cloud/setup-uptimerobot.sh` stores the read-only key; `DAYFILE_STATUS_MONITORS` picks which monitors to show.
- `/status`: JSON on the API (`https://dayfile.cloud/status`) and a readable status page at dayfile.app, linked from the footer.
- `dayfile mcp`: an MCP server on stdio (optional extra `dayfile[mcp]`, new dependency `mcp`) with tools to list and get events, add an event, close a to-do and retract an event. It uses a read token (`DAYFILE_READ_TOKEN`) and a write token (`DAYFILE_WRITE_TOKEN`). `/healthz` on the API also answers HEAD.
- Per-log storage quota (`max_bytes`, 25 MiB on the free beta plan, `tenant limit --max-bytes`, shown in Settings) and a cap of 60 new accounts an hour.
- Landing page rewritten around the outcome ("One log for your scripts, apps and agents."), with use cases, a "Your data, your control" section and a public `/demo` example log. An empty log now shows how to send the first event.
- Signup is open: signing in with GitHub and accepting the terms is enough, and the site says it is in beta (`DAYFILE_SIGNUP=invite` brings the invite-code step back; the cloud compose file defaults to `open`). The log filter takes several tags (all must match). Delete account is red. The footer is laid out left, center, right.
- Breaking: events no longer have a `type`. The tag `todo` makes an event a to-do (open until closed); every other event has no status. The `type=` filter, the `type` column and the post form's State menu are gone. Housekeeping events carry the tag `dayfile-admin` instead of a type. CloudEvents export always sets `type` to `app.dayfile.event`; a foreign event's `type` is imported as a tag. `dayfile add` gained `--todo` (and `--due` implies it). Schema version 10: existing databases must be recreated.
- M0 to M8: event log, HTTP ingest, JSON Feed and Atom views, lifecycle, tokens and audiences,
  ntfy notifications, generators, Ed25519 signing, CloudEvents.
- M9: write credentials and rate limit, release cursor for time-released events, IMAP IDLE with a
  persisted cursor, reversible lifecycle events.
- M10a: `/healthz`, `dayfile backup`, Docker and Caddy deployment.
- Licensed under AGPL-3.0-only.
- `dayfile add --db` (direct operator mode); packaged wheel now includes the schema files so `pip install` works.
- M11: native JSON API (`GET /events`, `/events/{id}`, long-poll, SSE stream). Fixed: JSON Feed and Atom ETags changed on every request (conditional GET never matched).
- M12: tenancy seam (`DAYFILE_TENANTS_DIR`, one SQLite file per subdomain), suspension, event quota, per-tenant write limit, 1 MiB request body limit, `dayfile tenant` commands, `/internal/tls-ask` hook. Single-tenant behaviour is unchanged.
- M15: web app for `dayfile.app` (`dayfile.web`): landing, docs, draft terms and privacy, and a read-only log viewer that signs in with a read token. Runs in its own container with no tenant files. New dependencies: `jinja2`, `python-multipart`. The static holding page is gone.
- Favicon: `/favicon.svg` and `/favicon.ico` on every host; the cloud profile also serves a static holding page and the same icons on `dayfile.app` (`DAYFILE_SITE_DOMAIN`).
- M13: `deploy/` split into `selfhost/` and `cloud/` profiles; tenant backup script, Litestream config, cloud Caddyfile; `docs/CLOUD.md`.
- M14: per-token read rate limit (300/60 s) and a cap of 8 held streams and long-polls, both answering 429 with Retry-After.
- Cloud profile prep: dayfile.cloud for tenants and dayfile.app for the site, `cloud-init.yaml` for the Droplet, `smoke-test.sh` for after first boot, `team@dayfile.app` as the security contact.
- Feeds advertise the dayfile icon (JSON Feed `icon`/`favicon`, Atom `<icon>`), served as PNGs on every host.
- Web: visual refresh (hero with a log mock-up, cards, code samples, amber accents, status-coloured events).
- Web: bundled fonts (later replaced by Source Sans 3 and JetBrains Mono) (SIL OFL, served from the same origin, CSP gains `font-src 'self'`); the homepage now points at invite sign-in.
- Quick post: `POST /events` accepts just `{"title": ...}`; the server fills id, type, time and (for a one-source write token) the source.
- The web app's libraries (jinja2, python-multipart) are an optional extra, `dayfile[web]`; a test keeps the core free of web imports.
- M16a: accounts store (`dayfile.web.accounts`), invite codes (hashed, single-use by default, atomic redemption) and the `python -m dayfile.web.admin` operator CLI. Not wired into the site yet.
- Cloud profile: every service rotates its Docker logs (10 MB x 3); `exim4` is not needed on the host.
- M16b: admin API under `/internal/admin/` (tenants, tokens, suspend, delete) behind a shared-secret file, `dayfile.admin` events in the tenant log, token `last_used` (schema 8).
- Cloud profile: `setup-backup.sh` writes the root-only rclone config (Spaces key plus encryption password and salt) from hidden prompts and proves it with an encrypted round trip.
- Backups: `dayfile tenant backup-all` stages verified copies and each tenant.json inside the container; `backup-tenants.sh` uploads them from the host; systemd service and timer run it nightly.
- M17: named feeds (`/f/<name>.json|.atom|.cloudevents`, bare `/f/<name>` and `/feed` give JSON Feed), per-feed private or public visibility, anonymous reads of public feeds (events labelled `public` only, short public cache, own rate budget), `public` events visible to all readers, `dayfile feed` commands and admin API routes.
- M16c: sign in with GitHub (OAuth with PKCE, no scope), opaque server-side sessions, invite-code signup with terms acceptance, a minimal account page. Off until a GitHub OAuth app is configured.
- M16d: account dashboard: choose a permanent log name (creates the tenant through the admin API), create and revoke tokens, create and publish feeds, open the viewer. The admin secret is wired between the API and web containers.
- Usernames can be changed (the old name is held for its owner for one day); the homepage card says you.dayfile.cloud.
- Web: the header shows Account or Your log when signed in; the homepage card is tilted a little more.
- Web: the signed-in layout is Log, Feeds, Tokens and Settings (a gear); the Log has a Post button that opens a dialog, cards with a relative time, a short id, colored status, tags and Markdown details, and a History of closing steps with Undo. Docs open with a diagram of logs, tokens and feeds; the footer has the docs link, copyright and email and GitHub icons.
- `type` is optional. Only a `todo` has a status (`open`, then `closed`); other events have none (`status=none`). `status=done` is still accepted as `closed`. A closing, replacing or retracting event may omit `title` and inherits its target's.
- The service's own housekeeping records (`dayfile.admin`) are left out of feeds and the Log unless asked for.
- Source Sans 3 and JetBrains Mono are the site fonts (SIL OFL); the repository moved to the `dayfile` GitHub organization.
- M16e: Settings can download the whole log (gzipped SQLite, integrity-checked) and delete the account (type the username); `dayfile tenant purge` and a weekly timer remove deleted logs after 30 days; terms and privacy describe it.
- Backups: `dayfile tenant verify-backup DIR` and `deploy/cloud/restore-drill.sh` (read-only restore drill, prints the elapsed time); results go in `docs/ops/drills.md`.
