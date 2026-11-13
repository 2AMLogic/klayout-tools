"""End-to-end tests for ``klt pex``'s multi-top mode (issue #5494): the
envelope, and real runs against a real ngspice.

These are what settle the question the issue left open -- that the
*concatenated* extracted netlist (two ``.SUBCKT`` blocks, and one ``.GLOBAL``
card per extraction) actually simulates, so the extracted leg of a two-top
bench really grades. They skip with a clear reason on a machine without
ngspice, exactly like ``test_pex.py``'s own integration tier.

The pure helpers and the shared two-top fixtures live in
``test_pex_multi_top.py``; the monkeypatched ``run_pex``-level behaviour lives
in ``test_pex_multi_top_runs.py``.
"""

from __future__ import annotations

import pytest

from klayout_tools._provenance import sha256_file
from klayout_tools.cli import main
from klayout_tools.pex import BODY_BIAS_BIASED, PexError, _subckt_interfaces, run_pex
from test_pex import (
    _SKIP_NO_NGSPICE,
    _make_sky130_poly_resistor_layout,
    _write_gds,
    _write_request,
    _write_schematic_dut,
    _write_testbench,
)
from test_pex_multi_top import (
    _make_two_top_resistor_layout,
    _two_top_request,
)

# --- the envelope: `tops` is additive ------------------------------------- #


@_SKIP_NO_NGSPICE
def test_integration_run_pex_single_top_envelope_carries_tops_null(
    tmp_path, resistor_layout
):
    """A zero/one-`--top` run produces the envelope it always did, plus
    `"tops": null` (issue #5494). The `null` is what makes the change
    additive under `docs/json-contract.md`: no existing field changes type or
    meaning, and a reader that does not know about `tops` is unaffected.

    Checked against a real run (not a stub) and for *both* spellings -- no
    `--top` at all, and one `--top` -- since the library now normalises both
    through the same list.
    """
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    implicit = run_pex(resistor_layout, [str(request)], "sky130")
    assert implicit["tops"] is None
    assert implicit["status"] == "pass"
    assert implicit["extraction"]["device_count"] == 1
    assert implicit["extraction"]["net_count"] == 2

    explicit = run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        top="RES",
        output=str(tmp_path / "explicit.spice"),
        artifacts_dir=str(tmp_path / "explicit-art"),
    )
    assert explicit["tops"] is None
    # Byte-identical extraction summary either way: one `--top` naming the
    # stream's only top cell is the same extraction as naming none.
    assert explicit["extraction"]["device_count"] == 1
    assert explicit["extraction"]["net_count"] == 2
    assert explicit["body_bias"] == implicit["body_bias"]


