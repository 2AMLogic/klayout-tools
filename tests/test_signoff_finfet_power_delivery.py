"""Item 11 on the FinFET gate-level route: the derived ``power_connectivity``
verdict reaches ``met`` (issue #5757).

Split out of ``test_signoff.py`` to respect the per-file size ratchet.

Before #5757 the FinFET ``gate-level-verilog`` route reported
``power_connectivity.status: "unchecked"`` by construction, so item 11's
digital (PDN) branch -- which requires ``"match"`` -- was unsatisfiable
through the one route ``docs/design-evidence-tiers.md`` steers routed
digital blocks through. These tests pin that the verdict the route now
derives from its expanded-cell compare (produced by
:func:`klayout_tools.lvs_finfet_power.derive_finfet_gate_level_power`, the
same function the report builder calls) is accepted by the grader, while
the pre-#5757 ``unchecked`` stub still is not.
"""

from __future__ import annotations

from klayout_tools.lvs_finfet_power import derive_finfet_gate_level_power
from klayout_tools.signoff import build_tier_report
from test_signoff import (
    _item_11,
    _manifest,
    _power_delivery_evidence,
)

#: The ``unchecked`` stub ``lvs_reference_stage`` writes on the FinFET
#: gate-level route -- the pre-#5757 shape (issue #4462's reason text).
FINFET_STUB = {
    "status": "unchecked",
    "reason": (
        "reference.form is 'gate-level-verilog' on a FinFET layout.deck: "
        "each library cell was expanded to its transistor-level CDL body "
        "with its own power/ground pins, so supply connectivity takes part "
        "in the ordinary compare (a 'match' evidences it) and the "
        "stub-form power-pin check does not apply"
    ),
    "power_pins": [],
    "power_pins_derivation": None,
    "instance_count": 0,
    "expected_nets": None,
    "unchecked_expected_pins": [],
    "excluded_pins": [],
    "findings": [],
    "finding_count": 0,
}

#: What the route derives once the compare matched and the declared
#: supplies paired -- produced here by the derivation itself, so the test
#: pins the producer's real output shape, not a hand-copied block.
DERIVED_MATCH = derive_finfet_gate_level_power(
    dict(FINFET_STUB),
    "match",
    [
        {"layout": "G_VPWR_M1|MNT_G|VPWR", "reference": "VPWR"},
        {"layout": "VGND", "reference": "VGND"},
    ],
    finfet_layout=True,
    reference_form="gate-level-verilog",
    power_connectivity_enabled=True,
    supply_nets=["VPWR", "VGND"],
)


def _grade_item_11(tmp_path, *, power_connectivity: dict) -> dict:
    lvs_envelope = _lvs_envelope(power_connectivity)
    return _item_11(
        build_tier_report(
            _manifest(
                kind="digital",
                evidence={
                    "11": _power_delivery_evidence(
                        tmp_path, kind="digital", lvs_envelope=lvs_envelope
                    )
                },
            )
        )
    )


def _lvs_envelope(power_connectivity: dict) -> dict:
    from test_signoff import LVS_MATCH_ENVELOPE

    return {**LVS_MATCH_ENVELOPE, "power_connectivity": power_connectivity}


def test_item_11_met_through_the_derived_finfet_gate_level_verdict(tmp_path):
    """The route's derived `match` satisfies item 11's digital branch: the
    PDN evidence plus an LVS whose power_connectivity the expanded-cell
    compare proved."""
    item = _grade_item_11(tmp_path, power_connectivity=DERIVED_MATCH)

    assert item["status"] == "met"
    assert item["reason"] is None
    assert DERIVED_MATCH["status"] == "match"
    assert "expanded-cell compare" in DERIVED_MATCH["reason"]


def test_item_11_unmet_while_the_route_still_reports_unchecked(tmp_path):
    """The pre-#5757 stub (`unchecked`) still does not satisfy item 11 --
    the derived verdict, not the route, is what closed the gap."""
    item = _grade_item_11(tmp_path, power_connectivity=dict(FINFET_STUB))

    assert item["status"] == "unmet"
    assert item["reason"] == "lvs_supply_unproven"
