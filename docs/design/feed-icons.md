# Design: icons in Atom and JSON Feed

Status: the app serves `/favicon.svg` and `/favicon.ico` on every host (`src/dayfile/icons.py`). The feeds do not mention any
icon, so feed readers fall back to guessing or show a blank.

## Fields

| Format | Field | Spec guidance |
|---|---|---|
| JSON Feed 1.1 | `icon` | square image, "relatively large", 512 × 512 recommended |
| JSON Feed 1.1 | `favicon` | small, 64 × 64 recommended |
| Atom (RFC 4287) | `<icon>` | square, small, 1:1 |
| Atom | `<logo>` | wider, 2:1 |

Readers are inconsistent about SVG; PNG is the safe format for `icon`, `favicon` and Atom `<icon>`. Atom `<logo>` is
optional and can be omitted.

## Design

- **Assets**: render the existing SVG to `icon-512.png` and `icon-64.png` once (headless Chrome, as for the screenshots) and
  commit them next to `favicon.ico`; no image library at runtime or as a new dependency.
- **Routes**: add `/icon-512.png` and `/icon-64.png` to `ICONS`, served with the same headers as the favicon (public,
  `max-age=86400`, GET/HEAD, ahead of tenant gating).
- **Feed output**: absolute URLs built from the request host, *without* any token or query: `https://<host>/icon-512.png`.
  Because the output is the same for every credential, invariant 8 (cache leaks) is unaffected. Tests assert the icon URL
  never contains `/t/` and the feed still validates.
- **Behind a proxy**: use the same host-derivation as the existing `self` links so the scheme is `https`.
- **Self-hosted installs** get the dayfile icon unless `DAYFILE_FEED_ICON` points elsewhere (small, optional).
- **Not** exposing any per-tenant custom icon in v1.

## Acceptance

Atom and JSON Feed render the icon fields; both PNGs are served with correct media types; a feed validator (W3C Feed
Validation for Atom, the JSON Feed spec's reference validator) accepts them; the existing renderer tests pass unchanged.

## Size

Roughly 40 lines plus two binary files and about eight tests. Good candidate to do first because it needs nothing from the
operator.
