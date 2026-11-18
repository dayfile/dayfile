# Design notes

Designs for the work after M15. Much of it is built since, so the status lines in these notes are history; for what is live and how to run it, read `../ops/README.md` (the operator's handbook, not in the public repo). Each file says what is decided, what is open (**[decide]**), and
what only the operator can supply.

| Note | Covers | Blocked on the operator? |
|---|---|---|
| [m16-accounts.md](m16-accounts.md) | GitHub sign-in, invite codes, admin API, dashboard, export and delete | only slice 16f (live OAuth app) |
| [backups-and-restore.md](backups-and-restore.md) | Spaces bucket, encrypted nightly copies, Litestream, restore drills | bucket, keys, crypt password |
| [monitoring.md](monitoring.md) | DO alert policies, external probes, heartbeat, channels | provider choice, alert policies |
| [dns-and-mail.md](dns-and-mail.md) | `team@` mailbox, SPF/DMARC, CAA, `www`, reserved IP, security.txt | mailbox provider, DNS console steps |
| [legal-review.md](legal-review.md) | review package for the draft terms and privacy pages | operator entity, governing law |
| [going-public.md](going-public.md) | history scan results, GitHub settings, CI additions | scanner approval, commit identity |
| [feed-icons.md](feed-icons.md) | icon fields in Atom and JSON Feed | nothing |

## Suggested order

1. **feed-icons**: small, no inputs.
2. **dns-and-mail**: the four spoof-protection records for `dayfile.cloud` can go in today; then the mailbox.
3. **backups-and-restore** plus **monitoring**: before any data matters. Fix the `tenant.json` gap first.
4. **going-public**: scan, rotate the tokens shown in transcripts, then decide on visibility.
5. **M16** slices 16a to 16e (no credentials needed), then 16f.
6. **legal-review**: start the questions now; the final text needs M16's data model.

## Needed from you

- Spaces: a bucket and two access keys; an rclone crypt password and salt kept in your password manager.
- Mailbox provider choice (recommended: a forwarding service now).
- Probe and heartbeat providers; a phone ntfy topic.
- GitHub OAuth app for `https://dayfile.app` with callback `https://dayfile.app/auth/github/callback`, client id and secret
  placed in a server secret file, never in chat.
- ~~Plan limits~~ decided: 50,000 events, 25 MiB, 60 writes/min, 20 tokens.
- Legal: operating entity, governing law, minimum age, DCO or CLA.
- Approval to run gitleaks/trufflehog. (History was rewritten locally on 2026-11-10 so every commit uses `team@dayfile.app`.)
- ~~`tokens.last_used`~~ decided: yes.

## Residual-risk summary

- **Web compromise in M16** can mint tokens through the admin API (so it can read) but only with rate limits and visible
  audit events in the tenant's own log.
- **Backup deletion by a compromised server** is possible because Spaces has no write-only keys; layers B and D cover it.
- **Tenant slugs are public** through certificate transparency logs; the signup page must say so.
- **Append-only** means individual events cannot be erased; the terms must say so in plain words.
