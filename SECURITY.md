# Security policy

Please report vulnerabilities privately, by email to team@dayfile.app or through GitHub's
"Report a vulnerability" button on this repository. Do not open a public issue.
Include what you found, how to reproduce it, and the version or commit.

Of particular interest: credential or audience leaks between tokens, token values reaching logs,
ways to write outside a write token's sources or labels, and ways to alter or delete stored events.

Supported versions: the latest release only while the project is pre-1.0.

Deployment notes: run dayfile behind HTTPS; keep the database file and backups private (mode 600);
treat `/t/<token>/` URLs as secrets.
