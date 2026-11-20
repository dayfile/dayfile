# Directory listing: text for review

Draft wording for the Connectors Directory portal (<https://claude.ai/directory/manage>, Submit new,
MCP connector). Nothing here has been submitted. Limits are Anthropic's: name 100 characters,
one-liner 200, description 2,000, one to five categories.

## Connection

- **Server URL:** `https://dayfile.cloud/mcp`
- **Authentication:** OAuth 2.0 with PKCE (S256) and dynamic client registration. The authorization
  server is `https://dayfile.app`. Claude's callback, `https://claude.ai/api/mcp/auth_callback`, is
  accepted because clients register their own redirect.

## Name (7 of 100)

dayfile

## One-liner (147 of 200)

An append-only log for your scripts, cron jobs and agents. Ask Claude what happened, add events and to-dos, and close them in your own dayfile log.

## Description (1459 of 2,000)

dayfile is an append-only event log for the things that run without you: backups, deploys, cron jobs, scripts and AI agents. Each one posts a short event with a title and optional tags; dayfile keeps them in order, never edits or deletes them, and shows them as a feed you can read.

Connect it to Claude and you can ask what happened instead of digging through logs: "did last night's backup finish?", "what shipped this week?", "what is still open?" Claude can also write: add a note or a to-do, close a to-do when it is done, or retract an event that was a mistake. Retracting hides an event; it stays in the log.

To-dos are ordinary events tagged todo, open until closed. A source that goes quiet can raise its own to-do (a heartbeat), so a silent backup shows up as an open item Claude can find.

You choose the access when you connect: read your events only, or read and write. Claude only ever sees your own log. You can disconnect at any time under Tokens at dayfile.app.

Five tools: list events (filter by tag, status and recency), get event, add event, close to-do and retract event. Everything is stored in your own log at your-username.dayfile.cloud, which you can also read as an Atom or JSON feed and download as a SQLite file at any time.

dayfile is free while it is in beta and open source (AGPL-3.0). You need a dayfile account at dayfile.app (sign in with GitHub). A log holds up to 50,000 events and 25 MB, with up to 60 writes a minute.

## Suggested categories (choose from the portal's own list)

Developer tools, Productivity, Automation, Monitoring.

## Use cases

- Ask what happened: last night's backup, this week's deploys, anything that failed.
- Keep a to-do list in the same log, and close items from a chat.
- Let an agent record what it did, so a person can read it later.
- Find what has gone quiet: open to-dos tagged `heartbeat`.

## What a user needs before connecting

A dayfile account (free during the beta), made at <https://dayfile.app> by signing in with GitHub.
The account has one log. No payment details, no API key to copy: the connection signs in through the
browser.

## Reads, writes or both

Both, at the user's choice on the consent screen: read only, or read and write. Reading uses two
tools (list events, get event). Writing uses three (add event, close to-do, retract event), none of
which deletes anything.

## Links

| Field | Value |
|---|---|
| Documentation | <https://dayfile.app/docs#connect> |
| Privacy policy | <https://dayfile.app/privacy> |
| Terms | <https://dayfile.app/terms> |
| Support | team@dayfile.app |
| Source | <https://github.com/dayfile/dayfile> |
| Icon | `docs/assets/icon.svg`, `docs/assets/icon-512.png` (512 by 512) |

## For you to decide

- The company name and website for the portal's Company step. The terms name no operating entity.
- Which of the suggested categories the portal offers.
- Whether the description should mention the beta and the limits (it does now).
