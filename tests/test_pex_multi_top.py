"""Unit tests for ``klt pex``'s multi-top mode (issue #5494).

``klt pex --top`` is repeatable: with two or more values each named cell is
extracted on its own and the results are concatenated into the one ``-o``
netlist, so one graded run can extract every drawn cell a link testbench
instantiates. See ``docs/cli/pex.md``'s "Several drawn tops in one run".

Three modules, mirroring the three source modules:

- **this one** -- the pure helpers (``--top`` normalisation and duplicate
  rejection, concatenating the per-top extractions, reducing them into one
  combined report, the per-top ``.SUBCKT`` requirement), plus the two-top
  layout/DUT/testbench fixtures the other two import from here.
- ``test_pex_multi_top_runs.py`` -- ``run_pex``-level behaviour: the flag
  refusals and the per-top pre-flight diagnostics, with ``run_sim``
  monkeypatched so they run everywhere.
- ``test_pex_multi_top_e2e.py`` -- the envelope, and real end-to-end runs
  against a real ngspice (which is what settles that the *concatenated*
  netlist simulates).
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.pex import (
    BODY_BIAS_BIASED,
    BODY_BIAS_UNBIASED,
    PexError,
    _body_bias_report,
    _subckt_interfaces,
)
from klayout_tools.pex_multi_top import (
    _concatenate_extractions,
    _first_firing,
    _require_subckt_per_top,
)
from klayout_tools.pex_multi_top_flags import (
    MULTI_TOP_UNSUPPORTED_CODE,
    _normalize_tops,
)
from klayout_tools.pex_multi_top_report import (
    _combine_extract_reports,
    _combine_parasitics,
)
from test_pex import (
    _RES_BAR,
    _RES_MARKED,
    _write_gds,
    _write_request,
)


def _make_two_top_resistor_layout(
    first_taps: tuple[str, ...] = (),
    second_taps: tuple[str, ...] = (),
) -> kdb.Layout:
    """One stream with **two** top cells, each the same drawn poly resistor
    `_make_sky130_poly_resistor_layout` builds, with its own labelled heads:
    `RESA` (`RA`/`RB`) and `RESB` (`RC`/`RD`).

    This is the shape issue #5494 reports from a real link bench -- a
    testbench instantiating two separately drawn cells that are separate
    tops in the layout, so no single `--top` below the whole array covers
    both. Each cell is its own top, so their nets are independent even though
    the drawn geometry is identical and overlapping in coordinates.

    `first_taps`/`second_taps` add extra labelled li1 islands to that cell
    only, exactly as `_make_sky130_poly_resistor_layout`'s own
    `extra_tap_labels` does: each extracts to its own net and therefore its
    own promoted top-cell pin the hand-written schematic DUT does not
    declare. Per cell, so a pin-count mismatch can be aimed at the *second*
    top specifically -- see `_PROMOTED_TAP_LABELS` for why the count is two.
    """
    layout = kdb.Layout()

    def add(top_name: str, pin_a: str, pin_b: str, taps: tuple[str, ...]) -> None:
        top = layout.create_cell(top_name)

        def draw(layer, datatype, box):
            top.shapes(layout.layer(layer, datatype)).insert(box)

        def label(layer, datatype, text, x, y):
            top.shapes(layout.layer(layer, datatype)).insert(
                kdb.Text(text, kdb.Trans(x, y))
            )

        draw(66, 20, _RES_BAR)
        draw(66, 13, _RES_MARKED)
        for x, name in ((1500, pin_a), (10500, pin_b)):
            draw(66, 44, kdb.Box(x - 100, 400, x + 100, 600))
            draw(67, 20, kdb.Box(x - 400, 200, x + 400, 800))
            label(67, 5, name, x, 500)
        for index, name in enumerate(taps):
            x = 1500 + index * 3000
            draw(67, 20, kdb.Box(x - 400, 3000, x + 400, 3600))
            label(67, 5, name, x, 3300)

    add("RESA", "RA", "RB", first_taps)
    add("RESB", "RC", "RD", second_taps)
    return layout


def _write_two_top_schematic_dut(
    path: Path, second_pins: str = "RC RD", r_ohm: float = 289.2
) -> Path:
    """A schematic DUT declaring a `.SUBCKT` for *both* tops -- what a
    multi-top run requires (issue #5494). `second_pins` lets a test give
    `RESB` an interface that disagrees with its extraction."""
    path.write_text(
        f".SUBCKT RESA RA RB\nR1 RA RB {r_ohm}\n.ENDS RESA\n"
        f".SUBCKT RESB {second_pins}\nR2 RC RD {r_ohm}\n.ENDS RESB\n"
    )
    return path


def _write_two_top_testbench(
    path: Path, dut_path: Path, second_nodes: str = "RB RD"
) -> Path:
    """A testbench instantiating both drawn cells in series off one supply --
    the link-bench shape, with a load after each. One `.include`, as the
    exactly-one-`.include` rule (#1030, #2871) requires."""
    path.write_text(
        f'.include "{dut_path}"\n'
        ".model res_generic_po r\n"
        "Vdd RA 0 DC 1.8\n"
        "Xa RA RB RESA\n"
        "Rl1 RB 0 1000\n"
        f"Xb {second_nodes} RESB\n"
        "Rl2 RD 0 1000\n"
    )
    return path


@pytest.fixture
def two_top_layout(tmp_path):
    return _write_gds(_make_two_top_resistor_layout(), tmp_path / "two_tops.gds")


def _two_top_request(tmp_path, *, second_pins="RC RD", second_nodes="RB RD"):
    """The `(request_path)` for a two-top bench measuring `v(RD)`."""
    dut = _write_two_top_schematic_dut(
        tmp_path / "schematic_dut.spice", second_pins=second_pins
    )
    tb = _write_two_top_testbench(
        tmp_path / "testbench.spice", dut, second_nodes=second_nodes
    )
    return _write_request(tmp_path / "request.json", tb, node="RD")


# --- `--top` normalization and duplicate rejection ------------------------- #


def test_normalize_tops_none_is_the_single_top_default():
    """`None` -- "let the stream's one top cell decide" -- normalises to the
    empty list, which is the `top=None` call `run_extract` always got."""
    assert _normalize_tops(None) == []


def test_normalize_tops_accepts_a_bare_string():
    """Every library caller predating the flag's repeatability passes a
    string; it must keep working, as a one-entry list."""
    assert _normalize_tops("RES") == ["RES"]


def test_normalize_tops_preserves_given_order():
    assert _normalize_tops(["RESB", "RESA"]) == ["RESB", "RESA"]


def test_normalize_tops_rejects_a_repeated_top():
    with pytest.raises(PexError) as excinfo:
        _normalize_tops(["RESA", "RESB", "RESA"])
    assert "more than once" in str(excinfo.value)
    assert excinfo.value.code == MULTI_TOP_UNSUPPORTED_CODE


def test_normalize_tops_rejects_a_repeated_top_case_insensitively():
    """ngspice is case-insensitive about subcircuit names, so `--top RES
    --top res` is the same collision -- two identical `.SUBCKT` blocks in
    one combined netlist, and doubled device/net counts."""
    with pytest.raises(PexError, match="more than once"):
        _normalize_tops(["RES", "res"])


# --- concatenating the per-top extractions --------------------------------- #


def test_concatenate_extractions_joins_subckts_in_top_order(tmp_path):
    first = tmp_path / "a.spice"
    first.write_text("* extracted A\n.SUBCKT RESA RA RB\nR1 RA RB 1\n.ENDS RESA\n")
    second = tmp_path / "b.spice"
    second.write_text("* extracted B\n.SUBCKT RESB RC RD\nR2 RC RD 2\n.ENDS RESB\n")
    combined = tmp_path / "combined.spice"

    _concatenate_extractions([str(first), str(second)], ["RESA", "RESB"], str(combined))

    text = combined.read_text()
    assert text.index(".SUBCKT RESA") < text.index(".SUBCKT RESB")
    assert list(_subckt_interfaces(text)) == ["resa", "resb"]
    # The first line is a klt pex title comment naming the mode -- SPICE
    # reads line 1 as the deck title whatever it holds, and `klt extract`'s
    # own output already opens with a comment.
    assert text.splitlines()[0].startswith("* combined extraction written by klt pex")
    assert "RESA, RESB" in text.splitlines()[0]


def test_concatenate_extractions_comments_out_a_repeated_global_card(tmp_path):
    """`klt extract --parasitics` emits one `.GLOBAL` per extraction for the
    substrate identities it synthesized (issue #1503), so two extractions of
    the same PDK repeat the identical card. The first is kept; later
    identical ones are commented out rather than deleted, so the combined
    file says what was dropped."""
    first = tmp_path / "a.spice"
    first.write_text("* A\n.GLOBAL vsubs\n.SUBCKT RESA RA RB\n.ENDS RESA\n")
    second = tmp_path / "b.spice"
    second.write_text("* B\n.GLOBAL vsubs\n.SUBCKT RESB RC RD\n.ENDS RESB\n")
    combined = tmp_path / "combined.spice"

    _concatenate_extractions([str(first), str(second)], ["RESA", "RESB"], str(combined))

    lines = combined.read_text().splitlines()
    assert [line for line in lines if line.strip().upper().startswith(".GLOBAL")] == [
        ".GLOBAL vsubs"
    ]
    assert any("duplicate card already declared above" in line for line in lines)
    assert any(".GLOBAL vsubs" in line and line.startswith("*") for line in lines)


def test_concatenate_extractions_keeps_a_global_declaring_other_nets(tmp_path):
    """A later card declaring a *different* net set is new information, not a
    repeat, so it is kept."""
    first = tmp_path / "a.spice"
    first.write_text("* A\n.GLOBAL vsubs\n.SUBCKT RESA RA RB\n.ENDS RESA\n")
    second = tmp_path / "b.spice"
    second.write_text("* B\n.GLOBAL vnw\n.SUBCKT RESB RC RD\n.ENDS RESB\n")
    combined = tmp_path / "combined.spice"

    _concatenate_extractions([str(first), str(second)], ["RESA", "RESB"], str(combined))

    text = combined.read_text()
    assert ".GLOBAL vsubs" in text.splitlines()
    assert ".GLOBAL vnw" in text.splitlines()


def test_concatenate_extractions_comments_out_a_dropped_cards_continuation(tmp_path):
    """A `+` line belongs to the card above it: dropping a duplicate
    `.GLOBAL` without its continuation would leave an orphan continuation
    line the engine reads as a card of its own."""
    first = tmp_path / "a.spice"
    first.write_text("* A\n.GLOBAL vsubs\n+ vnw\n.SUBCKT RESA RA RB\n.ENDS RESA\n")
    second = tmp_path / "b.spice"
    second.write_text("* B\n.GLOBAL vsubs\n+ vnw\n.SUBCKT RESB RC RD\n.ENDS RESB\n")
    combined = tmp_path / "combined.spice"

    _concatenate_extractions([str(first), str(second)], ["RESA", "RESB"], str(combined))

    lines = combined.read_text().splitlines()
    assert [line for line in lines if line.startswith("+")] == ["+ vnw"]


def test_concatenate_extractions_unreadable_input_names_the_top(tmp_path):
    with pytest.raises(PexError, match="--top RESB"):
        _concatenate_extractions(
            [str(tmp_path / "missing_a.spice"), str(tmp_path / "missing_b.spice")],
            ["RESB", "RESC"],
            str(tmp_path / "combined.spice"),
        )


# --- the combined report: never cleaner than the worst top ----------------- #


def _extract_report_stub(top, *, devices, nets, unbiased=(), integrity=None, nets_=()):
    report = {
        "top": top,
        "netlist_path": f"/stage/{top}.spice",
        "netlist_sha256": f"sha256:{top}",
        "device_count": devices,
        "net_count": nets,
        "provenance": {"deck": {"name": "sky130"}, "top": top},
        "unbiased_pmos_body_nets": [
            {"device": f"M_{top}", "net": net} for net in unbiased
        ],
        "parasitics": {"model": "lumped", "nets": list(nets_)},
    }
    if integrity is not None:
        report["parasitics"]["integrity"] = integrity
    return report


def test_combine_extract_reports_sums_device_and_net_counts():
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2),
            _extract_report_stub("RESB", devices=3, nets=5),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    assert combined["device_count"] == 4
    assert combined["net_count"] == 7
    # The combined file is what the extracted leg simulated, so it is what
    # the report's `netlist`/`netlist_sha256` must identify.
    assert combined["netlist_path"] == "/out/combined.spice"
    assert combined["netlist_sha256"] == "sha256:combined"


def test_combine_extract_reports_is_unbiased_when_any_top_is():
    """The worst-top rule for `body_bias` (issue #5494): an unbiased body in
    the *second* top must not be invisible in a report whose `netlist` names
    the file that top's devices are in. `klt signoff` quotes `body_bias`
    verbatim into an item-7 citation, so losing the disclosure there is the
    failure this rule exists to prevent."""
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2),
            _extract_report_stub("RESB", devices=1, nets=2, unbiased=["$3", "$4"]),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    report = _body_bias_report(combined)
    assert report["status"] == BODY_BIAS_UNBIASED
    assert report["unbiased_device_count"] == 2
    assert report["unbiased_nets"] == ["$3", "$4"]


