"""Accounts for dayfile.app (M16, not built; see docs/design/m16-accounts.md).

Kept in its own package so it can move to a separate repository as a unit. It may use the core only
through the public HTTP API and the admin API, never by importing storage code.
"""
