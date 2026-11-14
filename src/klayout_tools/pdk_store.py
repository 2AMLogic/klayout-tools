"""The submitter's PDK revision stamp, for comparison with an off-host runner's.

A stamp is the sha256 of a variant install's ``SOURCES`` text
(``sources-sha256``), falling back to the bundle's ``.pdk-store.json``
``version`` (``store-version-sha256``, issue #5318) for a bundle install,
which carries no ``SOURCES``; that file sits one level above the real
(symlink-resolved) variant dir. Read only, never written.

:func:`client_pdk_stamp` is the entry point `klt sim`'s ``batch`` backend
uses: it stamps the install under a pinned root when the request or
``$PDK_ROOT`` names one, and otherwise the install the PDK search order
resolves (issue #5474) -- the same one ``provenance.pdk`` reports.
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


#: Upper bound on bytes read from a variant's ``SOURCES`` file.
_SOURCES_FILE_MAX = 16384


def sources_pdk_stamp(variant: str, variant_dir: str) -> dict[str, Any] | None:
    """Client-side ``sources-sha256`` stamp from ``<variant_dir>/SOURCES``.

    ``None`` when the file is missing or unreadable (the caller then falls
    back to :func:`store_pdk_stamp`). The text is newline-normalised and
    right-stripped per line before hashing, so a trailing-whitespace or CRLF
    difference is not reported as a different PDK revision.
    """
    try:
        with open(os.path.join(variant_dir, "SOURCES"), "rb") as handle:
            text = handle.read(_SOURCES_FILE_MAX + 1)[:_SOURCES_FILE_MAX].decode(
                "utf-8", "replace"
            )
    except OSError:
        return None
    normalized = "\n".join(
        line.rstrip() for line in text.replace("\r\n", "\n").strip().split("\n")
    )
    return {
        "variant": variant,
        "stamp_scheme": "sources-sha256",
        "stamp_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def resolved_variant_dir(variant: str) -> str | None:
    """The directory of ``variant`` as :func:`klayout_tools.pdk.find_pdk`'s
    search order resolves it (issue #5474), or ``None`` when no install on
    this host holds it.

    Used only when no root was pinned (``models.pdk_root`` / ``$PDK_ROOT``),
    so the submitter's own stamp describes the very install the run resolved
    its models from rather than being reported as unavailable. The import is
    deferred because :mod:`klayout_tools.pdk` imports this module.
    """
    from .pdk import PdkNotFoundError, find_pdk

    try:
        info = find_pdk(variant=variant)
    except (PdkNotFoundError, OSError, ValueError):
        return None
    root = info.get("root")
    name = info.get("variant")
    if not (isinstance(root, str) and root and isinstance(name, str) and name):
        return None
    # A flat, single-PDK install (issue #522) *is* its own variant directory;
    # a nested open_pdks store holds it as `<root>/<name>`.
    nested = os.path.join(root, name)
    return nested if os.path.isdir(nested) else root


def client_pdk_stamp(variant: Any, root: Any = None) -> dict[str, Any] | None:
    """The submitter's stamp for ``variant`` (``SOURCES``, else the bundle
    ``.pdk-store.json``): under ``root`` when one is pinned, otherwise from
    the install the PDK search order resolves (issue #5474).

    ``None`` when ``variant`` is not a non-empty string, no install holds it,
    or the resolved install has neither stamp source.
    """
    if not (isinstance(variant, str) and variant):
        return None
    if isinstance(root, str) and root:
        variant_dir: str | None = os.path.join(root, variant)
    else:
        variant_dir = resolved_variant_dir(variant)
    if variant_dir is None:
        return None
    return sources_pdk_stamp(variant, variant_dir) or store_pdk_stamp(
        variant, variant_dir
    )
