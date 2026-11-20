# Directory pre-submission check

Status on 2026-11-19. **Pass** means checked, with how. **Not yet** means work is left. **You** means
only the owner can do it. Sources: Anthropic's submission, review-criteria, authentication and
testing pages (docs at claude.com/docs/connectors/building).

## Technical

| Item | Status | Evidence or what is left |
|---|---|---|
| Remote over HTTPS | Pass | `https://dayfile.cloud/mcp`, Streamable HTTP, on port 443 |
| 401 with `resource_metadata` | Pass | The challenge names `https://dayfile.cloud/.well-known/oauth-protected-resource/mcp` |
| `resource` equals the server URL | Pass | The metadata says `https://dayfile.cloud/mcp` |
| OAuth 2.0, PKCE S256, dynamic client registration | Pass | Claude added the connector, detected sign-in, registered itself and was connected (2026-11-19) |
| Refresh tokens rotate; `invalid_grant` for dead ones | Pass | Covered by `tests/test_oauth.py` |
| Authorization server reachable from Anthropic's addresses | Pass | `dayfile.app` is public; the only fail2ban jail is `sshd`, so no web address is blocked |
| Every tool has a title and a read-only or destructive hint | Pass | `tests/test_mcp_server.py` fails if one lacks them |
| Read and write split, no catch-all tool | Pass | Five fixed tools: two read, three append |
| Tool descriptions match behavior, no instructions to other tools | Pass | Reviewed; see `docs/SPEC.md` |
| Actionable errors | Pass | Errors carry the status and message, such as `404: not found` or `400: ...` |
| Responses a reasonable size | Pass | `list events` returns at most 100 |
| Works with read-only sign-in | Pass | Tested from claude.ai, read-only, 2026-11-19 |
| Tested in claude.ai with read and write | **You** | Only read-only was tried from Claude. Writes are covered by tests and by direct calls on the server |
| A tool call from a chat | **You** | Not run from claude.ai yet |
| Tested with MCP Inspector | Not yet | `npx @modelcontextprotocol/inspector --transport http`, URL `https://dayfile.cloud/mcp` |

## Listing

| Item | Status | Where |
|---|---|---|
| Name, one-liner, description | Pass | Within limits, draft in `docs/directory-listing.md` |
| Documentation URL | Not yet | `https://dayfile.app/docs#connect`: the section is on a branch, live after the next deploy |
| Privacy policy URL | Not yet | `https://dayfile.app/privacy` exists but has gaps (below) |
| Support contact | Pass | `team@dayfile.app` on the docs and privacy pages |
| Icon | Pass | `docs/assets/icon.svg` and `icon-512.png`; Anthropic's pages state no size, so check the portal |
| Categories (one to five) | **You** | Pick from the portal's list |
| Company name and website | **You** | See open questions |

## Privacy page gaps

Anthropic requires data collection, use and storage, third-party sharing, retention and a contact,
and rejects an incomplete policy at once. No text below is written for you.

1. **Connected assistants.** Nothing says that when you connect an assistant, what it reads from
   your log goes to that assistant's provider under their terms.
2. **What a connection holds.** Not covered: the app registrations and connection records kept for
   connected assistants, the one-hour access tokens and refresh tokens, and when they are deleted.
3. **Retracting is not erasing.** The page says deleting an account erases the log. It does not say
   that retracting or closing keeps the original event.
4. **Retention of operational records.** The page says health messages are kept but not for how long.
5. **Contact emails.** The page does not name who processes the emails we send, if anyone does.
6. **Status of the page.** It still says "Draft" with "Last updated 17 November 2026". The terms say
   the operating entity and governing law "will be stated before general availability".

## Reviewer test account

| Item | Status | Where |
|---|---|---|
| Steps for the owner | Pass | `docs/directory-review.md` |
| One command seeds the log | Pass | `dayfile gen review-log --db ...`, 19 events, safe to rerun |
| The account itself | **You** | A dedicated GitHub account and a dayfile account made from it |
| Sign-in method for reviewers | **You** | Anthropic's pages do not address two-factor, emailed codes or GitHub sign-in. Ask `mcp-review@anthropic.com` |

## Open questions for you

1. **Company name and website** for the portal's Company step. The terms name no operating entity.
2. **Registry name.** I used `io.github.dayfile/dayfile`. A domain name (`app.dayfile/dayfile`) needs
   DNS or HTTP proof.
3. **`destructiveHint` on the write tools.** I set it to `false` (they only append), as the plan said
   and as the MCP specification defines it. Anthropic's pages describe it as "for tools that modify or
   delete data", so a reviewer could expect `true` on writes. `true` would make Claude always ask
   before a write. Which do you want?
4. **Registration limit.** Dynamic client registration allows 60 new registrations an hour across
   everyone. Anthropic says to prefer a published client identity or client metadata documents for
   heavy traffic. Fine for now; raise or switch if a listing brings volume.
5. **A person without an account.** Plan Task 1 asked for a clear error telling them to create a log
   at dayfile.app. Today, signing in as a new GitHub user leads into sign-up, which seems friendlier.
   Keep that?
6. **Release.** `server.json` and the README marker name version 0.1.4, which is not on PyPI yet.
   The registry reads that release's README, so publish 0.1.4 before `mcp-publisher publish`.

## Not done, on purpose

- Submissions to the portal and the registry, creating accounts, DNS changes, pricing and limits.
- A CI workflow that publishes `server.json`.
