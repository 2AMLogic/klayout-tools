"""PDK/technology-package compatibility metadata (issue #2759).

A klt technology package or deck may have been built against one specific
PDK revision. This module holds the small declarative record of those
expectations and the pure comparison logic; :func:`klayout_tools.pdk.find_pdk`
and :func:`klayout_tools.pdk.list_pdks` embed the result as an additive
``compatibility`` object.

Rules (see ``docs/cli/pdk.md`` -> "Package compatibility"):

* Only **like identities** are compared. An identity is a
  ``{"namespace", "value"}`` pair; two identities in different namespaces
  never match (the result is ``unknown``), e.g. an open_pdks ``SOURCES``
  stamp is never compared with a lambdapdk release tag.
* A missing or unreadable installed identity is ``unknown``, never ``match``.
* A package with no verified pin is ``not_declared``. Pins are declared only
  from checkable provenance -- nothing is invented for IHP.
* Warning-only: this module never raises and never changes exit codes.
"""

from __future__ import annotations

import os
import re
from typing import Any

#: Identity namespace: a lambdapdk *wrapper release* (the tag
#: ``scripts/fetch-pdks.sh`` fetches). This is **not** an upstream ASAP7
#: revision and must never be presented as one.
NS_LAMBDAPDK_RELEASE = "lambdapdk-release"

#: Identity namespace: open_pdks ``SOURCES`` stamp (the ``version`` field).
NS_OPEN_PDKS_SOURCES = "open_pdks-sources"

#: The open_pdks commit the fleet pins for sky130 and gf180mcu (issue #3427,
#: epic #3420). The authoritative copy is ``pdks.families.*.open_pdks_commit``
#: in ``docker/eda-sim/pdk-versions.json``; that manifest is not shipped in the
#: wheel, so the value is mirrored here and
#: ``tests/test_pdk_compat_open_pdks.py`` fails if the two ever diverge.
OPEN_PDKS_PIN = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"
_MANIFEST_PIN_SOURCE = (
    "docker/eda-sim/pdk-versions.json pdks.families.{}.open_pdks_commit"
)

#: File the lambdapdk fetch writes at the archive root, holding the release.
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
LAMBDAPDK_MARKER = ".fetched-version"
FETCHED_COMMIT_MARKER = ".fetched-commit"

#: How many directory levels above a variant directory to look for
#: :data:`LAMBDAPDK_MARKER` (lambdapdk nests ``<root>/lambdapdk/<process>``).
_MARKER_SEARCH_DEPTH = 2

#: Verified package pins. ``layout`` + ``variant`` select the install the
#: record applies to. ``source`` names where the pin is checkable from.
COMPATIBILITY_RECORDS: list[dict[str, Any]] = [
    {
        "package": "lambdapdk-asap7",
        "layout": "asap7",
        "variant": "asap7",
        "namespace": NS_LAMBDAPDK_RELEASE,
        "expected": "0.2.17",
        "source": "scripts/fetch-pdks.sh LAMBDAPDK_VERSION",
    },
    # A ciel-installed f6eeac7 tree's SOURCES is the single line
    # ``open_pdks <full 40-hex commit>`` (verified against a real
    # ~/.ciel/{sky130A,gf180mcuD} install), so that is the stamp form pinned.
    {
        "package": "open_pdks-sky130",
        "layout": "open_pdks",
        "variant": "sky130A",
        "namespace": NS_OPEN_PDKS_SOURCES,
        "expected": f"open_pdks {OPEN_PDKS_PIN}",
        "source": _MANIFEST_PIN_SOURCE.format("sky130"),
    },
    {
        "package": "open_pdks-gf180mcu",
        "layout": "open_pdks",
        "variant": "gf180mcuD",
        "namespace": NS_OPEN_PDKS_SOURCES,
        "expected": f"open_pdks {OPEN_PDKS_PIN}",
        "source": _MANIFEST_PIN_SOURCE.format("gf180mcu"),
    },
]

