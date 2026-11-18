# M16 design: accounts for the web app

Status: design, not built. Nothing here changes behavior today. Open decisions are marked **[decide]**; things only the
operator can supply are collected in `README.md` under "Needed from you".

## Goal

Someone with an invite code can sign in with GitHub, get their own log (`<slug>.dayfile.cloud`), mint and revoke tokens,
see usage, export their data and delete their account, all from `dayfile.app`. Billing is out of scope. No email is sent
or collected.

## Decisions already made

- GitHub OAuth is the only sign-in method. Invite codes gate signup. Billing comes later.
- The web app (`dayfile.web`) stays a separate container with no tenant files.
- Python, FastAPI, Jinja2, vendored htmx. No new dependency is needed (see "Dependencies").

## Trust boundaries

```
browser ──► Caddy ──► web (accounts.db, admin secret) ──► dayfile (tenant files)   over the compose network only
                         │                                   ▲
                         └──► github.com, api.github.com     └─ /internal/admin/*  (never routed by Caddy)
```

| Component | Holds | Cannot |
|---|---|---|
| `web` | `accounts.db` (own volume), the admin secret, GitHub client secret | open tenant files; read events except through the API with a token |
| `dayfile` | tenant files, the admin secret | reach accounts.db |
| Caddy | nothing | proxy `/internal/*` (already tested to 404 on the tenant catch-all; the site block proxies only to `web`) |

Chosen over mounting tenant files into `web`: a bug in the web app (templating, OAuth parsing) would then reach every
tenant's data. With the admin API, a compromised `web` can still create tenants and mint tokens (and so read), but only
through narrow, rate-limited, audited calls. That is the residual risk, stated plainly in the privacy page.

## Admin API (new, in `dayfile`)

Routes under `/internal/admin/`, registered before tenant gating like `/internal/tls-ask`, so no `Host` match is needed.
Auth: `Authorization: Bearer <secret>` compared in constant time against a file (`/run/secrets/dayfile_admin`, 32 random
bytes, mode 0400). Absent file means the routes do not exist (404), so the self-host profile is unaffected. The secret
is a Docker file-based secret, not an environment variable (`docker inspect` shows env).

| Route | Effect | Idempotence |
|---|---|---|
| `PUT /internal/admin/tenants/{slug}` | create with plan limits; 200 if it exists with the same owner key | by slug + `owner` field in `tenant.json` |
| `GET /internal/admin/tenants/{slug}` | usage (events, bytes, limits, suspended) | read |
| `POST /internal/admin/tenants/{slug}/tokens` | mint; body `{kind: read\|write, labels, write_source, expires_in, note}`; returns the secret once | no (new token each time) |
| `GET .../tokens` | list ids, kind, labels, source, created, expires, revoked (never secrets) | read |
| `DELETE .../tokens/{id}` | revoke | yes |
| `POST .../suspend`, `.../resume` | lifecycle | yes |
| `POST .../export` | stream a consistent copy (SQLite backup API, gzip) | read |
| `DELETE /internal/admin/tenants/{slug}` | move to `.trash/` | yes |

Rules:
- Every mutating call also appends a `dayfile.admin` event (`source.id = "dayfile"`, no secrets) to that tenant's own
  log, so the owner sees "token abc created from the dashboard" in their feed. A compromised web app therefore cannot
  act silently on a tenant.
- Hard caps enforced server-side regardless of what web asks: 20 live tokens per tenant, 30 admin writes per minute
  overall.
- Log token ids and slugs, never token values (invariant 9). A test asserts the minted secret is absent from logs.
- Needs `tokens.last_used` (not in `schema.sql` today) for the "last used" column. **Decided: add it**, with a coarse
  update (at most one write per token per hour) so reads stay cheap. It is the best signal for "revoke what you no longer use".

## Accounts database (`accounts.db`, owned by `web`)

SQLite on its own volume `/srv/dayfile/accounts` (web uid, 0700), WAL, same append-only discipline for the audit table.

```
account   id ULID pk · github_id INTEGER UNIQUE NOT NULL · github_login TEXT · created_at · status 'active'|'suspended'|'deleted'
          slug TEXT UNIQUE NULL · terms_version TEXT · terms_accepted_at · deleted_at
invite    id ULID pk · code_hash BLOB UNIQUE · note · created_at · expires_at · max_uses · uses · revoked_at
redeem    invite_id · account_id · at                            (one row per redemption)
session   id_hash BLOB pk · account_id · created_at · last_seen · expires_at · viewer_token_id · viewer_token (see below)
oauth     state_hash pk · verifier · created_at                  (short-lived, deleted on use or after 10 minutes)
reserved  slug pk                                                (seed list below)
slug_hold slug pk · until                                        (quarantine after deletion)
audit     id · at · account_id · action · detail JSON            (no secrets, append-only triggers)
```

