# Design: scanning the history and making the repository public

Status: private repository `dayfile/dayfile`, 26 commits, one author identity, CI on push (ruff + pytest on a Python
matrix). Not yet public.

## Preliminary scan (done in this session, read-only)

Without installing tools, I searched every commit (`git rev-list --all`) for:

- private-key headers, AWS-style keys, GitHub tokens, DigitalOcean tokens (`dop_v1_`), and strings shaped like dayfile tokens
  (`<12 hex>.<30+ url-safe>`): **no matches**
- the Droplet's IP `<droplet-ip>`: **none**
- file names ever tracked that look sensitive (`.env`, `*.pem`, `*.key`, `*.db`, `secret*`, `id_*`): **none**; `dayfile.db` in
  the working tree is ignored by `.gitignore`
- the old contact domain: all commits and `SECURITY.md` once used another address. **History was rewritten on 2026-11-10** (authors,
  committers and file text) so every commit now carries `team@dayfile.app`. The local branch `backup-before-rewrite` holds the old history;
  delete it before publishing.

This is a first pass. It does not replace the tools below.

## Before flipping to public

1. **Run real scanners over full history**: `gitleaks git --log-opts="--all"` and `trufflehog git file://. --since-commit=''`
   (both read-only; verify verified-secret mode on). Also scan the working tree and `docs/`. I will run them if the operator
   installs them or approves a container run (`docker run --rm -v $PWD:/repo zricethezav/gitleaks`), since adding tools to the
   system is the operator's call.
2. **Review everything that names the deployment**: `deploy/cloud/README.md`, `cloud-init.yaml` (placeholders for keys),
   docs mention `dayfile.cloud`/`dayfile.app` and the Droplet size: acceptable, they are the product's domains. No IPs,
   key paths, deploy-key names or personal hostnames should remain; re-grep for `/home/giladg`, `giladg`, `.ssh`, and the
   deploy key name.
3. **Rotate anything that ever appeared in a transcript**, since the deployment sessions displayed tokens: the `dogfood` write
   and read tokens (revoke and mint new ones), and the read-only GitHub deploy key if its private half was ever printed. The
   tokens were not committed, but rotation costs a minute and removes doubt.
4. **Repository hygiene**: `LICENSE` (AGPL-3.0, present), `SECURITY.md` (present; switch the contact to `security@dayfile.app`
   after the mailbox exists), `CONTRIBUTING.md` (add DCO text), `CODE_OF_CONDUCT.md` (Contributor Covenant), issue and PR
   templates, a README that states plainly that it is pre-1.0 and what is not built (accounts, billing).
5. **GitHub settings**: enable private vulnerability reporting (SECURITY.md already mentions the button), secret scanning with
   push protection, Dependabot alerts and security updates, branch protection on `master` (require CI, no force-push), 2FA for
   every org member, and make `dayfile` org policies require it.
6. **CI additions** (each is a new workflow step, no runtime dependency): `pip-audit` (or `uv` equivalent) on the lockfile, a
   gitleaks step on pull requests, `actions/*` pinned to commit SHAs, `permissions: contents: read` at the top, a weekly
   schedule for the audit. Build the Docker image in CI and run the existing `tests/test_deploy.py` checks.
7. **Supply chain**: the vendored `htmx.min.js` has a test pinning its hash and a NOTICE file; keep. `uv.lock` committed.
   Release process: tag, changelog, signed tags, container image to GHCR later.
8. **Trademark and name check** before announcing (see `legal-review.md`).

## Decision: keep history or start clean

| | Keep history | Squash to one commit |
|---|---|---|
| Pros | full provenance, milestones visible | nothing old can leak |
| Cons | any past mistake is public | loses authorship history |

Recommendation: keep history if both scanners are clean and the author email is acceptable; otherwise rewrite the email with
`git filter-repo --mailmap` before publishing. Squashing is not needed on current evidence.

## Inputs from the operator

- Approve running gitleaks/trufflehog (container or install).
- Confirm the repository owner name and the license (AGPL-3.0) are final.

## Acceptance

Two clean scanner reports kept in the PR description, GitHub protections enabled, CI runs the audit and secret scan,
rotated credentials recorded in the runbook, then visibility changed by the operator themself (I will not flip it).
