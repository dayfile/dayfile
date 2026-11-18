# Running dayfile as a managed service

The same code runs both editions. Self-hosting is the default and needs none of this. The managed-service profile
adds a **tenancy layer** (this repository) and expects a **control plane** (not in this repository) around it.

```
 customer ──https──► Caddy ──► dayfile (one process) ──► /data/tenants/<slug>/dayfile.db  (one file per tenant)
                      │  ▲                                      │
         on-demand TLS└──┘ GET /internal/tls-ask               ├─► Litestream ─► object storage (continuous)
                                                                └─► backup-tenants.sh ─► object storage (nightly)
 control plane ── dayfile tenant create|suspend|limit|usage|delete  (CLI or tenant.json)
```

## Domains

| Domain | Role |
|---|---|
| `dayfile.cloud` | Tenant feeds and the API: `<slug>.dayfile.cloud`. Wildcard DNS, certificates on demand. Never serves a login page or sets cookies. |
| `dayfile.app` | Site, docs, signup and the future web UI (today the `dayfile.web` app: landing, docs, legal drafts and a read-only log viewer, in its own container with no tenant files; see SPEC section 10). Kept on a separate registrable domain so a tenant can never share cookies with the site. |

Keep the proxy for `dayfile.cloud` direct to the server: tokens appear in URLs, and a TLS-terminating CDN would see them.
A CDN in front of `dayfile.app` alone is fine, because that site holds no tokens.

## What lives where

| Concern | Where | State |
|---|---|---|
| Event log, tokens, lifecycle, feeds, JSON API | core | built |
| Subdomain → database, isolation between tenants | `tenancy.py` | built (M12) |
| Suspension, event quota, write-rate limit, body-size limit | core + `tenant.json` | built (M12) |
| Certificates per new subdomain | Caddy on-demand TLS + `/internal/tls-ask` | built, not run |
| Backups: nightly copies and continuous replication | `deploy/cloud` | written, not run |
| Metering for billing | `dayfile tenant usage` (JSON) | built |
| Signup, login, billing, plans, invoices | control plane | **not built** |
| Web UI for viewing events and managing tokens | separate app | **not built** |
| Mailbox ingestion for tenants | not offered | by design: use webhooks or forwarding, so the service never holds customers' mail credentials |

## The control-plane contract

A control plane needs only these operations, all idempotent to retry after checking state:

| To do this | Run |
|---|---|
| Create a tenant | `dayfile tenant create <slug> [--max-events N] [--write-limit E/S]` → prints the database path |
| Give them a first credential | `dayfile --url https://<slug>.<domain> token create --labels self --db <path>` (add `--write-source ID` for a write token) |
| Change a plan | `dayfile tenant limit <slug> --max-events N --write-limit E/S` (`none` clears) |
| Suspend or resume | `dayfile tenant suspend\|resume <slug>` |
| Meter usage | `dayfile tenant usage` → JSON: id, events, bytes, limits, suspended |
| Delete | `dayfile tenant delete <slug> --yes` → data moves to `.trash/` |

Any of these can instead edit `<root>/<slug>/tenant.json` directly (written atomically; read on every request).
The DNS record is a single wildcard `*.<domain>`; certificates are issued on first visit, and only for live tenants.

## Properties the design depends on

- **Isolation by file.** A bug in routing could cross tenants; a bug in a query cannot, because each file holds one tenant. Tests cover cross-tenant tokens, writes, ETags and unknown hosts. Review any change to `tenancy.py` and `gate()` in `app.py` as security-critical.
- **One process, one worker.** SQLite allows one writer and the write rate limit is in memory. Scale up the machine first. Scaling out means sharding tenants across machines, with routing by tenant, not adding workers.
- **Append-only vs. erasure.** Account deletion removes the whole file (and, separately, its backups and replicas). Removing single events is not offered. State this in the terms.
- **Tokens in URLs.** The proxy keeps no access log, and the application redacts `/t/<token>/`. Encourage Bearer headers for programs.

## Known gaps before real customers

1. A control plane and web UI.
2. Abuse controls beyond per-token write limits: per-IP limits at the edge, signup verification.
3. Per-IP limits at the edge. The app limits per token (reads, writes, held streams), but a flood of unauthenticated requests is only cheap to refuse, not free; Caddy needs a rate-limit plugin, or use the host firewall or fail2ban.
4. Wildcard certificates (DNS-challenge Caddy build) once signups outgrow Let's Encrypt's per-domain issuance limits.
5. Restore drills: both backup paths are written and unit-tested against stubs but have never run against real storage.
6. Metrics and alerting; `/healthz` and `tenant usage` are the only signals.
