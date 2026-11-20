# Publishing to the MCP Registry

The official registry (registry.modelcontextprotocol.io) lists `io.github.dayfile/dayfile`, described
by `server.json` at the repository root. These are the owner's steps. Nothing here runs by itself.

The registry checks two things: that you control the `io.github.dayfile` namespace (you sign in to
GitHub as a member of the `dayfile` organization), and that you own the PyPI package (the string
`mcp-name: io.github.dayfile/dayfile` appears in the package's README on PyPI, which `README.md` has
in a hidden comment).

## Each time

1. **Release to PyPI first.** `server.json` names a PyPI version (`packages[0].version`, and the
   top-level `version`), and the registry reads the README of that release. Bump `version` in
   `pyproject.toml` and in both places in `server.json` to the same number (a test fails if they
   differ), add a `CHANGELOG.md` entry, merge, then publish a GitHub release as usual. Wait until
   `https://pypi.org/project/dayfile/<version>/` shows it.
2. **Install the publisher.** Get `mcp-publisher` from the registry's releases at
   <https://github.com/modelcontextprotocol/registry/releases> (or `brew install mcp-publisher`).
3. **Validate.** From the repository root: `mcp-publisher validate`.
4. **Sign in.** `mcp-publisher login github` opens a device-code flow. Use a GitHub account that
   belongs to the `dayfile` organization.
5. **Publish.** `mcp-publisher publish`.
6. **Check it.**
   `curl -s 'https://registry.modelcontextprotocol.io/v0/servers?search=io.github.dayfile/dayfile'`
   should return the server with `"isLatest": true` and the `https://dayfile.cloud/mcp` remote.

## Notes

- The registry rejects a version it already has. Publish a new version for every change to
  `server.json`.
- The local package entry runs `uvx --with "mcp>=2.3" dayfile==<version> mcp`: the `mcp` extra's
  dependency is passed with `--with` because `server.json` cannot name extras.
- `DAYFILE_URL` has no default because a log's address contains its owner's username.
- A workflow that publishes on release was left out on purpose: it would download and run a binary
  in CI. Add one only if you want that.
