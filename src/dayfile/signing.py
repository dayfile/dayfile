"""Ed25519 event signatures over the RFC 8785 canonical form, plus the key store."""

import base64
import binascii
import hashlib
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# kid becomes a file name, so it must start alphanumeric and contain no path separators.
KID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NOT_SIGNED = ("sig", "seq", "recorded_at", "status", "sig_status")
SIGNATURE_BYTES = 64


def b64(data: bytes) -> str:
    """Standard base64, as used in `sig.value` and exported public keys."""
    return base64.b64encode(data).decode()


def default_key_dir() -> Path:
    """Where private keys live: $DAYFILE_KEY_DIR or ~/.config/dayfile/keys."""
    return Path(os.environ.get("DAYFILE_KEY_DIR") or Path.home() / ".config" / "dayfile" / "keys")


def signing_form(event: dict[str, Any]) -> dict[str, Any]:
    """The part of an event a signature covers, with defaults dropped so it is storable.

    Server-assigned fields and `sig` are removed. Fields equal to their default (urgency 0,
    empty tags/refs/links, a null expires_at) are omitted, so an event reconstructed from the
    database has the same form as the one the author signed.
    """
    form = {k: v for k, v in event.items() if k not in NOT_SIGNED}
    if form.get("urgency") == 0:
        del form["urgency"]
    if form.get("expires_at") is None:
        form.pop("expires_at", None)
    for field in ("tags", "refs", "links"):
        if field in form and not form[field]:
            del form[field]
    return form


def canonical_bytes(event: dict[str, Any]) -> bytes:
    """RFC 8785 canonical JSON of the signing form."""
    return rfc8785.dumps(signing_form(event))


def sign_event(event: dict[str, Any], kid: str, key: Ed25519PrivateKey) -> dict[str, Any]:
    """Complete an event (normalize, fill defaults) and attach a signature over that form."""
    from dayfile.ingest.core import prepare  # imported late: ingest verifies signatures too

    completed = prepare(event)
    value = b64(key.sign(canonical_bytes(completed)))
    return {**completed, "sig": {"kid": kid, "alg": "ed25519", "value": value}}


def _signature_bytes(sig: Any) -> bytes | None:
    if not isinstance(sig, dict) or sig.get("alg") != "ed25519":
        return None
    try:
        raw = base64.b64decode(sig.get("value", ""), validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None
    return raw if len(raw) == SIGNATURE_BYTES else None


def verify_event(event: dict[str, Any], public_key: Ed25519PublicKey) -> bool:
    """True if the event's signature matches its signing form under `public_key`."""
    raw = _signature_bytes(event.get("sig"))
    if raw is None:
        return False
    try:
        public_key.verify(raw, canonical_bytes(event))
    except (InvalidSignature, ValueError, TypeError, rfc8785.CanonicalizationError):
        return False
    return True


# --- key store ---------------------------------------------------------------


def _check_kid(kid: str) -> str:
    if not KID.match(kid):
        raise ValueError("key id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return kid


def create_key(
    conn: sqlite3.Connection, key_dir: Path, kid: str | None = None, note: str | None = None
) -> str:
    """Generate a key pair: private PEM into `key_dir` (mode 0600), public key into the database."""
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    default = "key-" + hashlib.sha256(public).hexdigest()[:8]
    kid = _check_kid(default if kid is None else kid)
    if public_key_for(conn, kid) is not None:
        raise ValueError(f"key {kid!r} already exists")
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        fd = os.open(key_dir / f"{kid}.pem", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError(f"a private key file for {kid!r} already exists") from None
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    conn.execute(
        "INSERT INTO keys (kid, public_key, note) VALUES (?, ?, ?)", (kid, b64(public), note)
    )
    return kid


def import_public_key(
    conn: sqlite3.Connection, kid: str, public_b64: str, note: str | None = None
) -> None:
    """Trust someone else's public key (base64 of the 32 raw bytes)."""
    _check_kid(kid)
    try:
        raw = base64.b64decode(public_b64, validate=True)
        Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError):
        raise ValueError("public key must be base64 of a 32-byte Ed25519 key") from None
    try:
        conn.execute(
            "INSERT INTO keys (kid, public_key, note) VALUES (?, ?, ?)", (kid, b64(raw), note)
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"key {kid!r} already exists") from None


def public_key_for(conn: sqlite3.Connection, kid: str) -> Ed25519PublicKey | None:
    """Return the trusted public key for a key id, if registered."""
    row = conn.execute("SELECT public_key FROM keys WHERE kid = ?", (kid,)).fetchone()
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(row[0])) if row else None


def load_private_key(key_dir: Path, kid: str) -> Ed25519PrivateKey:
    """Read a private key created by `create_key`."""
    try:
        key = serialization.load_pem_private_key(
            (key_dir / f"{_check_kid(kid)}.pem").read_bytes(), None
        )
    except FileNotFoundError:
        raise ValueError(f"no private key {kid!r} in {key_dir}") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{kid!r} is not an Ed25519 key")
    return key


def list_keys(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Describe registered public keys (id, fingerprint, note, date)."""
    return [
        {
            "kid": r["kid"],
            "fingerprint": hashlib.sha256(base64.b64decode(r["public_key"])).hexdigest()[:16],
            "note": r["note"],
            "created_at": r["created_at"],
        }
        for r in conn.execute("SELECT * FROM keys ORDER BY created_at, kid")
    ]


# --- read-time status --------------------------------------------------------


def status(conn: sqlite3.Connection, event: dict[str, Any]) -> str | None:
    """None if unsigned; else `valid`, `invalid`, or `unknown-key` (checked at call time)."""
    sig = event.get("sig")
    if sig is None:
        return None
    if _signature_bytes(sig) is None:
        return "invalid"
    public = public_key_for(conn, sig.get("kid", ""))
    if public is None:
        return "unknown-key"
    return "valid" if verify_event(event, public) else "invalid"


def annotate(conn: sqlite3.Connection, events: list[dict[str, Any]]) -> None:
    """Add `sig_status` to each signed event in place."""
    for ev in events:
        if "sig" in ev:
            ev["sig_status"] = status(conn, ev)
