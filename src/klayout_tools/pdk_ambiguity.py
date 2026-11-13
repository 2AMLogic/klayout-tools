"""The PDK-ambiguity warning :mod:`klayout_tools.pdk` hands to its callers.

Split out of :mod:`klayout_tools.pdk` (file-size ratchet, issue #5449) and
re-exported from it, so ``pdk.ambiguity_warning`` / ``pdk.AMBIGUITY_REMEDY``
keep working for every existing caller.

Pure formatter, like the rest of ``pdk``: nothing here prints. A caller emits
the returned string itself -- to stderr for a CLI verb, or into its own
diagnostics channel.
"""

from __future__ import annotations

from typing import Any

#: The remedy sentence :func:`ambiguity_warning` appends by default (issue
#: #2564): pin the root with ``--pdk-root`` or a request's
#: ``models.pdk_root``, which disables the search entirely.
#:
#: A caller whose backend *refuses* both spellings passes its own ``remedy``
#: instead (issue #5449) rather than advising a fix that cannot be applied --
#: ``klt sim --backend batch`` is the one such caller today.
AMBIGUITY_REMEDY = (
    "Resolution is first-match-wins, so a pinned open_pdks build may not be "
    "the one this run read -- pin the root explicitly (--pdk-root, or a "
    "request's models.pdk_root) to disable the search and choose "
    "deliberately."
)


def ambiguity_warning(
    report: dict[str, Any], *, remedy: str | None = None
) -> str | None:
    """Render a one-line warning for a :func:`klayout_tools.pdk.find_pdk`
    report whose ``ambiguous_roots`` is non-empty, or ``None`` when the
    resolution was unambiguous (issue #2564).

    The message names the **absolute** root actually read and every root
    skipped, because that is the only form of the information a caller can
    act on; the JSON reports carry the path-free ``resolved_via`` labels
    instead (see ``docs/json-contract.md``'s path-privacy rule for committed
    evidence).

    ``remedy`` replaces the trailing :data:`AMBIGUITY_REMEDY` sentence; the
    chosen/skipped-root half of the line is identical either way, so a
    backend-specific remedy cannot drift from the hazard it answers.
    """
    skipped = report.get("ambiguous_roots") or []
    if not skipped:
        return None
    others = ", ".join(
        f"{entry['root']} ({entry['resolved_via']})" for entry in skipped
    )
    return (
        f"klt: warning: {len(skipped) + 1} installs provide PDK variant "
        f"{report['variant']!r}; read {report['root']} "
        f"({report['resolved_via']}), skipped {others}. "
        + (AMBIGUITY_REMEDY if remedy is None else remedy)
    )
