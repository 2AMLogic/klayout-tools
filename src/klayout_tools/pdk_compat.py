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
  from checkable provenance -- nothing is invented for sky130/gf180/IHP.
* Warning-only: this module never raises and never changes exit codes.
"""

from __future__ import annotations

import os
from typing import Any

#: Identity namespace: a lambdapdk *wrapper release* (the tag
#: ``scripts/fetch-pdks.sh`` fetches). This is **not** an upstream ASAP7
#: revision and must never be presented as one.
NS_LAMBDAPDK_RELEASE = "lambdapdk-release"

#: Identity namespace: open_pdks ``SOURCES`` stamp (the ``version`` field).
NS_OPEN_PDKS_SOURCES = "open_pdks-sources"

#: File the lambdapdk fetch writes at the archive root, holding the release.
LAMBDAPDK_MARKER = ".fetched-version"

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
]


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
        current = os.path.abspath(variant_dir)
        for _ in range(_MARKER_SEARCH_DEPTH + 1):
            marker = os.path.join(current, LAMBDAPDK_MARKER)
            if os.path.isfile(marker):
                try:
                    with open(marker, encoding="utf-8") as handle:
                        value = handle.read().strip()
                except OSError:
                    return None, None
                if not value:
                    return None, None
                return (
                    {"namespace": namespace, "value": value},
                    LAMBDAPDK_MARKER,
                )
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return None, None
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
    return "match" if expected["value"] == installed["value"] else "mismatch"


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