def test_combine_extract_reports_keeps_the_clean_verdict_when_every_top_is_biased():
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2),
            _extract_report_stub("RESB", devices=1, nets=2),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    assert _body_bias_report(combined)["status"] == BODY_BIAS_BIASED


def test_combine_extract_reports_takes_provenance_from_the_first_top():
    """`provenance` and the flag echoes are this run's own inputs -- the same
    layout stream, deck, PDK and flags for every top -- so the first top's
    are the run's. Per-top detail lives in `tops[]`."""
    combined = _combine_extract_reports(
        [
            _extract_report_stub("RESA", devices=1, nets=2),
            _extract_report_stub("RESB", devices=1, nets=2),
        ],
        netlist_path="/out/combined.spice",
        netlist_sha256="sha256:combined",
    )
    assert combined["provenance"]["top"] == "RESA"


def test_combine_parasitics_sums_integrity_counters_and_concatenates_nets():
    """An integrity finding on the second top is summed into the top-level
    `extraction.parasitic_integrity`, never dropped -- the same worst-top
    rule `body_bias` follows. Thresholds are the caller's own flags,
    identical across tops, so the first top's are the run's."""
    base = {
        "short_threshold_ohm": 1.0,
        "cap_tolerance": {"absolute_ff": 0.1, "relative": 0.01},
        "max_detail_items": 20,
        "nets_checked": 0,
        "nets_with_findings": 0,
        "open_nets": 0,
        "island_components": 0,
        "orphan_nodes": 0,
        "suspicious_resistors": 0,
        "short_candidates": 0,
        "capacitance_mismatches": 0,
    }
    merged = _combine_parasitics(
        [
            {
                "integrity": {**base, "nets_checked": 2},
                "nets": [{"net": "RA", "integrity": {"ok": True}}],
            },
            {
                "integrity": {
                    **base,
                    "nets_checked": 3,
                    "nets_with_findings": 1,
                    "open_nets": 1,
                },
                "nets": [{"net": "RC", "integrity": {"ok": False}}],
            },
        ]
    )
    assert merged["integrity"]["nets_checked"] == 5
    assert merged["integrity"]["nets_with_findings"] == 1
    assert merged["integrity"]["open_nets"] == 1
    assert merged["integrity"]["short_threshold_ohm"] == 1.0
    assert [entry["net"] for entry in merged["nets"]] == ["RA", "RC"]


