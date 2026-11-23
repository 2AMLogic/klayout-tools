"""Text-mode rendering contract for ``klt extract`` (issue #5481).

``cli/extract_text.py`` is the courtesy surface, and almost every block in it
is *additive* -- printed only when the matching flag was given. The end-to-end
tests in ``tests/test_extract.py`` run real extractions, so they only exercise
the few blocks a plain run emits; the optional ones (``--probe``,
``--device-model``, ``--matched-group``, the parasitics sub-blocks, and the
``engine`` block this issue adds) were rendered but unasserted.

These tests drive the renderer directly with report dictionaries, pinning each
block's exact output without paying for an extraction per flag combination.
The dictionaries carry the field names the report builders emit
(``extract.py``, ``extract_report.py``, ``substrate_resistance.py``).
"""

from __future__ import annotations

from klayout_tools.cli.extract_text import _print_engine, _print_text


def _base_report(**overrides) -> dict:
    """The keys ``_print_text`` reads unconditionally -- a flagless,
    ``--no-parasitics`` run, with every additive block absent, so a test can
    add back exactly the one it pins."""
    report = {
        "file": {"path": "blocks/inv/inv.gds", "scope": "repo"},
        "deck": "sky130",
        "top": "TOP",
        "dbu_um": 0.001,
        "status": "ok",
        "netlist_path": "blocks/inv/inv.spice",
        "netlist_sha256": "a" * 64,
        "netlist_normalized_sha256": "b" * 64,
        "device_count": 2,
        "net_count": 4,
        "pin_count": 3,
        "pdk": None,
        "device_counts": {},
        "warnings": [],
    }
    report.update(overrides)
    return report


def _probe(name: str, layer: str, x_um: float, y_um: float, net: str | None) -> dict:
    """One ``probes[]`` entry as ``extract.py`` builds it."""
    return {
        "name": name,
        "layer": layer,
        "x_um": x_um,
        "y_um": y_um,
        "found": net is not None,
        "net": net,
    }


def _parasitics(**overrides) -> dict:
    """The ``parasitics`` keys the renderer reads unconditionally."""
    block = {
        "r_count": 3,
        "c_count": 5,
        "total_resistance_ohm": 12.5,
        "total_capacitance_ff": 0.8,
        "cc_count": 0,
        "nets": [],
    }
    block.update(overrides)
    return block


def _render(report: dict, capsys) -> list[str]:
    _print_text(report)
    return capsys.readouterr().out.splitlines()


def test_minimal_report_renders_only_the_mandatory_lines(capsys):
    """A flagless run prints the fixed envelope and nothing else: every
    additive block stays silent when its key is absent, which is what lets
    the per-block tests below attribute a line to the block that added it."""
    lines = _render(_base_report(), capsys)

    assert lines == [
        "file: blocks/inv/inv.gds",
        "deck: sky130",
        "top: TOP",
        "dbu_um: 0.001",
        "status: ok",
        "netlist_path: blocks/inv/inv.spice",
        "netlist_sha256: " + "a" * 64,
        "netlist_normalized_sha256: " + "b" * 64,
        "devices: 2  nets: 4  pins: 3",
    ]


def test_no_netlist_run_renders_the_nulls_rather_than_omitting_them(capsys):
    """``--no-netlist`` (issue #2850) leaves the three netlist fields
    ``None``; text mode still prints them, so a reader cannot mistake a
    skipped write for a line that scrolled past."""
    nulls = dict.fromkeys(
        ("netlist_path", "netlist_sha256", "netlist_normalized_sha256")
    )
    lines = _render(_base_report(**nulls), capsys)

    assert [line for line in lines if line.startswith("netlist_")] == [
        "netlist_path: None",
        "netlist_sha256: None",
        "netlist_normalized_sha256: None",
    ]


def test_runset_engine_report_renders_engine_deck_file_and_deck_vars(capsys):
    """The one behaviour change in the #5481 move: a report carrying
    ``engine`` -- which only the ``klayout-runset`` engine emits -- also
    prints its runset and its ``-rd`` globals, before the ``deck`` line."""
    lines = _render(
        _base_report(
            deck="ihp-sg13g2",
            engine="klayout-runset",
            deck_file="/pdk/ihp-sg13g2/libs.tech/klayout/tech/lvs/sg13g2.lvs",
            deck_vars={"lvs_sub": "sub!", "spice_net_names": "true"},
        ),
        capsys,
    )

    assert lines[1] == "engine: klayout-runset"
    assert (
        lines[2] == "deck_file: /pdk/ihp-sg13g2/libs.tech/klayout/tech/lvs/sg13g2.lvs"
    )
    # Sorted, so the line is stable across dict insertion order.
    assert lines[3] == "deck_vars: lvs_sub=sub!, spice_net_names=true"
    assert lines[4] == "deck: ihp-sg13g2"