Identity is the numeric GitHub id, never the login (logins can be renamed and reassigned). The login is display only and
refreshed at each sign-in. No email, avatar, name or token from GitHub is stored.

### Slugs

Pattern is the existing `SLUG` from `dayfile.tenancy`. Refuse: reserved names (`www api app admin mail smtp imap ftp ns
ns1 ns2 ns3 dashboard login signup auth static assets cdn status blog docs help support team security abuse postmaster
root dayfile internal test demo dogfood` plus anything the operator adds), names that look like tenant-ids from other
products (**[decide]** whether to reserve common brand names; recommendation: no, handle by takedown policy), and held
slugs. After account deletion the slug is held for 90 days so an old feed URL cannot be taken over by someone else and
made to serve different content to existing readers. One slug per account in v1; the schema already allows more later.

## Sign-in flow

1. `GET /login` offers "Sign in with GitHub" as a plain **link** to `GET /auth/github`, plus the existing paste-a-read-
   token form (kept for people who were only handed a read token). It must be a link, not a form post: with
   `form-action 'self'` Chrome also blocks redirects that follow a form submission.
2. `/auth/github` creates `state` (256 bit) and a PKCE verifier, stores their hashes server side, sets a short cookie
   `__Host-dayfile-oauth` (`HttpOnly; Secure; SameSite=Lax; Max-Age=600`), and 302s to
   `https://github.com/login/oauth/authorize` with `client_id`, `redirect_uri`, `state`, `code_challenge` (S256) and
   `scope=` empty (public profile only: id and login). No `user:email`.
3. `GET /auth/github/callback?code&state`: check the state against the cookie and the stored hash, once, within 10
   minutes. Exchange the code with POST to `github.com/login/oauth/access_token` (client secret, verifier), then `GET
   https://api.github.com/user`. Read `id` and `login`. **Discard the GitHub access token immediately**; it is never
   stored or logged. Both calls use the existing `http_fetch` pattern: fixed hosts, no redirects, size cap, timeout.
4. Known id → create a session. Unknown id → create a *pending signup* (session with no account) and show the invite
   form. Suspended or deleted account → generic "can't sign in" page.
5. The callback answers 200 with a tiny page that links onward, not a 303: a `SameSite=Strict` cookie is not sent on the
   first request of a chain that began on another site, so a redirect would appear signed out. The session cookie is
   `__Host-dayfile-session`, `SameSite=Lax`, because OAuth returns via a cross-site navigation; the existing Origin
   check on every `POST` remains the CSRF defense. `Lax` still withholds the cookie from cross-site subrequests and POSTs.

Session: opaque 256-bit id in the cookie, only its hash in `accounts.db`. Idle 8 hours, absolute 30 days, rotated at
sign-in, deleted at sign-out and on account suspension. The token-paste sessions from M15 stay as they are (separate
cookie name `__Host-dayfile`, read-only viewer, no account).

### Viewer access for an account session

The viewer needs a read token to call the API. For a signed-in account the web app asks the admin API for a **15-minute
read token** (`labels=["self"]`, `expires_in=900`, note "dashboard viewer"), keeps it in the session row, and renews it
when it is within a minute of expiry. Tokens already expire (schema `expires_at`), so a leaked cookie or database page
yields at most a short-lived read, and sign-out revokes it. The tokens list hides these rows.

## Invite codes

- Format: 12 Crockford base32 characters in groups (`7F3K-9QXM-2D8T`, 60 bits), generated with `secrets`. Stored as
  SHA-256 only. Shown once at creation.
- Created by the operator: `docker compose exec web python -m dayfile.web.admin invite create --note "for A" --uses 1
  --expires 14d` (also `list`, `revoke`). No UI in v1. **[decide]** later, per-account "invite a friend" quotas.
- Redeem atomically: `UPDATE invite SET uses = uses + 1 WHERE code_hash=? AND uses < max_uses AND revoked_at IS NULL AND
  (expires_at IS NULL OR expires_at > now)` and require `rowcount == 1`, in the same transaction that inserts the account.
  A test races two redemptions of a single-use code.
- Brute force: 60 bits is not guessable online, but throttle anyway: 5 failed redemptions per session, 20 per minute
  overall (reuse `ratelimit.Window`). The error text never distinguishes expired, used and unknown.

## Account creation

