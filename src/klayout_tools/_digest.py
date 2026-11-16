"""SHA-256 digest helpers shared by the verbs' provenance envelopes.

Re-exported from :mod:`klayout_tools._provenance`, which stays the public
home of these names.
"""

from __future__ import annotations

import hashlib
import os


def sha256_file(path: str | None) -> str | None:
    """Streamed SHA-256 hex digest of the file at ``path``, or ``None``.

    Returns ``None`` for a falsy path or one that is not an existing file --
    the defensive shape ``sim.py`` already relied on, now shared by every
    caller (``lvs``/``extract`` only pass paths that exist at hash time, so
    the guard is a no-op there).
    """
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_required(path: str) -> str:
    """:func:`sha256_file` for a path that must exist: raises
    ``FileNotFoundError`` instead of returning ``None``."""
    digest = sha256_file(path)
    if digest is None:
        raise FileNotFoundError(path)
    return digest


def content_hash(path: str | None) -> str | None:
    """The envelope's ``sha256:``-prefixed deck-hash form of ``path``, or
    ``None`` when the file can't be hashed."""
    digest = sha256_file(path)
    return f"sha256:{digest}" if digest is not None else None


def content_hash_required(path: str) -> str:
    """:func:`content_hash` for a path that must exist: raises
    ``FileNotFoundError`` instead of returning ``None``."""
    return "sha256:" + sha256_file_required(path)


def content_hash_bytes(data: bytes) -> str:
    """The ``sha256:``-prefixed digest of in-memory ``data``."""
    return "sha256:" + hashlib.sha256(data).hexdigest()
