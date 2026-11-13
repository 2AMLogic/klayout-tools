"""The ``klt signoff --attest --check`` drift result, and naming the
``attestation.checks[]`` entry each drift entry is about (issue #5460).

``klt signoff --attest ITEM --inventory INV --check COMMITTED`` diffs the
freshly re-attested envelope against the committed one through the shared
``--rerun`` machinery, so each ``drift[]`` entry's ``field`` is a dotted
*positional* pointer: ``attestation.checks.17.content_hash``. That pointer is
contract surface shared with ``klt drc``/``extract``/``lvs`` and is not
rewritten here. But on its own it identifies nothing a reader can act on --
and the index is not even stable, because file-presence checks are sorted by
path, so inventorying one new file renumbers every entry after it. Acting on
the report means opening the committed envelope and counting to entry 17.

This module supplies the additive ``drift[].check`` object that names the
check instead, which matters most where the report is the only artifact a
reader has: a CI job log, or a reviewer on a PR they did not write.

Split out of ``signoff_attest`` for the per-file size ratchet
(``scripts/check_file_size_baseline.py``), the same reason
``signoff_attest_text`` and ``signoff_attest_item9`` are separate modules.
It deliberately imports nothing from ``signoff_attest`` -- it reads only the
envelopes and check dicts it is handed -- so both that module and
``signoff_attest_stream`` can use it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ._report_verify import VOLATILE_PROVENANCE_PATHS, build_rerun_result

#: Volatile envelope paths ``--check`` excludes from drift: the producing
#: build's version strings. A `klt` upgrade that attests byte-identical
#: inputs with identical outcomes is not evidence drift.
VOLATILE_ATTESTATION_PATHS = VOLATILE_PROVENANCE_PATHS

#: Keys beyond ``role`` that identify one ``attestation.checks[]`` entry, in
#: the order a ``drift[].check`` object lists them: ``path`` for a ``present``
#: file check, ``target``/``name``/``testbench`` for a ``command`` or
#: ``recorded_hash`` check, ``row`` for an item-8 citation check, and
#: ``corner`` + ``metric`` for an item-8 metric check (there is one check per
#: corner x metric, so the corner alone would not name it). A check kind
#: carrying none of them -- item 9's ``pdk``/``tools`` pins, a top-level
#: ``cold_start`` covering every bench -- is identified by its ``role`` alone.
CHECK_IDENTITY_KEYS = (
    "path",
    "target",
    "name",
    "testbench",
    "row",
    "corner",
    "metric",
)

#: A ``drift[].field`` pointer at (or into) one ``attestation.checks[]``
#: entry, capturing its index. Deliberately not anchored at the end: the
#: drifted leaf may sit further down the check
#: (``attestation.checks.17.content_hash``,
#: ``attestation.checks.3.mismatches.0.actual``). A whole-list
#: ``attestation.checks`` entry -- the length-mismatch branch of
#: ``diff_verdict_fields`` -- names no single check and does not match.
_CHECK_DRIFT_FIELD = re.compile(r"attestation\.checks\.(\d+)(?:\.|\Z)")


def check_identity(check: Mapping[str, Any]) -> dict[str, Any]:
    """The identity of one ``attestation.checks[]`` entry: its ``role``
    followed by whichever of :data:`CHECK_IDENTITY_KEYS` the check carries,
    in that order."""
    identity: dict[str, Any] = {"role": check["role"]}
    identity.update((key, check[key]) for key in CHECK_IDENTITY_KEYS if key in check)
    return identity


def annotate_check_drift(
    drift: list[dict[str, Any]], checks: Sequence[Mapping[str, Any]]
) -> None:
    """Attach an additive ``check`` identity object, in place, to every
    ``drift`` entry whose ``field`` names one entry of ``checks``.

    ``field``, ``committed`` and ``fresh`` are left exactly as
    ``diff_verdict_fields`` produced them -- the dotted positional pointer is
    shared contract surface -- so this only *adds* the
    ``role``/``path``/``target``/... that name the drifted check.

    The identity is taken from the **fresh** checks (what ``checks`` is). A
    list is only diffed index-by-index when both sides have the same length,
    so the index is in range on both; and where the committed and fresh
    checks at one index disagree about their identity, that disagreement is
    itself a ``drift`` entry carrying both values, so nothing is lost by
    taking one side.

    Entries that name no single check are left without the key: a whole-list
    ``attestation.checks`` entry (the length-mismatch branch, which is what
    an inventory that gained or lost a file still produces), ``status``, and
    anything outside ``attestation.checks``.
    """
    for entry in drift:
        matched = _CHECK_DRIFT_FIELD.match(entry["field"])
        if matched is None:
            continue
        index = int(matched.group(1))
        if index < len(checks) and "role" in checks[index]:
            entry["check"] = check_identity(checks[index])


def drift_result(
    committed_path: str, committed: dict[str, Any], fresh: dict[str, Any]
) -> dict[str, Any]:
    """The ``--attest --check`` response: ``fresh`` diffed against the
    committed envelope, with ``skipped_checks`` and ``fresh_status`` beside
    it and every check-scoped ``drift`` entry annotated by
    :func:`annotate_check_drift`."""
    # ``skipped`` marks a local_only check this run did not execute; its
    # (carried-through) outcome is the committed one, so it is reported beside
    # the diff rather than as a difference from the committed envelope.
    skipped = []
    for check in fresh["attestation"]["checks"]:
        if check.pop("skipped", False):
            skipped.append(
                {
                    key: check[key]
                    for key in ("role", "name", "target", "argv", "cwd", "passed")
                    if key in check
                }
            )
    result = build_rerun_result(
        report_path=committed_path,
        committed=committed,
        fresh=fresh,
        exclude=VOLATILE_ATTESTATION_PATHS,
        mode="check",
    )
    annotate_check_drift(result["drift"], fresh["attestation"]["checks"])
    fresh_payload = result.pop("fresh")
    result["fresh_status"] = fresh_payload["status"]
    result["skipped_checks"] = skipped
    result["fresh"] = fresh_payload
    return result