def test_runset_engine_report_without_deck_vars_omits_that_line(capsys):
    """No ``--deck-var`` was given: the engine and its runset are still
    named, but no empty ``deck_vars:`` line is printed."""
    lines = _render(
        _base_report(
            engine="klayout-runset",
            deck_file="/pdk/sg13g2.lvs",
            deck_vars={},
        ),
        capsys,
    )

    assert "engine: klayout-runset" in lines
    assert "deck_file: /pdk/sg13g2.lvs" in lines
    assert not any(line.startswith("deck_vars:") for line in lines)


def test_curated_deck_report_prints_no_engine_block(capsys):
    """A curated-deck report has no ``engine`` key at all, so its text
    output is unchanged by #5481 -- the helper returns before printing."""
    _print_engine({"deck": "sky130"})

    assert capsys.readouterr().out == ""


def test_deck_options_mark_silently_defaulted_values(capsys):
    """``provenance.deck.options`` is the *resolved* option set (issue
    #2394), so each value the caller did not pass is marked ``(default)``
    and the text surface cannot present one option's answer as *the*
    answer."""
    lines = _render(
        _base_report(
            provenance={
                "deck": {
                    "options": {"metal_stack": "5M1LI", "mim": "on"},
                    "options_explicit": {"metal_stack": True, "mim": False},
                }
            }
        ),
        capsys,
    )

    assert "deck_options: metal_stack=5M1LI, mim=on (default)" in lines


def test_deck_options_tolerate_a_legacy_record_and_a_null_provenance(capsys):
    """No ``options_explicit`` at all predates #2394, when ``options`` held
    caller-passed keys only -- nothing there is marked ``(default)``. A
    present-but-``null`` ``provenance``/``deck`` is why the renderer uses
    ``or {}`` at each hop rather than a ``.get`` default."""
    lines = _render(
        _base_report(provenance={"deck": {"options": {"metal_stack": "5M1LI"}}}),
        capsys,
    )
    assert "deck_options: metal_stack=5M1LI" in lines

    for empty in (None, {"deck": None}):
        lines = _render(_base_report(provenance=empty), capsys)
        assert not any(line.startswith("deck_options:") for line in lines)


def test_label_layers_device_models_and_probes_render_one_line_each(capsys):
    """The three per-flag echoes (issues #2656, #4234, #2986). A probe that
    hit a net names it; one that missed says so rather than printing an
    empty net name."""
    lines = _render(
        _base_report(
            label_layers={"net": [66, 20], "pin": None},
            device_models={"PMOS": "sky130_fd_pr__pfet_01v8", "NMOS": "nfet_01v8"},
            probes=[
                _probe("out", "met1", 1.5, 0.25, "Y"),
                _probe("stray", "met2", 9.0, 9.0, None),
            ],
        ),
        capsys,
    )

    assert "label_layers: net=66/20, pin=none" in lines
    assert "device_models: NMOS=nfet_01v8, PMOS=sky130_fd_pr__pfet_01v8" in lines
    assert "probe out: met1 (1.5, 0.25) -> Y" in lines
    assert "probe stray: met2 (9.0, 9.0) -> not found" in lines


def test_pdk_block_renders_a_dash_for_an_unknown_version(capsys):
    lines = _render(
        _base_report(pdk={"variant": "sky130A", "version": "1.0.0"}), capsys
    )
    assert "pdk: sky130A (1.0.0)" in lines

    lines = _render(_base_report(pdk={"variant": "sky130A", "version": None}), capsys)
    assert "pdk: sky130A (-)" in lines


def test_device_counts_and_ignored_layer_tables_render_per_entry(capsys):
    lines = _render(
        _base_report(
            device_counts={"PMOS": 1, "NMOS": 1},
            ignored_layers=[{"layer": 68, "datatype": 20, "shapes": 4}],
            device_recognition_only_layers=[{"layer": 65, "datatype": 44, "shapes": 2}],
            warnings=["two devices share a label"],
        ),
        capsys,
    )

    assert "device_counts:" in lines
    assert "  NMOS: 1" in lines
    assert "  PMOS: 1" in lines
    assert "  68/20: 4 shape(s)" in lines
    assert "  65/44: 2 shape(s)" in lines
    assert "warnings:" in lines
    assert "  two devices share a label" in lines


