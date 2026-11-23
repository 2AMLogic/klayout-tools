"""Opt-in disclosure of evidence produced outside ``klt`` for the
``--manifest`` tier report (issue #6953), split out of ``signoff.py`` for
its size-ratchet ceiling -- the ``signoff_pex_disclosure`` precedent: pure
functions over manifest/row dicts, importing nothing from the module they
were split out of at module level, re-imported there by name so
``signoff.<name>`` keeps resolving.

The gap (issue #6953): a block whose flow legitimately ran through external
scripts -- OpenROAD STA results, a synthesized gate-level netlist from RTL
pinned in another repository -- has first-class, committed evidence this
grader cannot read: no T1 item accepts a foreign document, and citing one
as ``evidence`` renders ``unreadable_evidence``/``unrecognized_envelope``.
Leaving the item uncited renders ``no_evidence`` -- a reason string
asserting *no evidence exists* -- so a fleet reader concludes the work was
never done and files the wrong next issue.

``disclosed_evidence`` (a manifest field, optional) closes that gap the
honest way: it **identifies** the artifact without claiming it. The
grader's own statement about a disclosed path is only that the path names a
readable local file -- the contents are never parsed, classified, executed,
or interpreted as a grading envelope. The row stays ``unmet`` with
``reason: "evidence_not_klt_native"``, distinct from ``no_evidence``, the
declared path recorded in the row's ``detail.disclosed_evidence``. No T1
credit, no ``citation``, no exit-code change: a disclosure is
identification, never a verdict.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .signoff_base import SignoffError
from .signoff_unmet_reasons import (
    _REASON_EVIDENCE_NOT_KLT_NATIVE,
    _REASON_NO_EVIDENCE,
    _REASON_UNREADABLE_EVIDENCE,
)

#: The item keys a disclosure map accepts: ``"<item id>"`` or
#: ``"<item id>.<analog|digital>"`` -- exactly the ``evidence`` map's key
#: grammar, resolved with the same partition-qualified precedence by
#: :func:`._lookup_evidence` (this module, re-exported by ``signoff``).
_DISCLOSED_KEY = re.compile(r"\d+(?:\.(?:analog|digital))?")


def _lookup_evidence(
    evidence: dict[str, Any], item_id: int, partition_kind: str | None
) -> Any:
    """Look up a manifest ``evidence`` entry for ``item_id``, preferring the
    partition-qualified key (``"<id>.<partition_kind>"``) over the bare
    ``"<id>"`` key -- so a mixed-signal manifest can give per-partition
    evidence for a per-kind item while still sharing one evidence entry
    across both partitions for a kind-independent item.

    ``partition_kind`` (issue #2362) is the partition **being graded** --
    ``"analog"`` or ``"digital"`` for a ``"mixed-signal"`` manifest (which
    grades one partition at a time), and the block's own ``kind`` for a pure
    ``"analog"``/``"digital"`` manifest. It is deliberately *not* the report
    row's ``"partition"`` field, which stays ``None`` outside a mixed-signal
    manifest: issue #2362 -- ``docs/cli/signoff.md`` documents
    ``"<item id>.<analog|digital>"`` as *the* spelling for a per-kind item
    without restricting it to mixed-signal blocks, so a ``"7.digital"`` key
    on a ``kind: "digital"`` manifest has to resolve. Keying the lookup off
    the row's ``partition`` instead silently ignored exactly the citation the
    docs asked for and rendered the item ``unmet``/``no_evidence``.

    Only the partition actually being graded is ever tried, so a mismatched
    qualifier (``"7.analog"`` on a ``"digital"`` manifest) still finds
    nothing and falls through to the bare key -- it is never graded against
    the other kind's evidence.

    Lives here since issue #6953 (re-exported by ``signoff`` so
    ``signoff._lookup_evidence`` keeps resolving): the disclosure map reuses
    exactly this key precedence, and the move keeps ``signoff.py`` at its
    size-ratchet ceiling.
    """
    if partition_kind:
        keyed = evidence.get(f"{item_id}.{partition_kind}")
        if keyed is not None:
            return keyed
    return evidence.get(str(item_id))


def read_disclosed_evidence(manifest: dict[str, Any]) -> dict[str, str]:
    """Validate the manifest's optional ``disclosed_evidence`` map (issue
    #6953), returning it unchanged -- ``{}`` when the manifest declared
    none.

    The declaration is a **manifest-level** field like
    ``partition_boundary``, so a malformed one is an authoring mistake
    (:class:`~klayout_tools.signoff.SignoffError`) rather than a per-item
    grade: there is no per-item grading pathway for a disclosure to degrade
    (unlike ``evidence``, whose malformed entries render that one item
    ``invalid_evidence``), and silently dropping a typo'd declaration would
    leave the manifest looking like it made a disclosure it did not -- the
    exact failure mode the issue reports.

    Refused: a non-object declaration; a key that is not ``"<item id>"`` or
    ``"<item id>.<analog|digital>"``; a value that is not a non-empty
    string path. A well-formed key naming an item id the parsed doc does
    not list is *not* refused -- it is simply never looked up, exactly as
    an ``evidence`` key for an uncited item id behaves.
    """
    raw = manifest.get("disclosed_evidence", {})
    if not isinstance(raw, dict):
        raise SignoffError(
            "block manifest 'disclosed_evidence' must be a JSON object of "
            f"item keys to file paths, got {type(raw).__name__}"
        )
    for key, value in raw.items():
        if not isinstance(key, str) or not _DISCLOSED_KEY.fullmatch(key):
            raise SignoffError(
                "block manifest 'disclosed_evidence' keys must be "
                '"<item id>" or "<item id>.<analog|digital>", '
                f"got {key!r}"
            )
        if not isinstance(value, str) or not value:
            raise SignoffError(
                "block manifest 'disclosed_evidence' values must be "
                f"non-empty file path strings, got {value!r} for key {key!r}"
            )
    return raw


def apply_disclosed_evidence(
    row: dict[str, Any],
    disclosed: dict[str, str],
    *,
    item_id: int,
    partition_kind: str,
) -> dict[str, Any]:
    """Apply one disclosure to a **finished** T1 row (issue #6953): when
    this build graded the item and the row would otherwise read
    ``unmet / no_evidence``, and the disclosure map names a path for it
    (via :func:`._lookup_evidence`, so the
    partition-qualified key takes precedence over the bare one), the row's
    reason becomes ``evidence_not_klt_native`` (the path names a readable
    file) or ``unreadable_evidence`` (it does not: missing, a directory, or
    unreadable), and ``detail.disclosed_evidence`` records the declared
    path. The row is returned (mutated in place when the disclosure
    applies), with ``status`` and ``citation`` untouched -- ``unmet`` and
    ``null``, exactly as before.

    Every other row is returned unchanged: a row this build did not grade
    (``graded_by_build: false``), and a row whose verdict anything else
    already produced -- a native pass, a native citation failure, a
    spec-row verdict, a declared gap, a recorded ``not_applicable``. Native
    evidence keeps precedence by construction: the disclosure can only ever
    *replace* the bare "nothing was cited" reason, never a graded outcome,
    so a valid native envelope at a disclosed path still cannot confer
    credit.
    """
    if not disclosed:
        return row
    if row.get("status") != "unmet" or row.get("reason") != _REASON_NO_EVIDENCE:
        return row
    if row.get("graded_by_build") is not True:
        return row
    path = _lookup_evidence(disclosed, item_id, partition_kind)
    if path is None:
        return row
    row["reason"] = (
        _REASON_EVIDENCE_NOT_KLT_NATIVE
        if _readable_file(path)
        else _REASON_UNREADABLE_EVIDENCE
    )
    row["detail"] = {**row.get("detail", {}), "disclosed_evidence": path}
    return row


def _readable_file(path: str) -> bool:
    """Check that the disclosure resolves to a readable regular file (#6953)."""
    return os.path.isfile(path) and os.access(path, os.R_OK)
