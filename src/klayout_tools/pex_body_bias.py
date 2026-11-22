"""``klt pex``'s ``body_bias`` block (issue #1983), split out of
:mod:`klayout_tools.pex` for its size-ratchet ceiling (issue #6559) -- the
``signoff_yield_disclosure`` precedent: a pure function over an extraction
report, re-imported into ``pex`` by name so ``pex.<name>`` keeps resolving.

``klt pex --engine klayout-runset`` (issue #6559) adds one verdict to the
vocabulary below, ``"not_assessed"``: the runset engine never runs the curated
deck's floating-PMOS-body check, so reporting ``"biased"`` for its extraction
would state a clean result nothing measured.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .extract_runset_flags import RUNSET_ENGINE

#: Issue #1983: ``body_bias.status`` values -- whether the extracted netlist
#: this run re-simulated has a DC bias path for every device body.
#:
#: - ``"biased"``: every PMOS body terminal resolved to a real, named net, so
#:   both legs' operating points are comparable.
#: - ``"unbiased"``: at least one PMOS body landed on an anonymous,
#:   KLayout-synthesized net with **no DC bias path at all** (``klt
#:   extract``'s own ``unbiased_pmos_body_nets[]``, issue #555). Per
#:   ``docs/cli/extract.md`` -> "Coverage", that makes a full-circuit
#:   resimulation of the extracted netlist "physically wrong, not merely
#:   imprecise" -- the run converges and produces numbers, and those numbers
#:   are not comparable to the schematic leg's.
#:
#: Deliberately a *separate* verdict from this command's top-level
#: ``status``, which stays exactly what it has always been (every graded
#: ``delta[]`` row met its tolerance). See :func:`_body_bias_report`.
BODY_BIAS_BIASED = "biased"
BODY_BIAS_UNBIASED = "unbiased"
#: Issue #4245: every reported-unbiased net was merged into a schematic net by
#: a caller's ``--tie-net`` declaration. Deliberately not ``"biased"``: that
#: means the *layout* drew the bias path.
BODY_BIAS_TIED = "tied"
#: ``body_bias.tied[].source`` for a ``--tie-net`` tie.
TIE_SOURCE_DECLARED = "declared"

#: Issue #6559: the extraction came from ``--engine klayout-runset``, which
#: reports devices and connectivity only -- it never runs the curated deck's
#: floating-PMOS-body check (``unbiased_pmos_body_nets[]``, issue #555), so
#: neither ``"biased"`` nor ``"unbiased"`` is a statement anything measured.
BODY_BIAS_NOT_ASSESSED = "not_assessed"


def _body_bias_report(
    extract_report: Mapping[str, Any],
    ties: Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """The ``body_bias`` block (issue #1983): whether the extracted netlist
    this run re-simulated actually has a DC bias path for every device body.

    Built from the extraction this command drove itself -- ``klt extract``
    already detects the condition and reports it as
    ``unbiased_pmos_body_nets[]`` (issue #555), so nothing here re-derives
    it; this reduces that array to a verdict plus its counts and carries the
    entries through into `klt pex`'s own envelope.

    **Why this belongs in the `klt pex` report and not only in the
    extraction's.** ``docs/cli/extract.md`` states that a PMOS body on an
    anonymous, synthesized net has no DC bias path at all, which makes a
    full-circuit resimulation of that netlist *physically wrong, not merely
    imprecise*. `klt pex`'s entire output is such a resimulation, compared
    row-by-row against a schematic leg -- so this condition does not merely
    reduce the precision of a ``delta[]`` row, it invalidates the comparison
    the row reports. A `klt pex` record is also the artifact
    ``docs/design-evidence-tiers.md`` item 7 is cited from, and that record
    never carried the extraction's own JSON: a reader of the evidence had no
    way to tell a post-layout number measured on a properly-biased netlist
    from one measured on a floating-body netlist. That is the gap issue
    #1983 reported.

    **Reported, not enforced.** ``status`` is untouched: a run whose deltas
    all met tolerance still reports ``status: "pass"`` with
    ``body_bias.status: "unbiased"``. Whether an unbiased body should
    invalidate the verdict is a policy question for the consumer of the
    evidence (see ``docs/cli/signoff.md`` and
    ``docs/design-evidence-tiers.md`` item 7), not one this command answers
    on the caller's behalf -- and answering it here would retroactively fail
    every design on a PDK whose deck has no well-tap mechanism, which is a
    separate decision from making the condition visible.

    ``unbiased_pmos_body_nets`` is carried verbatim (same field name, same
    ``{"device", "net"}`` entry shape as ``klt extract``'s own) rather than
    re-spelled, so a reader who knows one artifact already knows the other.
    """
    if extract_report.get("engine") == RUNSET_ENGINE:
        # Issue #6559: nothing measured this -- `null` counts, not zeros.
        return {
            "status": BODY_BIAS_NOT_ASSESSED,
            "unbiased_device_count": None,
            "unbiased_nets": None,
            "unbiased_pmos_body_nets": None,
        }
    entries = list(extract_report.get("unbiased_pmos_body_nets") or [])
    unbiased_nets = sorted({entry["net"] for entry in entries})
    report: dict[str, Any] = {
        "status": BODY_BIAS_UNBIASED if entries else BODY_BIAS_BIASED,
        "unbiased_device_count": len(entries),
        "unbiased_nets": unbiased_nets,
        "unbiased_pmos_body_nets": entries,
    }
    if ties:
        # Issue #4245: additive. `unbiased_*` above keep reporting what the
        # layout drew; the caller's declared ties are stated beside them, so
        # "tied by declaration" never reads as a drawn tap.
        report["tied"] = [
            {
                "extracted_net": extracted,
                "schematic_net": schematic,
                "source": TIE_SOURCE_DECLARED,
            }
            for extracted, schematic in ties
        ]
        if {extracted for extracted, _ in ties} >= set(unbiased_nets):
            report["status"] = BODY_BIAS_TIED
    return report
