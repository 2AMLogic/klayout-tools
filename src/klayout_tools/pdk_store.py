"""The submitter's PDK revision stamp, for comparison with an off-host runner's.

A stamp is the sha256 of a variant install's ``SOURCES`` text
(``sources-sha256``), falling back to the bundle's ``.pdk-store.json``
``version`` (``store-version-sha256``, issue #5318) for a bundle install,
which carries no ``SOURCES``; that file is accepted either at the variant
root (where the fleet's provisioner writes it, issue #5624) or one level
above the real (symlink-resolved) variant dir (#5318's documented
placement) -- the nearest one wins. Read only, never written.

:func:`client_pdk_stamp` is the entry point `klt sim`'s ``batch`` backend
uses: it stamps the install under a pinned root when the request or
``$PDK_ROOT`` names one, and otherwise the install the PDK search order
resolves (issue #5474) -- the same one ``provenance.pdk`` reports.

The same marker says what placed the tree (issue #8007):
:func:`read_store_install` carries its ``{sha256, version, via, synced}`` and
:func:`store_layout_of` the layout the tree sits in, into ``klt pdk find``
and ``provenance.pdk``. A batch worker cannot import this package, so
:func:`worker_store_evidence_source` ships it the same reading as
stdlib-only source; ``tests/test_sim_batch_store_evidence.py`` holds the two
to one answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from typing import Any

#: Upper bound on bytes read from ``.pdk-store.json``.
_STORE_FILE_MAX = 65536

#: The bundle stamp file ``read_store_version``/``read_store_sha256`` read.
PDK_STORE_MARKER = ".pdk-store.json"

#: What a ``.pdk-store.json`` records about the install that reports carry
#: (issue #8007), in report order. Nothing else in the marker -- not ``key``,
#: a store location -- reaches a report.
INSTALL_FIELDS: tuple[str, ...] = ("sha256", "version", "via", "synced")

#: Longest ``via``/``synced`` string carried into a report: the bound the
#: batch worker capture clips a captured string to.
INSTALL_TEXT_MAX = 300

#: Closed vocabulary of :func:`store_layout_of`.
STORE_LAYOUTS: tuple[str, ...] = ("manager_family", "legacy_manager", "flat")

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def _nearest_store_object(variant_dir: str) -> dict[str, Any] | None:
    """The parsed nearest ``.pdk-store.json``, or ``None`` when there is none.

    Searched at ``<variant_dir>/.pdk-store.json`` (the variant root, where
    the fleet's provisioner writes it, issue #5624) and then one level above
    the real (symlink-resolved) variant dir (#5318's documented placement).
    The nearest stamp wins: one that is present but unreadable, malformed,
    or not an object is **not** skipped in favour of a farther one (mirroring
    :func:`klayout_tools.pdk_compat.read_fetched_marker`).
    """
    candidates = [
        os.path.join(variant_dir, PDK_STORE_MARKER),
        os.path.join(os.path.dirname(os.path.realpath(variant_dir)), PDK_STORE_MARKER),
    ]
    for path in dict.fromkeys(candidates):
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        return _read_store_object(path)
    return None


def _read_store_object(path: str) -> dict[str, Any] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_STORE_FILE_MAX + 1)
        if len(raw) > _STORE_FILE_MAX:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _store_field(variant_dir: str, field: str) -> str | None:
    """The string ``field`` of the nearest ``.pdk-store.json``, or ``None``."""
    data = _nearest_store_object(variant_dir)
    if data is None:
        return None
    value = data.get(field)
    return value if isinstance(value, str) else None


def read_store_version(variant_dir: str) -> str | None:
    """The string ``version`` of the nearest ``.pdk-store.json``.

    Missing/unreadable/non-object/non-string/empty → ``None`` (never guessed).
    """
    value = _store_field(variant_dir, "version")
    return (value or "").strip() or None


def read_store_sha256(variant_dir: str) -> str | None:
    """Return the nearest stamp's 64-digit ASCII hex digest, preserving case.

    A missing or malformed digest is unknown. The unpacked tree cannot be
    hashed back to the original tar bytes.
    """
    value = _store_field(variant_dir, "sha256")
    return value if _HEX64.fullmatch(value or "") else None


def read_store_stamp(variant_dir: str) -> str | None:
    """``store:<version>`` for :func:`pdk.read_version`, or ``None``."""
    store = read_store_version(variant_dir)
    return f"store:{store}" if store else None


def read_store_install(variant_dir: str) -> dict[str, str | None] | None:
    """What the nearest ``.pdk-store.json`` records about the install (issue
    #8007): ``{sha256, version, via, synced}`` (:data:`INSTALL_FIELDS`).

    Each fact is the marker's own non-empty string, or ``None`` when the
    marker carries no usable one: ``sha256`` only when it is exactly 64 hex
    digits, ``via``/``synced`` clipped to :data:`INSTALL_TEXT_MAX`. ``None``
    when there is no evidence at all -- no marker, or a nearest marker that is
    unreadable, oversized, malformed, not an object, or names none of the
    four. Nothing is inferred from directory names or from tools on ``PATH``:
    neither can say what placed a tree, so an install without a marker
    reports its placing tool as unknown.
    """
    data = _nearest_store_object(variant_dir)
    return install_facts(data) if data is not None else None


def install_facts(data: dict[str, Any]) -> dict[str, str | None] | None:
    """Validate the install fields in a store marker or a collected worker report."""
    digest = data.get("sha256")
    facts: dict[str, str | None] = {
        "sha256": digest
        if isinstance(digest, str) and _HEX64.fullmatch(digest)
        else None,
        "version": _install_text(data.get("version")),
        "via": _install_text(data.get("via"), INSTALL_TEXT_MAX),
        "synced": _install_text(data.get("synced"), INSTALL_TEXT_MAX),
    }
    return facts if any(facts.values()) else None


def _install_text(value: Any, limit: int | None = None) -> str | None:
    """A marker string as one carried fact: stripped and non-empty, else
    ``None``; clipped to ``limit`` characters when given."""
    if not isinstance(value, str) or not (text := value.strip()):
        return None
    return text[:limit] if limit is not None else text


def _parts_under(base: str, real: str) -> list[str] | None:
    """Path components of ``real`` below ``base`` -- ``[]`` when they are the
    same directory -- or ``None`` when ``real`` is not inside ``base``."""
    try:
        relative = os.path.relpath(real, base)
    except ValueError:  # different drives on Windows
        return None
    if relative == os.curdir:
        return []
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    return relative.split(os.sep)


def store_layout_of(root: str, variant_dir: str) -> str | None:
    """The layout the variant at ``variant_dir`` was found in (issue #8007).

    Follows an enabled symlink and measures the resolved tree from the first
    of the conventional stores (:data:`klayout_tools.pdk.STORE_DIRS`) and the
    searched ``root`` that holds it in a version-manager shape::

        manager_family  <root>/<manager>/<family>/versions/<commit>/<variant>
        legacy_manager  <root>/<family>/versions/<commit>/<variant>
        flat            no version-manager shape: the root itself, a real
                        ``root/<variant>`` directory, or a link to a plain
                        directory

    A store is asked before ``root`` because that is where a manager's layout
    is defined: ``$PDK_ROOT`` often holds only the enabled links, or sits
    inside the store. If a tree sits in ``versions/<commit>`` and neither
    base establishes a manager layout, return ``None``. The layout describes
    where a tree sits. :func:`read_store_install` reports what placed it there.
    """
    from .pdk import STORE_DIRS  # lazy: pdk imports this module

    real = os.path.realpath(variant_dir)
    for base in (*STORE_DIRS, root):
        parts = _parts_under(os.path.realpath(os.path.expanduser(base)), real)
        if parts and len(parts) == 5 and parts[2] == "versions":
            return "manager_family"
        if parts and len(parts) == 4 and parts[1] == "versions":
            return "legacy_manager"
    versions_dir = os.path.dirname(os.path.dirname(real))
    return None if os.path.basename(versions_dir) == "versions" else "flat"


def store_evidence_lines(report: Mapping[str, Any]) -> list[str]:
    """``klt pdk find`` text for a report's ``store_layout`` and ``install``
    (issue #8007): the layout line, then the ``install`` block -- or one line
    saying the placing tool is unknown when the marker gave no evidence.
    A pure formatter; the caller prints."""
    install = report.get("install")
    lines = [f"store_layout: {report.get('store_layout') or '-'}"]
    if install is None:
        return [
            *lines,
            "install: none (no usable .pdk-store.json: placing tool unknown)",
        ]
    return [
        *lines,
        "install:",
        *(f"  {field}: {install.get(field) or '-'}" for field in INSTALL_FIELDS),
    ]


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

#: An open_pdks ``SOURCES`` file leads with the revision that built the
#: install: ``open_pdks <token>`` (a git commit id for a from-source build).
_SOURCES_REVISION_RE = re.compile(r"^open_pdks[ \t]+(\S{4,64})[ \t]*$", re.MULTILINE)


def sources_revision_token(stamp_text: Any) -> str | None:
    """The ``open_pdks <token>`` revision token of a ``SOURCES`` stamp text,
    or ``None`` when no parseable line is present (issue #5593). Worker and
    client stamps are then nameable by the same token a caller asserts as
    ``options.pdk_revision``; a non-``str`` input is simply unparsable."""
    if not isinstance(stamp_text, str):
        return None
    found = _SOURCES_REVISION_RE.search(stamp_text)
    return found.group(1) if found else None


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
    from .pdk import PdkNotFoundError, find_pdk, variant_dir_of

    try:
        info = find_pdk(variant=variant)
    except (PdkNotFoundError, OSError, ValueError):
        return None
    return variant_dir_of(info)


def client_pdk_stamp(variant: Any, root: Any = None) -> dict[str, Any] | None:
    """The submitter's stamp for ``variant`` (``SOURCES``, else the bundle
    ``.pdk-store.json``): under ``root`` when one is pinned, otherwise from
    the install the PDK search order resolves (issue #5474).

    A pinned root resolves layout-aware through
    :func:`klayout_tools.pdk.variant_dir_of`: a nested store's variant is
    ``root/<variant>``, a flat single-PDK install (issue #522) *is* the root
    itself. A ``root/<variant>`` that is a plain directory but not a variant
    tree (a bundle symlink, issue #5318) still stamps through the join, as
    before. ``None`` when ``variant`` is not a non-empty string, no install
    holds it, or the resolved install has neither stamp source.
    """
    if not (isinstance(variant, str) and variant):
        return None
    if isinstance(root, str) and root:
        # Lazy for the same circular-import reason as ``resolved_variant_dir``.
        from .pdk import variant_dir_of

        primary: str | None = variant_dir_of({"root": root, "variant": variant})
        nested = os.path.join(root, variant)
        candidates = [primary, nested] if nested != primary else [primary]
    else:
        candidates = [resolved_variant_dir(variant)]
    for variant_dir in candidates:
        if variant_dir is None:
            continue
        stamp = sources_pdk_stamp(variant, variant_dir) or store_pdk_stamp(
            variant, variant_dir
        )
        if stamp is not None:
            return stamp
    return None


#: The batch worker's twin of :func:`read_store_install` and
#: :func:`store_layout_of`, as the source it runs: that program is stdlib-only
#: text and cannot import this package. It names the capture program's own
#: ``clip``/``STR`` and resolves ``@STORES@`` through
#: :func:`worker_store_evidence_source`. Every reading rule here has a
#: counterpart above; change them together.
_WORKER_STORE_EVIDENCE_SOURCE = r"""
def store_version(vdir):
    try:
        parent = os.path.dirname(os.path.realpath(vdir))
        marker = os.path.join(parent, ".pdk-store.json")
        if not os.path.isfile(marker):
            return None
        d = json.loads(read(marker, 65536))
    except (OSError, ValueError, RecursionError):
        return None
    v = d.get("version") if isinstance(d, dict) else None
    return v.strip() if isinstance(v, str) and v.strip() else None
def read_store_object(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as h:
            raw = h.read(65537)
        if len(raw) > 65536:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None
def is_variant_tree(directory):
    return (
        has_descriptor(directory)
        or os.path.isdir(os.path.join(directory, "libs.tech"))
        or all(os.path.isdir(os.path.join(directory, *p)) for p in @ASAP7_MARKERS@)
    )
def evidence_variant_dir(root, variant):
    if not os.path.isdir(root):
        return None
    with os.scandir(root) as entries:
        nested = {entry.name: entry.path for entry in entries
                  if is_variant_tree(entry.path)}
    aliases = @ALIASES@
    names = [variant] + [name for name, family in aliases.items() if family == variant]
    if nested:
        candidates = names if variant is not None else sorted(nested)
        for name in candidates:
            if name in nested:
                directory = nested[name]
                if has_descriptor(directory):
                    load_descriptor(directory)
                return directory
    elif is_variant_tree(root):
        declared = load_descriptor(root)["variant"] if has_descriptor(root) else None
        if variant is None or (declared or os.path.basename(root) or root) in names:
            return root
    return None
def evidence_tree(root, variant, pinned_root):
    if not (variant or pinned_root):
        return None
    candidates = ([root] if root else []) + @SEARCH_ROOTS@
    if pinned_root is not None:
        candidates = [pinned_root]
    for candidate in candidates:
        directory = os.path.abspath(os.path.expanduser(candidate))
        vdir = evidence_variant_dir(directory, variant or os.environ.get("PDK"))
        if vdir is not None:
            return directory, vdir
    return None
def store_install(vdir):
    real, d = os.path.realpath(vdir), None
    markers = (os.path.join(vdir, ".pdk-store.json"),
               os.path.join(os.path.dirname(real), ".pdk-store.json"))
    for cand in dict.fromkeys(markers):
        try:
            os.lstat(cand)
        except FileNotFoundError:
            continue
        except OSError:
            break
        d = read_store_object(cand)
        break
    if d is None:
        return None
    def text(field):
        value = d.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        return clip(value.strip(), STR)
    sha = d.get("sha256")
    digest = None
    if isinstance(sha, str) and len(sha) == 64 and all(
        c in "0123456789abcdefABCDEF" for c in sha
    ):
        digest = sha
    facts = {"sha256": digest, "version": text("version"), "via": text("via"),
             "synced": text("synced")}
    return facts if any(facts.values()) else None
def under(base, real):
    try:
        rel = os.path.relpath(real, os.path.realpath(os.path.expanduser(base)))
    except ValueError:
        return None
    if rel == ".." or rel.startswith("../"):
        return None
    return [] if rel == "." else rel.split(os.sep)
def store_layout(vdir, root):
    real = os.path.realpath(vdir)
    for base in @STORES@ + [root]:
        parts = under(base, real)
        family = parts and len(parts) == 5 and parts[2] == "versions"
        legacy = parts and len(parts) == 4 and parts[1] == "versions"
        if family or legacy:
            return "manager_family" if family else "legacy_manager"
    versioned = os.path.basename(os.path.dirname(os.path.dirname(real))) == "versions"
    return None if versioned else "flat"
"""


def worker_store_evidence_source() -> str:
    """:data:`_WORKER_STORE_EVIDENCE_SOURCE` ready to splice into the batch
    worker capture program, with :data:`klayout_tools.pdk.STORE_DIRS` embedded
    so the worker measures a tree from the same conventional stores."""
    from .pdk import _ASAP7_MARKERS, CONVENTIONAL_PREFIXES, SHARED_ROOTS, STORE_DIRS
    from .pdk_families import PDK_VARIANT_FAMILY_ALIASES

    return (
        _WORKER_STORE_EVIDENCE_SOURCE.replace("@STORES@", repr(list(STORE_DIRS)))
        .replace("@ASAP7_MARKERS@", repr(_ASAP7_MARKERS))
        .replace(
            "@SEARCH_ROOTS@", repr([*STORE_DIRS, *SHARED_ROOTS, *CONVENTIONAL_PREFIXES])
        )
        .replace("@ALIASES@", repr(PDK_VARIANT_FAMILY_ALIASES))
    )
