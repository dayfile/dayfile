# Contributing

Thanks for helping. Read `docs/SPEC.md` (the event model) and `CLAUDE.md` (the invariants: append-only,
one timestamp format, seq cursors, deny-by-default audiences, no secret logging).

```
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

- Write the test first for anything covered by an invariant, and update `docs/SPEC.md` in the same change.
- One milestone-sized change per pull request; commit messages name the step (`M10: ...`).
- Ask before adding a dependency. The runtime set is deliberately small.
- By contributing you agree that your work is licensed under AGPL-3.0-only, the project license.
- Security problems: see `SECURITY.md`, not a public issue.