After a valid invite: choose a slug (live availability is checked only on submit, no enumeration endpoint), tick the
terms (version and time stored), then the web app calls `PUT .../tenants/{slug}` with the plan limits and mints the
first write token and read token, shown once on a page with ready-to-copy `curl` and feed-reader snippets. The creation
runs in order: reserve slug in `accounts.db` → create tenant → mint → mark account active. A failure in the middle
leaves a `pending` account that the next attempt resumes, never a half-owned tenant (the admin `PUT` is idempotent by
slug and owner).

## Dashboard (`/account`)

| Page | Content |
|---|---|
| Overview | log URL, usage bars (events and bytes against limits), write-rate limit, link to the viewer |
| Tokens | table: id, kind, labels, write source, created, last used, expires; "Create token" form (kind, labels from the allowed set, source, expiry default 1 year, note); revoke with a confirm page (a real `POST`, no JS dialogs) |
| New token | one-time display; never rendered again; `Cache-Control: no-store` |
| Export | downloads a gzip of a consistent SQLite copy, plus a JSON Lines option via the API for portability |
| Delete account | confirm by typing the slug; effect below |

All pages `no-store`, CSP unchanged (`script-src 'self'`), CSRF through the Origin check plus a per-session form token for
destructive actions.

### Deletion

Typing the slug confirms. The tenant is suspended and moved to `.trash/` immediately (log and feeds stop), the account
row is marked `deleted` with `deleted_at`, the slug is held for 90 days, sessions are destroyed. A weekly purge job
removes `.trash/` entries older than 30 days, which matches the legal drafts' 30-day deletion promise; the draft says
backups age out within 14 days after that (nightly retention) and replicas within 7 days. The purge is a documented
operator command until volume justifies a timer.

## Plans and limits

One plan, "free beta". **Decided (operator said "your call", 2026-11-10):** default limits are 50,000 events, 25 MiB of event bytes, 60 writes per minute,
20 tokens. Operator-adjustable per account through `tenant limit`. An account that hits a limit gets 429/409 from the API
(existing behavior) and a banner in the dashboard.

## Abuse and safety

- Invite-only is the main control. Per-account caps (one slug, 20 tokens). Existing per-token read and write limits apply.
- Operator can suspend an account (`admin account suspend <login>`), which suspends its tenant and kills sessions.
- Terms forbid abuse; legal drafts already carry that. A report address (`abuse@`) is needed; see `dns-and-mail.md`.
- GitHub login is not identity verification; do not treat it as such in the terms.

## Privacy impact (update `privacy.html` when built)

New data: GitHub numeric id and login, account and invite records, terms acceptance, session records, admin audit log.
No IP addresses are stored. GitHub is a new subprocessor-like third party (sign-in only; receives the user's browser,
not their log data). The page must also state the residual `web`-compromise risk above in plain words.

## Dependencies

None needed. OAuth with `http_fetch`-style urllib, PKCE and hashing with `hashlib`/`secrets`, SQLite with stdlib. (Authlib
or similar would add a dependency for about 80 lines of code; rejected under the "ask before adding" rule.) Compose
changes: a `secrets:` block, an accounts volume on `web`, `web` joins the network that can reach `dayfile:8000`
(already true), egress to GitHub allowed (default bridge).

## Slices (each its own commit, tests first)

| Slice | Content | Acceptance |
|---|---|---|
| 16a (done) | `accounts.db` schema, `dayfile.web.admin` CLI (invite create/list/revoke, account list/suspend), atomic redemption | race test; hash-only storage; audit append-only |
| 16b (done) | Admin API in `dayfile` + `tokens.last_used`, `dayfile.admin` events, caps | auth required; secret absent from logs; self-host unaffected; Caddy test for `/internal/admin` hidden |
| 16c (done) | GitHub OAuth against a stub GitHub server; pending signup; sessions | state/PKCE replay refused; expired state; cookie flags; no token persisted |
| 16d (done) | Slug choice, tenant creation, first tokens, dashboard pages | resumable creation; reserved/held slugs; ownership checks on every route (account A cannot touch B's token ids) |
| 16e | Export, delete, purge command, privacy and terms updates, smoke-test additions | delete frees nothing before the hold; export opens with `sqlite3` and passes `integrity_check` |
| 16f | Live: register OAuth app, install secrets, deploy, first invite | end-to-end sign-in by the operator |

16a to 16e need no credentials (tests use a stub GitHub). 16f needs the OAuth app.

## Risks

- OAuth bugs are classic: open redirect (fixed `redirect_uri`, no `next` parameter), state fixation (state bound to the
  cookie and a server record), code replay (single use). Each gets a test.
- The admin secret is a high-value target; mitigated by file permissions, the audit events and the caps above.
- One worker and SQLite: accounts.db is small and rarely written; no contention with tenant writes.
