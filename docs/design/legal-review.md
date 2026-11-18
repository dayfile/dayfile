# Design: getting the legal pages reviewed

Status: `terms.html` and `privacy.html` are drafts written by the assistant, labelled "Draft" on the page. They are not
legal advice and must be reviewed by a lawyer before anyone outside the operator's circle signs up. This note prepares
that review.

## Decisions only the operator can make

1. **Who operates the service**: a registered company, or the individual? The site says "the operator". Counsel will want
   the legal name, jurisdiction and a postal address (required in some places for a privacy contact).
2. **Governing law and venue**: blank in the draft.
3. **Who it is for**: a closed beta by invite, which keeps the exposure small. Minimum age (the draft says 16): confirm.
4. **Whether to charge**: billing is out of scope for M16, so the terms say "free beta, no SLA". Payment adds consumer-law,
   tax and refund terms later.

## What the drafts say, and what needs checking

| Topic | Draft position | Question for counsel |
|---|---|---|
| Service level | beta, no uptime promise, may change | enough disclaimer for the jurisdiction? |
| Acceptable use | no abuse, no illegal content, no attempts to read other tenants | takedown procedure and notice-and-action expectations (EU DSA hosting rules); DMCA designated agent if US users post public content. Feeds are private behind tokens, which reduces but does not remove this |
| Content ownership | the user owns their events; license to store and serve only | wording on backups, cache, replicas |
| Deletion | account deletion erases the log at once; backups age out in 14 days; replicas in 7 | confirm this satisfies erasure rights; **append-only means single events cannot be removed**, only the whole log: is that acceptable to say plainly? |
| Termination | operator may suspend for abuse | notice and appeal wording |
| Liability | limited | enforceability limits by jurisdiction |
| Security incidents | notify affected users | breach notification clocks (GDPR 72 h to the authority; state laws in the US) |
| AGPL | the software is AGPL-3.0; the hosted service offers its source | confirm network-use obligations are met by linking the repository and any modifications |

## Privacy: GDPR-style review list

- **Roles.** Controller for account data (GitHub id and login, invite and terms acceptance, sessions, admin audit). Processor
  for the content of events users write. A data processing agreement text should exist for users who ask.
- **Legal bases** for each purpose (contract, legitimate interest for abuse prevention).
- **Retention table**: sessions (30 days max), oauth state (10 minutes), audit log (**[decide]** 12 months), invites and
  redemptions (until account deletion plus 90 days), backups (14 days plus monthly keepers 12 months, which conflicts with
  the 30-day deletion promise: either drop monthly keepers from the plan or state the 12 months honestly; I recommend
  dropping them for tenant data).
- **Subprocessors**: DigitalOcean (hosting, backups storage), GitHub (sign-in), Let's Encrypt (certificate issuance sees
  tenant hostnames in Certificate Transparency logs, which makes **tenant slugs public**: say so on the signup page), the mail
  forwarder, the uptime/heartbeat providers (they see no user data).
- **International transfers**: US hosting for EU users; whether standard clauses are needed for DigitalOcean.
- **Cookies**: only strictly necessary ones (`__Host-dayfile-session`, `__Host-dayfile-oauth`, `__Host-dayfile`); no tracking, no
  banner needed under most readings, but ask.
- **Data subject requests**: access (export), erasure (delete account), portability (JSON Lines/SQLite export). Documented
  as built by M16e.
- **Logs**: Caddy discards access logs and the app redacts tokens; confirm that no IP address is retained anywhere
  (journald and Docker logs rotated, DO's own network logs are theirs).

## Mechanics to implement with the review

- Version the terms (`terms_version` constant; the account table stores which version and when each user accepted).
  Material changes require re-acceptance at next sign-in.
- Show the change date at the top of both pages; keep old versions in git (the site is open source, history is public).
- Remove the "Draft" banner only when counsel has signed off, in a commit whose message names who and when.
- A short plain-language summary box at the top of each page, written to match the legal text, not instead of it.
- Add `Contact: privacy@` and `abuse@` once the mailbox exists (`dns-and-mail.md`).

## Other things counsel should look at

- Trademark and name: "dayfile" (clearance search in the operator's jurisdiction and where users are), and the domains.
- Open-source licensing of the site's own text and artwork (the favicon is original; decide its license, AGPL for code
  and a content license such as CC BY for docs).
- Contributor terms: `CONTRIBUTING.md` has no CLA or DCO. Under AGPL a dual-licensing option would need one; decide now,
  before outside contributions arrive. Recommendation: DCO sign-off, no CLA, unless commercial relicensing is a goal.

## Repository split and AGPL

If the managed-service code (accounts, dashboard, billing) moves to a separate private repository that talks to the core only over
HTTP, ask counsel to confirm that it is a separate work for AGPL purposes and that the public core, which the operator also runs
and may modify, still meets the network-use source offer. Also decide DCO versus CLA now: only a CLA keeps the option of
relicensing contributed code.

## Process

1. Operator answers the four decisions above.
2. I update the drafts and add the retention table, subprocessors and AGPL paragraph to match the final design.
3. Export `terms` and `privacy` to a single reviewable document (Markdown or Word) with this list as the cover note.
4. Counsel reviews; changes come back as a redline; I apply them and remove the banner.

Nothing here has been sent to anyone.