def test_matched_device_groups_render_status_mismatches_and_unresolved(capsys):
    """``--matched-group``: a group whose compared fields all agree is
    ``[OK]``; one that disagrees is ``[MISMATCH]`` and lists the differing
    values per instance, then any instance that could not be resolved to a
    device at all."""
    lines = _render(
        _base_report(
            matched_device_groups=[
                {
                    "name": "pair",
                    "instances": ["M1", "M2"],
                    "mismatched_fields": [],
                    "unresolved_instances": [],
                },
                {
                    "name": "mirror",
                    "instances": ["M3", "M4"],
                    "mismatched_fields": [
                        {"field": "w", "values": {"M3": 1.0, "M4": 2.0}}
                    ],
                    "unresolved_instances": ["M5"],
                },
            ]
        ),
        capsys,
    )

    assert "matched_device_groups (--matched-group):" in lines
    assert "  pair: M1, M2 [OK]" in lines
    assert "  mirror: M3, M4 [MISMATCH]" in lines
    assert "    w: M3=1.0, M4=2.0" in lines
    assert "    unresolved: M5" in lines


def test_parasitics_block_renders_only_the_sub_blocks_that_are_present(capsys):
    """The parasitics sub-blocks are each additive, and the common case
    (a small isolated cell, no scoping flags) prints the summary line
    alone rather than a row of zeros."""
    lines = _render(
        _base_report(parasitics=_parasitics()),
        capsys,
    )

    assert "parasitics: R=3 C=5  total_R=12.5 ohm  total_C=0.8 fF" in lines
    assert not any(line.startswith("parasitics_coupling:") for line in lines)
    assert not any(line.startswith("critical_nets:") for line in lines)
    assert not any(line.startswith("parasitics_top_cell_only:") for line in lines)


def test_parasitics_sub_blocks_each_render_when_their_flag_was_given(capsys):
    """One report carrying every scoped-parasitics block at once: coupling
    (#760), ``--critical-net``, ``--parasitics-net``, ``--parasitic-variant``,
    ``--distributed-rc``, ``--parasitics-top-cell-only``, ``--mom-net``,
    ``--substrate-spreading`` and ``--mom-rlc-net``. The two counted blocks
    count only the nets that actually carry the attribute."""
    lines = _render(
        _base_report(
            spef_path="blocks/inv/inv.spef",
            parasitics=_parasitics(
                cc_count=2,
                total_coupling_capacitance_ff=0.03,
                critical_nets=["Y", "A"],
                parasitics_nets=["Y"],
                parasitic_variant=["cworst", "cbest"],
                distributed_rc=True,
                top_cell_only=True,
                nets=[
                    {"rc_model": "distributed", "capacitance_ff_top_cell": 0.4},
                    {"rc_model": "lumped", "capacitance_ff_top_cell": None},
                ],
                mom_crosscheck={
                    "net": "Y",
                    "lumped_rc_capacitance_ff": 0.5,
                    "mom_capacitance_ff": 0.55,
                    "delta_ff": 0.05,
                    "delta_pct": 10.0,
                },
                substrate_spreading={
                    "net": "VSUBS",
                    "tap_count": 3,
                    "pair_count": 3,
                    "min_resistance_ohm": 120.0,
                    "max_resistance_ohm": 240.0,
                    "resistivity_ohm_cm": 10.0,
                },
                mom_rlc_override={
                    "net": "Y",
                    "resistance_ohm": 1.5,
                    "capacitance_ff": 0.6,
                    "inductance_nh": 0.02,
                },
            ),
        ),
        capsys,
    )

    assert "parasitics_coupling: CC=2  total_CC=0.03 fF" in lines
    assert "critical_nets: Y, A" in lines
    assert "parasitics_nets: Y" in lines
    assert "parasitic_variant: cworst, cbest" in lines
    # Only the one net whose `rc_model` is actually `distributed`.
    assert "distributed_rc: 1 net(s)" in lines
    # Only the one net whose top-cell split is not `None`.
    assert "parasitics_top_cell_only: 1 net(s)" in lines
    assert (
        "mom_crosscheck: net=Y  lumped_rc=0.5 fF  mom=0.55 fF  delta=0.05 fF (10.0%)"
        in lines
    )
    assert (
        "substrate_spreading: net=VSUBS  taps=3  pairs=3  R=120.0..240.0 ohm  "
        "(rho=10.0 ohm-cm, two-terminal estimate, not a network solve)" in lines
    )
    assert "mom_rlc_override: net=Y  r_ohm=1.5  c_ff=0.6  l_nh=0.02" in lines
    assert "spef_path: blocks/inv/inv.spef" in lines


