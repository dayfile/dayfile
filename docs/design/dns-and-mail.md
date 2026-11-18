# Design: DNS hardening and the team mailbox

Status: both zones are on DigitalOcean DNS with `A` records for `dayfile.cloud`, `*.dayfile.cloud` and `dayfile.app`. There is
no mail, CAA, `www` or security.txt. `team@dayfile.app` is referenced by `SECURITY.md`, the footer and the legal drafts but
does not receive mail.

## 1. The mailbox

### Needs

- **Inbound only** for now: `team@`, `security@`, `abuse@`, `privacy@`, `postmaster@`. RFC 2142 expects `abuse@` and
  `postmaster@`; `security@` is what researchers try first.
- No outbound mail from the service: M16 uses GitHub OAuth and invite codes, so there is no verification or notification
  email. That keeps the sending reputation, SPF/DKIM operations and a whole class of abuse out of scope.
- Replying *as* `team@` is wanted but not essential on day one.

### Options

| Option | Inbound | Send as `team@` | Notes |
|---|---|---|---|
| Forwarding service (ForwardEmail.net, ImprovMX, similar) | yes, to the operator's own inbox | some offer SMTP credentials; or Gmail "send mail as" | cheapest; works with DNS at DigitalOcean; decide by privacy posture (ForwardEmail is open source) |
| Registrar forwarding (Porkbun) | yes | no | the MX records would have to live in the DO zone; check that registrar forwarding works with external DNS before relying on it |
| Hosted mailbox (Fastmail, Google Workspace, Proton) | yes | yes | real inboxes and shared access; a monthly fee per user |

Recommendation: a forwarding service now, with all five aliases going to one inbox, and move to a hosted mailbox when a
second person needs access. **[decide]** provider. I do not hard-code its MX/TXT values here because the provider issues
them at signup; I will turn them into exact records for the DO console as soon as one is picked.

### Records (shape; values come from the provider)

| Name | Type | Value |
|---|---|---|
| `dayfile.app` | MX | the provider's two MX hosts |
| `dayfile.app` | TXT | SPF `v=spf1 include:<provider> -all` (a single SPF record) |
| `_dmarc.dayfile.app` | TXT | `v=DMARC1; p=reject; adkim=s; aspf=s; rua=mailto:dmarc@dayfile.app` (start with `p=none` for two weeks if sending is added) |
| DKIM | TXT/CNAME | only if the provider sends mail for us |

### Protect the tenant domain from spoofing

`dayfile.cloud` should never send mail. Publish: `MX 0 .` (null MX, RFC 7505), TXT `v=spf1 -all`, and `_dmarc` TXT
`v=DMARC1; p=reject`. Without these, anyone can send mail "from" `anything@dayfile.cloud` and some receivers will accept it.
These four records need no provider and can be added today.

### security.txt

Serve `/.well-known/security.txt` from `dayfile.web` (static route, tested): `Contact: mailto:security@dayfile.app`,
`Expires` (renewed yearly; a test fails 30 days before expiry), `Preferred-Languages: en`, `Policy` linking `SECURITY.md`
on GitHub once public. Update `SECURITY.md`, `docs`, legal drafts to use `security@` and `privacy@` where appropriate.

## 2. CAA

Restrict who may issue certificates for both zones. Caddy uses Let's Encrypt with ZeroSSL as fallback by default, so decide
which to allow. On-demand certificates are per host, not wildcards.

| Name | Type | Value |
|---|---|---|
| `dayfile.cloud` | CAA | `0 issue "letsencrypt.org"` |
| `dayfile.cloud` | CAA | `0 issuewild ";"` (no wildcard certificates; remove when moving to the DNS-challenge wildcard noted in `docs/CLOUD.md`) |
| `dayfile.cloud` | CAA | `0 iodef "mailto:security@dayfile.app"` |
| `dayfile.app` | CAA | same three |

Optional hardening: bind issuance to the account with `accounturi=` (the ACME account URL is in Caddy's data volume).
**[decide]** allow ZeroSSL too: add `0 issue "sectigo.com"`, or pin the Caddyfile to Let's Encrypt only
(`acme_ca`/`issuer acme`), recommended so that CAA and Caddy agree. After any change, request a certificate for a new test
tenant and confirm issuance still works before relying on it.

## 3. `www`

`www.dayfile.app` has no record. Add `A www.dayfile.app → <droplet-ip>` and a Caddy site block
`www.dayfile.app { redir https://dayfile.app{uri} permanent }`. The test in `tests/test_deploy.py` checks the block order
(site blocks before the on-demand catch-all). No `www` for `dayfile.cloud` (tenant namespace, reserved).

## 4. Other DNS and registrar items

- **Reserved IP:** reserve the Droplet's public IP in DO (free while assigned) so a rebuild keeps the address. Then
  `A` records never change.
- **IPv6:** not enabled; skip until needed, then add `AAAA` for all three names at once.
- **DNSSEC:** check whether DigitalOcean DNS offers it when this is implemented; if not, note the gap.
- **HSTS:** `.app` is on the browser preload list as a whole TLD, so `dayfile.app` is HTTPS-only already. Do not add
  `preload` to `dayfile.cloud`'s header: removal from the list is slow.
- **Porkbun:** two-factor sign-in on, registrar lock on, auto-renew on, WHOIS privacy on, and a payment method that will not
  expire. If either domain lapses, every tenant URL and the OAuth callback die. Put renewal dates in the runbook.

## Inputs from the operator

- Pick the mailbox provider, or tell me to choose the recommended forwarder.
- Add the DNS records in the DO console (I will provide the finished table), or give a scoped DO token and I will use
  `doctl`; either is fine, the console is safer.
- Decide the ZeroSSL question above.

## Acceptance

`dig` shows each record; a mail sent to each alias arrives; a spoof test to `x@dayfile.cloud` is rejected by receivers
that check DMARC; a fresh tenant still gets a certificate after CAA is set; `www` redirects.