@_SKIP_NO_NGSPICE
def test_integration_run_pex_multi_top_extracts_both_tops_and_grades(
    tmp_path, two_top_layout
):
    """The headline acceptance criterion (issue #5494), end to end against a
    real ngspice: `--top RESA --top RESB` extracts both tops, writes one `-o`
    netlist defining `.SUBCKT RESA` *and* `.SUBCKT RESB`, re-points the
    testbench's single DUT `.include` at it, and produces `delta[]` rows as a
    single-top run does.

    This is also what settles the concatenation question the issue left
    open: each extraction emits its own `.GLOBAL` card for the substrate
    identity it synthesized (issue #1503), so the combined file is only
    simulable if the repeat is handled -- and the extracted leg here really
    runs, on a real engine, against the real combined file.
    """
    request = _two_top_request(tmp_path)
    combined = tmp_path / "combined.spice"

    report = run_pex(
        two_top_layout,
        [str(request)],
        "sky130",
        top=["RESA", "RESB"],
        output=str(combined),
        artifacts_dir=str(tmp_path / "art"),
    )

    # Both tops are defined in the one netlist the extracted leg included.
    interfaces = _subckt_interfaces(combined.read_text())
    assert list(interfaces) == ["resa", "resb"]
    assert interfaces["resa"][1] == ["RA", "RB"]
    assert interfaces["resb"][1] == ["RC", "RD"]

    assert report["status"] == "pass"
    # The singular fields describe the combined netlist: counts are sums.
    assert report["extraction"]["device_count"] == 2
    assert report["extraction"]["net_count"] == 4
    assert report["extraction"]["netlist_sha256"] == sha256_file(str(combined))

    # `tops[]` carries each top's own detail, in `--top` order.
    assert [entry["top"] for entry in report["tops"]] == ["RESA", "RESB"]
    assert [entry["extraction"]["device_count"] for entry in report["tops"]] == [1, 1]
    assert [entry["extraction"]["net_count"] for entry in report["tops"]] == [2, 2]
    # Each entry is in the shape of the top-level field it mirrors.
    for entry in report["tops"]:
        assert set(entry) == {
            "top",
            "netlist",
            "extraction",
            "body_bias",
            "pin_count_mismatch",
            "flat_dut_mismatch",
            "pin_order_mismatch",
            "provenance",
        }
        assert entry["body_bias"]["status"] == BODY_BIAS_BIASED
        assert entry["pin_count_mismatch"] is None
        assert entry["flat_dut_mismatch"] is None
        assert entry["pin_order_mismatch"] is None
        # A per-top `extraction` block names the per-top staged file, not the
        # combined one.
        assert (
            entry["extraction"]["netlist_sha256"]
            != report["extraction"]["netlist_sha256"]
        )

    # A real, graded comparison: the extracted side carries both cells'
    # interconnect parasitics on top of their drawn 289.2 ohm each, so it
    # reads measurably lower than the ideal schematic divider.
    (row,) = report["delta"]
    assert row["spec_row"] == "vout"
    assert row["status"] == "pass"
    assert row["schematic_value"] is not None
    assert row["extracted_value"] is not None
    assert row["extracted_value"] < row["schematic_value"]
    assert report["coverage"]["nothing_checked"] is False


@_SKIP_NO_NGSPICE
def test_integration_run_pex_multi_top_pins_applies_as_a_union_set(tmp_path):
    """`--pins` passes through to every top as the one per-net membership set
    it already is (issue #5494): the *union* of the tops' pin names keeps each
    top's real pins promoted while demoting each top's own extra labelled
    taps, so neither top's interface mismatches its schematic `.SUBCKT`.

    Both tops here carry two extra labelled taps that extraction would
    otherwise promote (the issue #1030 shape). Without `--pins` that is a
    `pin_count_mismatch`; with the union set the run grades.
    """
    layout = _write_gds(
        _make_two_top_resistor_layout(
            first_taps=("TAPA1", "TAPA2"), second_taps=("TAPB1", "TAPB2")
        ),
        tmp_path / "two_tops_taps.gds",
    )
    request = _two_top_request(tmp_path)
    combined = tmp_path / "combined.spice"

    report = run_pex(
        layout,
        [str(request)],
        "sky130",
        top=["RESA", "RESB"],
        declared_pins=frozenset({"RA", "RB", "RC", "RD"}),
        output=str(combined),
        artifacts_dir=str(tmp_path / "art"),
    )

    interfaces = _subckt_interfaces(combined.read_text())
    assert interfaces["resa"][1] == ["RA", "RB"]
    assert interfaces["resb"][1] == ["RC", "RD"]
    assert report["pin_count_mismatch"] is None
    assert report["status"] == "pass"
    assert len(report["delta"]) == 1


@_SKIP_NO_NGSPICE
def test_integration_run_pex_multi_top_order_reorders_the_output_and_tops(
    tmp_path, two_top_layout
):
    """`--top` order is the order of the combined file and of `tops[]` --
    reversing the flags reverses both, and nothing else about the run."""
    request = _two_top_request(tmp_path)
    combined = tmp_path / "reversed.spice"

    report = run_pex(
        two_top_layout,
        [str(request)],
        "sky130",
        top=["RESB", "RESA"],
        output=str(combined),
        artifacts_dir=str(tmp_path / "art"),
    )

    assert list(_subckt_interfaces(combined.read_text())) == ["resb", "resa"]
    assert [entry["top"] for entry in report["tops"]] == ["RESB", "RESA"]
    # The sums are order-independent, and the leg still grades.
    assert report["extraction"]["device_count"] == 2
    assert report["extraction"]["net_count"] == 4
    assert report["status"] == "pass"


