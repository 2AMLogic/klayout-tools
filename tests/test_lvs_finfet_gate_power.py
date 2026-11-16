"""The FinFET gate-level route's derived ``power_connectivity`` verdict
(issue #5757).

Split out of ``test_lvs.py`` to respect the per-file size ratchet: the
derivation lives in :mod:`klayout_tools.lvs_finfet_power`, is wired into
``assemble_lvs_report``, and these tests pin both the derivation rules and
the wiring.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

from klayout_tools.lvs_finfet_power import (
    derive_finfet_gate_level_power,
    supplies_pair_in_net_correspondence,
)

#: The ``unchecked`` stub ``lvs_reference_stage`` writes on the FinFET
#: gate-level route (shape per ``_power_connectivity_unchecked``).
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

#: ``net_correspondence`` rows proving both declared supplies: the layout
#: side carries the merged-alias spelling a real routed grid extracts as
#: (issue #2405), paired to non-empty reference-side nets.
SUPPLY_PAIRS = [
    {"layout": "G_VPWR_M1|MNT_G|VPWR", "reference": "VPWR"},
    {"layout": "VGND", "reference": "VGND"},
    {"layout": "net_a", "reference": "net_a"},
]

_FINFET = dict(
    finfet_layout=True,
    reference_form="gate-level-verilog",
    power_connectivity_enabled=True,
    supply_nets=["VPWR", "VGND"],
)

# Real input files: `assemble_lvs_report` hashes the layout side and reads
# the reference side (``reference_module_names``) on every call.
_TMPDIR = tempfile.mkdtemp(prefix="klt-lvs-finfet-power-")
LAYOUT_PATH = os.path.join(_TMPDIR, "top.gds")
REFERENCE_PATH = os.path.join(_TMPDIR, "top.v")
with open(LAYOUT_PATH, "wb") as _handle:
    _handle.write(b"stub layout")
with open(REFERENCE_PATH, "w", encoding="utf-8") as _handle:
    _handle.write("module TOP(a, y);\n  input a;\n  output y;\nendmodule\n")


def _derive(**overrides):
    kwargs = {**_FINFET, **overrides}
    return derive_finfet_gate_level_power(
        kwargs.pop("power_connectivity", dict(FINFET_STUB)),
        kwargs.pop("status", "match"),
        kwargs.pop("net_correspondence", SUPPLY_PAIRS),
        **kwargs,
    )


def test_derived_match_on_a_matched_compare_with_supplies_paired():
    block = _derive()

    assert block["status"] == "match"
    assert "expanded-cell compare" in block["reason"]
    assert "derived" in block["reason"]
    # The stub's key set is the block's contract -- flipped in place, no
    # new field, and the evidence-free counts stay put.
    assert sorted(block) == sorted(FINFET_STUB)
    assert block["power_pins"] == []
    assert block["instance_count"] == 0
    assert block["findings"] == []
    assert block["finding_count"] == 0


def test_mismatched_compare_stays_unchecked_unchanged():
    stub = dict(FINFET_STUB)
    block = _derive(status="mismatch", power_connectivity=stub)

    assert block is stub
    assert block["status"] == "unchecked"
    assert block["reason"] == FINFET_STUB["reason"]


def test_explicit_false_stays_unchecked():
    stub = dict(FINFET_STUB)
    block = _derive(power_connectivity_enabled=False, power_connectivity=stub)

    assert block is stub
    assert block["status"] == "unchecked"


def test_unpaired_supply_stays_unchecked():
    # `VPWR` never pairs: a reference whose expanded rails float on
    # internal nets must not grade as a verified power grid.
    stub = dict(FINFET_STUB)
    block = _derive(
        net_correspondence=[
            {"layout": "VGND", "reference": "VGND"},
            {"layout": "net_a", "reference": "net_a"},
        ],
        power_connectivity=stub,
    )

    assert block is stub
    assert block["status"] == "unchecked"


def test_supply_paired_to_an_empty_reference_side_does_not_count():
    assert not supplies_pair_in_net_correspondence(
        [{"layout": "VPWR", "reference": ""}], ["VPWR"]
    )


def test_alias_membership_and_case_insensitivity():
    assert supplies_pair_in_net_correspondence(
        [{"layout": "g_vpwr_m1|mnt_g|vpwr", "reference": "VPWR"}], ["vpwr"]
    )
    assert not supplies_pair_in_net_correspondence(
        [{"layout": "g_vpwr_m1|mnt_g", "reference": "VPWR"}], ["VPWR"]
    )


def test_non_finfet_and_other_reference_forms_are_untouched():
    stub = dict(FINFET_STUB)
    block = _derive(finfet_layout=False, power_connectivity=stub)
    assert block is stub

    stub = dict(FINFET_STUB)
    block = _derive(reference_form="plain-element", power_connectivity=stub)
    assert block is stub


def test_already_graded_block_is_never_rederived():
    graded = {**FINFET_STUB, "status": "mismatch", "reason": None}
    block = _derive(power_connectivity=graded)

    assert block is graded


def test_assemble_lvs_report_wires_the_derivation():
    """The report-building call site: a FinFET gate-level run with a matched
    compare and paired supplies ships the derived verdict; the same inputs
    under a mismatched compare ship the stub verbatim."""
    from klayout_tools.lvs_report import assemble_lvs_report

    def _run(status: str) -> dict:
        opts = SimpleNamespace(
            deck_options=None,
            label_layers=None,
            def_net_names=False,
            def_pins_echo=None,
            layout_abstraction=SimpleNamespace(patterns=(), echo={}),
            parameter_tolerance=None,
            combine_devices=False,
            combine_devices_per_circuit=None,
            combine_devices_max_attempts=5,
            flatten_layout=False,
            flatten_reference=True,
            compare_timeout_s=None,
            netgen_setup_echo=None,
            compare_parameters=None,
            power_connectivity_echo=None,
            anchor_top_level_pins_echo=None,
            strict_top_level_pin_count=False,
            compare_mos_flavour=False,
            ignore_mos_bulk=False,
            supply_nets=["VPWR", "VGND"],
            finfet_bulk=None,
            finfet_layout=True,
            power_connectivity_enabled=True,
        )
        return assemble_lvs_report(
            mismatches=[],
            status=status,
            engine="netgen",
            engine_version="1.0",
            netgen_binary="/usr/bin/netgen",
            layout_deck_name=None,
            layout_hash_source=LAYOUT_PATH,
            layout_input_role="layout",
            layout_circuit=SimpleNamespace(name="TOP"),
            layout_abstracted_cells=[],
            reference_circuit=SimpleNamespace(name="TOP"),
            reference_form="gate-level-verilog",
            reference_spec={},
            reference_pdk_info=None,
            reference_pin_order_sources=[],
            reference_pin_order_files=[],
            reference_netlist_path=REFERENCE_PATH,
            extracted_netlist_path=None,
            equivalent_pins_applied=None,
            constant_bindings=[],
            power_connectivity=dict(FINFET_STUB),
            body_verification=None,
            device_parameter_coverage=None,
            counts={},
            device_classes={},
            net_correspondence=list(SUPPLY_PAIRS),
            portable_paths=False,
            request_dir=".",
            opts=opts,
        )

    matched = _run("match")
    assert matched["status"] == "match"
    assert matched["power_connectivity"]["status"] == "match"
    assert "expanded-cell compare" in matched["power_connectivity"]["reason"]

    mismatched = _run("mismatch")
    assert mismatched["power_connectivity"] == FINFET_STUB