#: Shortest hash prefix accepted as the same open_pdks commit (git's default
#: abbreviation floor).
_MIN_SHORT_HASH = 7
_OPEN_PDKS_STAMP = re.compile(r"\bopen_pdks\s+([0-9a-fA-F]+)\b")


def _open_pdks_hash(stamp: str) -> str | None:
    """The commit hash of the ``open_pdks <hash>`` segment of a (possibly
    multi-line, ``"; "``-joined) ``SOURCES`` stamp, lowercased, or ``None``."""
    found = _OPEN_PDKS_STAMP.search(stamp)
    return found.group(1).lower() if found else None


def _open_pdks_equal(expected: str, installed: str) -> bool:
    """Same open_pdks commit; a short hash (>= 7 hex) matches by prefix."""
    want, have = _open_pdks_hash(expected), _open_pdks_hash(installed)
    if want is None or have is None:
        return expected == installed
    short, long_ = sorted((want, have), key=len)
    return len(short) >= _MIN_SHORT_HASH and long_.startswith(short)


def read_fetched_marker(variant_dir: str) -> str | None:
    """Return the stripped contents of the nearest :data:`LAMBDAPDK_MARKER`
    at ``variant_dir`` or up to :data:`_MARKER_SEARCH_DEPTH` parents, or
    ``None`` when absent/unreadable/empty. The nearest marker wins: an
    unreadable or empty one is not skipped in favour of a farther one."""
    current = os.path.abspath(variant_dir)
    for _ in range(_MARKER_SEARCH_DEPTH + 1):
        marker = os.path.join(current, LAMBDAPDK_MARKER)
        if os.path.isfile(marker):
            try:
                with open(marker, encoding="utf-8") as handle:
                    value = handle.read().strip()
            except OSError:
                return None
            return value or None
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def read_fetched_commit(variant_dir: str) -> str | None:
    """Return the upstream commit recorded by ``scripts/fetch-*.sh`` in the
    nearest :data:`FETCHED_COMMIT_MARKER` (same search as
    :func:`read_fetched_marker`), or ``None`` when absent, unreadable, or not
    a 40-hex id (issue #3786). Never guessed."""
    current = os.path.abspath(variant_dir)
    for _ in range(_MARKER_SEARCH_DEPTH + 1):
        marker = os.path.join(current, FETCHED_COMMIT_MARKER)
        if os.path.isfile(marker):
            try:
                with open(marker, encoding="utf-8") as handle:
                    value = handle.read().strip()
            except (OSError, UnicodeDecodeError):
                return None
            return value if _FULL_COMMIT.fullmatch(value) else None
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def read_installed_identity(
    variant_dir: str, namespace: str, sources_version: str | None = None
) -> tuple[dict[str, str] | None, str | None]:
    """Return ``(identity, source_label)`` for ``variant_dir`` in
    ``namespace``, or ``(None, None)`` when it cannot be read.

    ``source_label`` is a path-free description of where the identity came
    from (the JSON must not embed absolute paths -- see
    ``docs/json-contract.md``).
    """
    if namespace == NS_LAMBDAPDK_RELEASE:
        value = read_fetched_marker(variant_dir)
        if value is None:
            return None, None
        return (
            {"namespace": namespace, "value": value},
            LAMBDAPDK_MARKER,
        )
    if namespace == NS_OPEN_PDKS_SOURCES and sources_version:
        return (
            {"namespace": namespace, "value": sources_version},
            "SOURCES",
        )
    return None, None


def compare_identities(
    expected: dict[str, str] | None, installed: dict[str, str] | None
) -> str:
    """Compare two identities; return ``match``, ``mismatch`` or ``unknown``.

    ``unknown`` when either side is missing or the namespaces differ.
    """
    if not expected or not installed:
        return "unknown"
    if expected.get("namespace") != installed.get("namespace"):
        return "unknown"
    if not expected.get("value") or not installed.get("value"):
        return "unknown"
    if expected["namespace"] == NS_OPEN_PDKS_SOURCES:
        same = _open_pdks_equal(expected["value"], installed["value"])
    else:
        same = expected["value"] == installed["value"]
    return "match" if same else "mismatch"