@_SKIP_NO_NGSPICE
def test_integration_run_pex_multi_top_per_top_files_do_not_collide_with_output(
    tmp_path, two_top_layout
):
    """The per-top staged extractions live in their own `tops/` subdirectory
    of the run's `--outdir`, so they cannot collide with each other or with
    the combined `-o` netlist -- including when the caller points `-o`
    straight at `--outdir`."""
    outdir = tmp_path / "art"
    outdir.mkdir()
    combined = outdir / "combined.spice"
    request = _two_top_request(tmp_path)

    report = run_pex(
        two_top_layout,
        [str(request)],
        "sky130",
        top=["RESA", "RESB"],
        output=str(combined),
        artifacts_dir=str(outdir),
    )

    staged = sorted(path.name for path in (outdir / "tops").glob("*.spice"))
    assert staged == ["00-RESA.spice", "01-RESB.spice"]
    assert combined.is_file()
    assert report["status"] == "pass"
    # Each staged file holds exactly its own top; the combined file holds both.
    assert list(
        _subckt_interfaces((outdir / "tops" / "00-RESA.spice").read_text())
    ) == ["resa"]
    assert list(
        _subckt_interfaces((outdir / "tops" / "01-RESB.spice").read_text())
    ) == ["resb"]


def test_run_pex_multi_top_failed_extraction_names_the_top_and_leaves_output_alone(
    tmp_path, two_top_layout, monkeypatch
):
    """A failed *second* extraction names the top that failed and leaves the
    combined `-o` untouched -- the concatenation only runs once every
    extraction has succeeded, which is why `-o` is never half-written."""
    import klayout_tools.pex as pex_module
    from klayout_tools.extract import ExtractError

    real_run_extract = pex_module.run_extract
    calls = {"n": 0}

    def _fail_on_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_run_extract(*args, **kwargs)
        raise ExtractError("deck exploded")

    monkeypatch.setattr(pex_module, "run_extract", _fail_on_second)
    request = _two_top_request(tmp_path)
    combined = tmp_path / "combined.spice"
    combined.write_text("* a previous run's netlist\n")

    with pytest.raises(PexError) as excinfo:
        run_pex(
            two_top_layout,
            [str(request)],
            "sky130",
            top=["RESA", "RESB"],
            output=str(combined),
            artifacts_dir=str(tmp_path / "art"),
        )
    assert "--top RESB" in str(excinfo.value)
    assert "deck exploded" in str(excinfo.value)
    assert combined.read_text() == "* a previous run's netlist\n"


@pytest.fixture
def resistor_layout(tmp_path):
    """The single-top drawn poly resistor (`RES`) -- defined locally for the
    same reason as `two_top_layout`: pytest resolves fixtures by name."""
    return _write_gds(_make_sky130_poly_resistor_layout(), tmp_path / "res.gds")


@pytest.fixture
def two_top_layout(tmp_path):
    """A stream with two drawn top cells, `RESA` and `RESB` (issue #5494).

    Defined per module rather than imported: pytest resolves fixtures by
    name, so importing one trips `F811` on every test that takes it as a
    parameter. The layout builder itself is shared.
    """
    return _write_gds(_make_two_top_resistor_layout(), tmp_path / "two_tops.gds")


@_SKIP_NO_NGSPICE
def test_integration_cli_pex_multi_top_text_output_names_every_top(
    tmp_path, two_top_layout, capsys
):
    """The courtesy text output names every top, so a reader can attribute
    the summed `extraction:` counts without reaching for `--format json`."""
    request = _two_top_request(tmp_path)

    exit_code = main(
        [
            "pex",
            str(two_top_layout),
            str(request),
            "--deck",
            "sky130",
            "--top",
            "RESA",
            "--top",
            "RESB",
            "-o",
            str(tmp_path / "combined.spice"),
            "--outdir",
            str(tmp_path / "art"),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "tops: 2  (RESA, RESB)" in out
    assert "RESA:" in out
    assert "RESB:" in out
    assert "body_bias=biased" in out
