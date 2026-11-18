# Restore drills

A backup counts only once it has been restored. Run `deploy/cloud/restore-drill.sh` from your own
machine with the read-only key, after the first nightly run, then quarterly and after any change to
the backup code. Paste the row it prints below.

| Date | Backup | Restored | Elapsed | Result |
|---|---|---|---|---|
| 2026-11-18 | 20261117T230154Z | 2 tenants + accounts database | 1s | ok |