def test_first_firing_returns_the_first_tops_block():
    entries = [
        {"top": "RESA", "pin_count_mismatch": None},
        {"top": "RESB", "pin_count_mismatch": {"subcircuit": "RESB"}},
        {"top": "RESC", "pin_count_mismatch": {"subcircuit": "RESC"}},
    ]
    assert _first_firing(entries, "pin_count_mismatch") == {"subcircuit": "RESB"}


def test_first_firing_is_none_when_no_top_fires():
    assert _first_firing([{"pin_count_mismatch": None}], "pin_count_mismatch") is None


# --- the schematic DUT must declare a `.SUBCKT` per top -------------------- #


def test_require_subckt_per_top_passes_when_every_top_is_declared(tmp_path):
    dut = _write_two_top_schematic_dut(tmp_path / "dut.spice")
    assert _require_subckt_per_top(str(dut), ["RESA", "RESB"]) is None


def test_require_subckt_per_top_is_case_insensitive(tmp_path):
    dut = _write_two_top_schematic_dut(tmp_path / "dut.spice")
    assert _require_subckt_per_top(str(dut), ["resa", "resb"]) is None


def test_require_subckt_per_top_names_the_missing_top(tmp_path):
    dut = tmp_path / "dut.spice"
    dut.write_text(".SUBCKT RESA RA RB\nR1 RA RB 289.2\n.ENDS RESA\n")
    with pytest.raises(PexError) as excinfo:
        _require_subckt_per_top(str(dut), ["RESA", "RESB"])
    message = str(excinfo.value)
    assert "RESB" in message
    # It also says what the DUT *does* declare, so the fix is obvious.
    assert "it declares RESA" in message
    assert excinfo.value.code == MULTI_TOP_UNSUPPORTED_CODE


def test_require_subckt_per_top_on_a_flat_dut_names_every_top(tmp_path):
    """A flat schematic DUT (no `.SUBCKT` at all) fails here rather than via
    `flat_dut_mismatch`: naming which cells are missing is strictly more
    actionable for a multi-top run than "none are present"."""
    dut = tmp_path / "dut.spice"
    dut.write_text("R1 RA RB 289.2\n")
    with pytest.raises(PexError) as excinfo:
        _require_subckt_per_top(str(dut), ["RESA", "RESB"])
    assert "RESA, RESB" in str(excinfo.value)
    assert "(none)" in str(excinfo.value)
