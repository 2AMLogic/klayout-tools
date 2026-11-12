"""Fleet PDK bundle version stamp from ``.pdk-store.json`` (issue #5318).

Bundle installs carry no ``SOURCES``; the bundle's ``.pdk-store.json`` sits one
level above the real (symlink-resolved) variant dir. Read only, never written.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

#: Upper bound on bytes read from ``.pdk-store.json``.
_STORE_FILE_MAX = 65536


def read_store_version(variant_dir: str) -> str | None:
    """The string ``version`` of ``.pdk-store.json``.

    Missing/unreadable/non-object/non-string/empty → ``None`` (never guessed).
    """
    path = os.path.join(
        os.path.dirname(os.path.realpath(variant_dir)), ".pdk-store.json"
    )
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_STORE_FILE_MAX + 1)[:_STORE_FILE_MAX]
        data = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def read_store_stamp(variant_dir: str) -> str | None:
    """``store:<version>`` for :func:`pdk.read_version`, or ``None``."""
    store = read_store_version(variant_dir)
    return f"store:{store}" if store else None


def store_pdk_stamp(variant: str, variant_dir: str) -> dict[str, Any] | None:
    """Client-side ``store-version-sha256`` stamp for batch comparison."""
    store = read_store_version(variant_dir)
    if store is None:
        return None
    return {
        "variant": variant,
        "stamp_scheme": "store-version-sha256",
        "stamp_sha256": hashlib.sha256(store.encode("utf-8")).hexdigest(),
    }