def check_compatibility(
    variant: str,
    variant_dir: str,
    layout: str,
    sources_version: str | None = None,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compatibility result for one resolved variant.

    Shape::

        {"status": "match"|"mismatch"|"unknown"|"not_declared",
         "package": str | None, "variant": str,
         "expected": {"namespace", "value"} | None,
         "installed": {"namespace", "value"} | None,
         "source": str | None,            # where the pin is declared
         "installed_source": str | None}  # where the installed identity came from
    """
    for record in COMPATIBILITY_RECORDS if records is None else records:
        if record["layout"] != layout or record["variant"] != variant:
            continue
        expected = {"namespace": record["namespace"], "value": record["expected"]}
        installed, installed_source = read_installed_identity(
            variant_dir, record["namespace"], sources_version
        )
        return {
            "status": compare_identities(expected, installed),
            "package": record["package"],
            "variant": variant,
            "expected": expected,
            "installed": installed,
            "source": record["source"],
            "installed_source": installed_source,
        }
    return {
        "status": "not_declared",
        "package": None,
        "variant": variant,
        "expected": None,
        "installed": None,
        "source": None,
        "installed_source": None,
    }


def compatibility_warning(result: dict[str, Any]) -> str | None:
    """One-line warning for a ``mismatch``/``unknown`` result, else ``None``."""
    status = result.get("status")
    if status not in ("mismatch", "unknown"):
        return None
    expected = result["expected"] or {}
    installed = result["installed"]
    head = (
        f"klt: warning: PDK variant {result['variant']!r} vs package "
        f"{result['package']!r}: "
    )
    if status == "mismatch":
        return (
            head + f"installed {installed['namespace']} {installed['value']!r} != "
            f"expected {expected['value']!r} (pinned in {result['source']})"
        )
    return (
        head + f"installed {expected.get('namespace')} identity could not be "
        f"determined (expected {expected.get('value')!r}, pinned in "
        f"{result['source']}); compatibility is unverified"
    )


def check_expected_version(
    expected: str, variant_dir: str, sources_version: str | None = None
) -> dict[str, Any]:
    """Compare a caller-supplied pin (``klt pdk find --expect-version``)
    against the installed identity of ``variant_dir``.

    Shape: ``{"status": "match"|"mismatch"|"unknown", "expected": str,
    "installed": str | None}``. The installed identity is the open_pdks
    ``SOURCES`` stamp if present, else the lambdapdk ``.fetched-version``
    marker. A bare hex SHA (>= 7 chars) or an ``open_pdks <sha>`` stamp is an
    open_pdks identity; any other string is compared exactly in the installed
    tree's own namespace. Differing namespaces and an unreadable installed
    identity are ``unknown`` -- callers gating on this must fail closed.
    """
    installed: dict[str, str] | None = None
    for namespace in (NS_OPEN_PDKS_SOURCES, NS_LAMBDAPDK_RELEASE):
        installed, _ = read_installed_identity(variant_dir, namespace, sources_version)
        if installed:
            break
    wanted = expected.strip()
    if re.fullmatch(rf"[0-9a-fA-F]{{{_MIN_SHORT_HASH},}}", wanted):
        identity = {"namespace": NS_OPEN_PDKS_SOURCES, "value": f"open_pdks {wanted}"}
    elif _OPEN_PDKS_STAMP.search(wanted):
        identity = {"namespace": NS_OPEN_PDKS_SOURCES, "value": wanted}
    else:
        identity = {
            "namespace": installed["namespace"] if installed else "",
            "value": wanted,
        }
    return {
        "status": compare_identities(identity, installed),
        "expected": expected,
        "installed": installed["value"] if installed else None,
    }
