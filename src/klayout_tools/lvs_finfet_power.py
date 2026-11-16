"""Derived power-connectivity verdict for the FinFET gate-level route
(issue #5757).

On ``reference.form: "gate-level-verilog"`` with a FinFET ``layout.deck``
(issue #4462/#4463), every instantiated library cell is expanded to its
transistor-level CDL body with its own power/ground pins, so supply
connectivity takes part in the ordinary compare. The route has always said
so in ``power_connectivity.reason`` -- and then reported ``"unchecked"``
anyway, leaving the evidence the compare itself produced unreported. That
collided with ``docs/design-evidence-tiers.md``: item 11's digital
power-delivery branch requires ``power_connectivity.status == "match"``
(``signoff_power_delivery._pdn_branch_reason``), which was unreachable by
construction on the one route the same document steers routed digital
blocks through.

:func:`derive_finfet_gate_level_power` turns that evidence into the
verdict, **after** the compare -- called from
:func:`klayout_tools.lvs_report.assemble_lvs_report`, the one place the
final compare ``status`` and the ``net_correspondence`` are both in scope.
A ``match`` is derived only when every input that could refute it holds:

* the compare itself matched (never a clean verdict on a failed compare),
* the run did not opt out via ``options.power_connectivity: false`` (the
  caller asked for no power-connectivity evidence at all),
* and every declared supply net (``options.supply_nets``, the resolved
  list the report already echoes) pairs to a reference-side net in
  ``net_correspondence`` -- the same alias-membership rule
  ``signoff_power_delivery._lvs_reference_carries_supplies`` applies on the
  grading side, so the producer and the grader agree on what "the supplies
  took part in the compare" means.

Any input not holding leaves the ``unchecked`` block exactly as
``lvs_reference_stage`` wrote it, byte-identical -- including its reason --
so every non-deriving report (non-FinFET decks, mismatched compares,
disabled runs, unpaired supplies) is unchanged.
"""

from __future__ import annotations

from typing import Any


def supplies_pair_in_net_correspondence(
    net_correspondence: Any, supply_nets: list[str]
) -> bool:
    """Whether every declared supply net pairs in an LVS report's
    ``net_correspondence`` -- the same rule
    :func:`klayout_tools.signoff_power_delivery._lvs_reference_carries_supplies`
    applies to a committed envelope on the grading side (issue #2405).

    A row proves a supply when the row's ``reference`` side is non-empty
    and its ``layout`` alias names the supply: ``klt lvs`` writes a
    label-merged net as ``|``-joined aliases (e.g.
    ``"G_VDDR_M1|MNT_G|VDDR|VDDR1"``), so the declared name qualifies when
    it is *any one* of the row's aliases, not only the full joined string.
    Name comparison is case-insensitive, matching how `klt lvs` itself
    compares power net names. An empty ``supply_nets`` declaration is
    vacuously satisfied -- the same convention the grading-side function
    uses.
    """
    paired: set[str] = set()
    for row in net_correspondence or []:
        if not isinstance(row, dict):
            continue
        layout = row.get("layout")
        reference = row.get("reference")
        if isinstance(layout, str) and isinstance(reference, str) and reference:
            paired.update(alias.upper() for alias in layout.split("|") if alias)
    return all(name.upper() in paired for name in supply_nets)


def derive_finfet_gate_level_power(
    power_connectivity: Any,
    status: Any,
    net_correspondence: Any,
    *,
    finfet_layout: bool,
    reference_form: str,
    power_connectivity_enabled: bool,
    supply_nets: list[str],
) -> dict[str, Any]:
    """The ``power_connectivity`` block a FinFET gate-level compare's own
    evidence supports (issue #5757), or the input block unchanged.

    Returns the input block untouched -- the same object, with its
    ``unchecked`` reason verbatim -- unless **all** of these hold:

    * ``reference_form`` is ``"gate-level-verilog"`` on a FinFET deck
      (``finfet_layout``): this is the route whose expanded-cell compare
      carries supply connectivity, and the only one this derivation means
      anything for;
    * the block is the route's own ``unchecked`` stub (never a block some
      other path already graded);
    * ``power_connectivity_enabled`` is true -- an explicit
      ``options.power_connectivity: false`` asked for no power-connectivity
      evidence, and stays ``unchecked``;
    * the final compare ``status`` is ``"match"`` -- a failed compare
      evidences nothing;
    * every declared supply net (``supply_nets``, the resolved
      ``options.supply_nets``) pairs in ``net_correspondence``
      (:func:`supplies_pair_in_net_correspondence`) -- the guard that keeps
      a trivially-matching reference (its expanded rails floating on
      internal nets, never tied to the declared top-level supplies) from
      grading as a verified power grid.

    The derived block keeps the stub's exact key set (no new field -- the
    block's shape is a report contract), flipping ``status`` to ``"match"``
    and writing a ``reason`` that names the derivation, the paired supplies,
    and both guards that were applied.
    """
    if reference_form != "gate-level-verilog" or not finfet_layout:
        return power_connectivity
    if not power_connectivity_enabled:
        return power_connectivity
    if (power_connectivity or {}).get("status") != "unchecked":
        return power_connectivity
    if status != "match":
        return power_connectivity
    if not supplies_pair_in_net_correspondence(net_correspondence, supply_nets):
        return power_connectivity

    from .lvs import POWER_STATUS_MATCH

    derived = dict(power_connectivity)
    derived["status"] = POWER_STATUS_MATCH
    derived["reason"] = (
        "derived from the expanded-cell compare (issue #5757): the "
        "reference's library cells were expanded to transistor-level CDL "
        "bodies whose power/ground pins take part in the ordinary compare, "
        "which matched, and every declared supply net "
        f"({', '.join(sorted(supply_nets))}) paired in net_correspondence "
        "-- the stub-form pin-to-net check does not apply on this route"
    )
    return derived
