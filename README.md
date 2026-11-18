<p align="center"><img src="https://raw.githubusercontent.com/dayfile/dayfile/master/docs/img/logo.svg" alt="dayfile" height="96"></p>

# dayfile

A personal, append-only event log exposed as feed views (JSON Feed, Atom, CloudEvents).
Events arrive from email, SMS, webhooks, cron generators and agents. The log is the source
of truth; feeds are projections of it, addressed by URL queries such as
`/feed.json?tag=todo&status=open&within=7d`.

- Append-only SQLite log, enforced by triggers. State changes are new events that point back at the original (close it, replace it, retract it); the status of an event is worked out from that chain.
- Time release (`visible_at`, `expires_at`), idempotent ingest, deny-by-default audiences.
- A JSON API (`GET /events`, long-poll, live SSE stream) next to the feeds.
- Per-credential feeds: read tokens (Bearer header or `/t/<token>/` path) and write-only tokens.
- Push to ntfy, Ed25519 signed events, CloudEvents import/export, Gmail/Google Voice SMS ingest (IMAP IDLE).
- A web app (`dayfile[web]`): sign in with GitHub, a log you can read and post to, tokens, feeds (private or public) and settings. It is the managed service at dayfile.app; see below for what runs where.

Design: [docs/SPEC.md](https://github.com/dayfile/dayfile/blob/master/docs/SPEC.md). Milestones and status: [docs/ROADMAP.md](https://github.com/dayfile/dayfile/blob/master/docs/ROADMAP.md).
Contributor rules: [CLAUDE.md](https://github.com/dayfile/dayfile/blob/master/CLAUDE.md).

## How it fits together

![architecture](https://raw.githubusercontent.com/dayfile/dayfile/master/docs/img/architecture.svg)

State changes are new events, never edits:

![lifecycle](https://raw.githubusercontent.com/dayfile/dayfile/master/docs/img/lifecycle.svg)

One URL space, a different view per credential:

![access control](https://raw.githubusercontent.com/dayfile/dayfile/master/docs/img/access.svg)

## Quick start

Requires Python 3.11 or newer.

```
pip install dayfile            # or: uv tool install dayfile  /  pipx install dayfile
export DAYFILE_DB=~/dayfile.db

dayfile add "buy milk" --tag todo                          # write straight into the local log
dayfile token create --labels self                         # read token: prints the feed URLs once
dayfile token create --labels self --write-source adapter:cli   # write token, for remote clients
uvicorn dayfile.app:app                                    # serve on 127.0.0.1:8000
```

Open the feed URL that `token create` printed (`http://127.0.0.1:8000/t/<read token>/feed.json`, or
`feed.atom`) in a browser or a feed reader. Add events from another machine with
`DAYFILE_WRITE_TOKEN=<write token> dayfile --url http://127.0.0.1:8000 add "buy milk" --tag todo`.
For a server that stays up, use Docker or systemd: [`deploy/selfhost`](https://github.com/dayfile/dayfile/blob/master/deploy/selfhost).

Extras: `pip install "dayfile[mcp]"` for the assistant (MCP) server, `"dayfile[web]"` for the dayfile.app web app.

### Working on dayfile

```
git clone https://github.com/dayfile/dayfile && cd dayfile
uv sync                   # add --extra web to run the web app
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## JSON API

```
curl -H "Authorization: Bearer $READ_TOKEN" 'http://127.0.0.1:8000/events?tag=todo&status=open'
curl -H "Authorization: Bearer $READ_TOKEN" 'http://127.0.0.1:8000/events?since_seq=42&wait=25'   # long-poll
curl -N -H "Authorization: Bearer $READ_TOKEN" http://127.0.0.1:8000/events/stream               # live, SSE
curl -H "Authorization: Bearer $WRITE_TOKEN" -d @event.json -H 'content-type: application/json' http://127.0.0.1:8000/events
```

See the JSON API section of [docs/SPEC.md](https://github.com/dayfile/dayfile/blob/master/docs/SPEC.md).

## Commands

| Command | Purpose |
|---|---|
| `dayfile add` | append an event: with `--db PATH` straight into the database (operator mode), otherwise POST to `--url` using a write token in `$DAYFILE_WRITE_TOKEN` |
| `dayfile token create\|list\|revoke` | manage read and write tokens |
| `dayfile poll-email` | ingest a Gmail folder (default `SMS`) using `GMAIL_USER` / `GMAIL_APP_PASSWORD` from env or `.env` |
| `dayfile notify` | publish urgent events to ntfy (`$NTFY_URL`) as they become visible |
| `dayfile gen weather\|digest` | generators that write ordinary events back to the log |
| `dayfile key create\|list\|import` | signing keys (`add --sign`) |
| `dayfile mcp` | MCP server on stdio for AI assistants (`pip install dayfile[mcp]`); see below |
| `dayfile backup DEST` | consistent online copy of the database |
| `dayfile tenant ...` | managed-service operations: create, suspend, limit, usage (see [docs/CLOUD.md](https://github.com/dayfile/dayfile/blob/master/docs/CLOUD.md)) |

## MCP server

Let an assistant such as Claude read and write your log. Both ways use the same five tools (`dayfile_list_events`, `dayfile_get_event`, `dayfile_add_event`, `dayfile_close_todo`, `dayfile_retract_event`) and can do only what their tokens allow.

**Hosted (dayfile.cloud).** Every log has an MCP address, `https://<you>.dayfile.cloud/mcp`. In Claude, open Customize, then Connectors, then Add custom connector, paste that address and press Connect: you sign in with GitHub and choose read and write or read only. Claude Code can use it too: `claude mcp add --transport http dayfile https://<you>.dayfile.cloud/mcp`, then `/mcp` to sign in. To skip the sign-in, send a read token and a write token joined with `~` as `Authorization: Bearer <read>~<write>` (read only: the read token alone). Disconnect any time under Tokens.

**Local, over stdio.** `dayfile mcp` (`pip install "dayfile[mcp]"`) calls the API at `$DAYFILE_URL` with a read token in `$DAYFILE_READ_TOKEN` and a write token in `$DAYFILE_WRITE_TOKEN`; leave the write token out for a read-only assistant. This is the way to use a self-hosted server without OAuth.

```json
{"mcpServers": {"dayfile": {
  "command": "uvx", "args": ["--from", "dayfile[mcp]", "dayfile", "mcp"],
  "env": {"DAYFILE_URL": "https://you.dayfile.cloud",
          "DAYFILE_READ_TOKEN": "...", "DAYFILE_WRITE_TOKEN": "..."}}}}
```

Self-hosting the hosted endpoint: set `DAYFILE_MCP=1` (needs `dayfile[mcp]`) and, to offer the sign-in button, `DAYFILE_OAUTH_ISSUER` to the address of a dayfile web app that has accounts.

## Two ways to run it

The same code serves both; the difference is configuration.

| | Self-hosted ([`deploy/selfhost`](https://github.com/dayfile/dayfile/blob/master/deploy/selfhost)) | Managed-service profile ([`deploy/cloud`](https://github.com/dayfile/dayfile/blob/master/deploy/cloud)) |
|---|---|---|
| Logs | one, in `$DAYFILE_DB` | one SQLite file per tenant at `<slug>.<your-domain>` |
| Setup | `docker compose up` or systemd | same image with `DAYFILE_TENANTS_DIR` + `DAYFILE_BASE_DOMAIN` |
| Extras | none | suspension, quotas, per-tenant limits, tenant backups, Litestream, on-demand TLS |

The web app (`src/dayfile/web`, install with `pip install "dayfile[web]"`) is a separate process that talks to the API over HTTP and has no access to tenant files. Accounts, GitHub sign-in, invite codes and the dashboard need the cloud profile (they create tenants through an admin API); the self-hosted profile runs the API, CLI and feeds, and the web app is not part of its compose file yet. Billing is not built. [docs/CLOUD.md](https://github.com/dayfile/dayfile/blob/master/docs/CLOUD.md) describes the hooks.

## Security notes

- Keep the server on localhost or behind a trusted TLS proxy; tokens in URL paths are a compatibility layer for feed readers.
- Only token hashes are stored; token values are redacted from logs. `.env` is gitignored.

## License

dayfile is free software under the [GNU Affero General Public License v3](https://github.com/dayfile/dayfile/blob/master/LICENSE) (AGPL-3.0-only).
You may use, modify and self-host it freely. If you run a modified version as a network service,
section 13 requires you to offer its users the corresponding source code.