def test_resistance_paths_render_the_series_value_or_the_refusal_reason(capsys):
    """``--resistance-path`` (issue #2765): a solved query prints its series
    resistance and the node path it went through, an unsolved one prints why
    instead of a misleading number."""
    lines = _render(
        _base_report(
            parasitics=_parasitics(
                resistance_paths=[
                    {
                        "net_id": 7,
                        "from": "A",
                        "to": "Y",
                        "status": "ok",
                        "resistance_ohm": 4.0,
                        "path_nodes": ["n1", "n2", "n3"],
                    },
                    {
                        "net_id": 8,
                        "from": "A",
                        "to": "Z",
                        "status": "disconnected",
                        "reason": "no path between the endpoints",
                    },
                ],
            ),
        ),
        capsys,
    )

    assert (
        "resistance_path: net_id=7 A -> Y  status=ok  series_R=4.0 ohm  "
        "via n1 -> n2 -> n3" in lines
    )
    assert (
        "resistance_path: net_id=8 A -> Z  status=disconnected  "
        "(no path between the endpoints)" in lines
    )


def test_subcircuit_block_renders_pins_and_excluded_parasitics(capsys):
    """``--subcircuit`` (issue #2245). The excluded-parasitics line is itself
    conditional: a cell whose parent deck attributed nothing to it prints the
    pins and instance line alone."""
    subcircuit = {
        "cell": "inv",
        "path": "TOP/X1",
        "device_count": 2,
        "net_count": 4,
        "pins": [{"name": "A", "role": "input"}, {"name": "Y", "role": "output"}],
        "instance_line": "X1 A Y VPWR VGND inv",
        "excluded_parasitics": {
            "r_count": 2,
            "c_count": 1,
            "l_count": 0,
            "capacitance_ff": 0.12,
            "resistance_ohm": 3.4,
        },
    }
    lines = _render(_base_report(subcircuit=subcircuit), capsys)

    assert "subcircuit (inv):" in lines
    assert "  path: TOP/X1" in lines
    assert "  devices: 2  nets: 4  pins: 2" in lines
    assert "    A [input]" in lines
    assert "    Y [output]" in lines
    assert "  instance_line: X1 A Y VPWR VGND inv" in lines
    assert (
        "  excluded_parasitics (attributed to the parent deck): 2 R  1 C  0 L  "
        "0.12 fF  3.4 ohm" in lines
    )

    subcircuit["excluded_parasitics"] = dict.fromkeys(
        subcircuit["excluded_parasitics"], 0
    )
    lines = _render(_base_report(subcircuit=subcircuit), capsys)

    assert "subcircuit (inv):" in lines
    assert not any(line.lstrip().startswith("excluded_parasitics") for line in lines)


def test_hierarchy_block_renders_one_line_per_cell(capsys):
    """``--hierarchical-cells`` (issue #2722)."""
    lines = _render(
        _base_report(
            hierarchy={
                "cells": [
                    {
                        "cell": "inv",
                        "placements": 2,
                        "device_count": 2,
                        "pins": ["A", "Y"],
                    }
                ],
                "top_device_count": 4,
                "top_instance_count": 2,
            }
        ),
        capsys,
    )

    assert "hierarchy: 1 cell(s)  top devices: 4  top instances: 2" in lines
    assert "  inv: 2 placement(s)  devices: 2  pins: A Y" in lines


def test_def_pin_promotion_renders_only_error_severity_findings(capsys):
    """``--def-pins`` (issue #2473): the common case -- every declared DEF
    ``PINS`` name promoted, or missed for an ordinary reason -- has an empty
    ``findings`` list and prints nothing at all."""
    lines = _render(
        _base_report(def_pin_promotion={"declared": 3, "promoted": 3, "findings": []}),
        capsys,
    )

    assert not any(line.startswith("def_pin_promotion") for line in lines)

    lines = _render(
        _base_report(
            def_pin_promotion={
                "declared": 3,
                "promoted": 2,
                "findings": [
                    {"severity": "error", "layer": 71, "datatype": 20, "pins": ["VPWR"]}
                ],
            }
        ),
        capsys,
    )

    assert (
        "def_pin_promotion (--def-pins): 2 of 3 declared DEF PINS name(s) promoted"
        in lines
    )
    assert "  [error] 71/20 (outside this deck's connectivity graph): VPWR" in lines
