"""Tests for `klt pex` and the `klayout_tools.pex` library.

Two tiers, mirroring `tests/test_sim.py`/`tests/test_extract.py`:

- **Unit tests** (the majority) exercise the `.include`/`.inc` DUT-swap
  mechanics, the delta-row computation (status/`delta_pct`), and every error
  path that is cheap to trigger (bad testbench, no `.include` directive,
  testbenches disagreeing on their schematic DUT) without ever invoking
  `ngspice` -- these always run, everywhere.
- **Integration tests** (`@_SKIP_NO_NGSPICE`) run a real, self-contained
  end-to-end `klt pex`: a synthetic sky130 poly-resistor layout (the same
  fixture shape `tests/test_extract.py`'s drawn-resistor tests use, so no
  real PDK data is vendored here -- see that file's `_make_poly_resistor_
  layout`), extracted with `--parasitics`, re-simulated against a
  hand-written schematic DUT via a thin `.include`-based testbench. CI
  installs `ngspice` via the package manager (`.github/workflows/ci.yml`)
  so these always run there; they skip with a clear reason on a dev machine
  without it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.pex import (
    PexError,
    _build_delta_rows,
    _delta_pct,
    _find_dut_include,
    _rewrite_dut_include,
    _row_status,
    run_pex,
)

HAVE_NGSPICE = shutil.which("ngspice") is not None
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)


# --------------------------------------------------------------------------- #
# `.include`/`.inc` DUT-swap mechanics
# --------------------------------------------------------------------------- #


def test_find_dut_include_quoted():
    body = '.include "dut.spice"\nVdd RA 0 DC 1.8\n'
    index, path = _find_dut_include(body, "/work")
    assert index == 0
    assert path == "/work/dut.spice"


def test_find_dut_include_unquoted_and_case_insensitive():
    body = ".INCLUDE dut.spice\nVdd RA 0 DC 1.8\n"
    index, path = _find_dut_include(body, "/work")
    assert index == 0
    assert path == "/work/dut.spice"


def test_find_dut_include_inc_spelling():
    body = ".inc 'dut.spice'\nVdd RA 0 DC 1.8\n"
    index, path = _find_dut_include(body, "/work")
    assert index == 0
    assert path == "/work/dut.spice"


def test_find_dut_include_not_first_line():
    body = "* a comment\n.param vdd=1.8\n.include dut.spice\nVdd RA 0 DC {vdd}\n"
    index, path = _find_dut_include(body, "/work")
    assert index == 2
    assert path == "/work/dut.spice"


def test_find_dut_include_absolute_path_unchanged():
    body = '.include "/abs/dut.spice"\n'
    _index, path = _find_dut_include(body, "/work")
    assert path == "/abs/dut.spice"


def test_find_dut_include_missing_raises():
    body = "R1 RA RB 289.2\nVdd RA 0 DC 1.8\n"
    with pytest.raises(PexError, match=r"\.include"):
        _find_dut_include(body, "/work")


def test_rewrite_dut_include_replaces_only_that_line():
    body = '.include "dut.spice"\n.model res_generic_po r\nVdd RA 0 DC 1.8\n'
    index, _path = _find_dut_include(body, "/work")
    rewritten = _rewrite_dut_include(body, index, "/extracted/top.spice")
    assert rewritten == (
        '.include "/extracted/top.spice"\n.model res_generic_po r\nVdd RA 0 DC 1.8\n'
    )


def test_rewrite_dut_include_preserves_no_trailing_newline():
    body = '.model res_generic_po r\n.include "dut.spice"'
    index, _path = _find_dut_include(body, "/work")
    rewritten = _rewrite_dut_include(body, index, "/extracted/top.spice")
    assert rewritten == '.model res_generic_po r\n.include "/extracted/top.spice"'


# --------------------------------------------------------------------------- #
# `_delta_pct` / `_row_status`
# --------------------------------------------------------------------------- #


def test_delta_pct_basic():
    assert _delta_pct(100.0, 95.0) == pytest.approx(-5.0)
    assert _delta_pct(50.0, 55.0) == pytest.approx(10.0)


def test_delta_pct_none_when_schematic_zero():
    assert _delta_pct(0.0, 1.0) is None


def test_delta_pct_none_when_either_value_missing():
    assert _delta_pct(None, 1.0) is None
    assert _delta_pct(1.0, None) is None


def _measurement(value, status="pass"):
    return {
        "name": "vout",
        "value": value,
        "unit": "V",
        "status": status,
        "margin": 0.0,
    }


def test_row_status_pass_when_both_pass():
    assert _row_status(_measurement(1.0), _measurement(1.1)) == "pass"


def test_row_status_pass_follows_extracted_side_only():
    # Schematic side "fails" its own limit, extracted side passes -- the row
    # still grades on the extracted side (the side item 7 actually checks).
    assert _row_status(_measurement(1.0, "fail"), _measurement(1.1, "pass")) == "pass"


def test_row_status_fail_when_extracted_fails():
    assert _row_status(_measurement(1.0), _measurement(1.1, "fail")) == "fail"


def test_row_status_error_when_extracted_errors():
    assert _row_status(_measurement(1.0), _measurement(None, "error")) == "error"


def test_row_status_error_when_schematic_errors():
    assert _row_status(_measurement(None, "error"), _measurement(1.1)) == "error"


def test_row_status_error_when_schematic_missing():
    assert _row_status(None, _measurement(1.1)) == "error"


# --------------------------------------------------------------------------- #
# `_build_delta_rows`
# --------------------------------------------------------------------------- #


def _sim_report(corners):
    return {"corners": corners}


def test_build_delta_rows_single_testbench_bare_spec_row():
    schematic = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    extracted = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.1)]}]
    )
    rows = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert rows == [
        {
            "spec_row": "vout",
            "corner_id": "tt/1.800V/27C",
            "schematic_value": 1.0,
            "extracted_value": 1.1,
            "delta_pct": 10.0,
            "status": "pass",
        }
    ]


def test_build_delta_rows_multi_testbench_prefixes_spec_row():
    schematic = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    extracted = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.1)]}]
    )
    rows = _build_delta_rows(
        spec_row_prefix="gain-tb",
        schematic_report=schematic,
        extracted_report=extracted,
    )
    assert rows[0]["spec_row"] == "gain-tb.vout"


def test_build_delta_rows_missing_schematic_corner_is_error():
    schematic = _sim_report(
        [{"corner_id": "ss/1.620V/-40C", "measurements": [_measurement(1.0)]}]
    )
    extracted = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.1)]}]
    )
    rows = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert rows[0]["status"] == "error"
    assert rows[0]["schematic_value"] is None
    assert rows[0]["delta_pct"] is None


def test_build_delta_rows_iterates_extracted_corner_order():
    schematic = _sim_report(
        [
            {"corner_id": "ss/1.620V/-40C", "measurements": [_measurement(0.9)]},
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]},
        ]
    )
    extracted = _sim_report(
        [
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.05)]},
            {"corner_id": "ss/1.620V/-40C", "measurements": [_measurement(0.95)]},
        ]
    )
    rows = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert [row["corner_id"] for row in rows] == ["tt/1.800V/27C", "ss/1.620V/-40C"]


# --------------------------------------------------------------------------- #
# `run_pex` error paths that are cheap (no `ngspice`, extraction only)
# --------------------------------------------------------------------------- #

_RES_SQUARES = 6.0
_RES_BAR = kdb.Box(0, 0, 12000, 1000)
_RES_MARKED = kdb.Box(3000, 0, 9000, 1000)
_SHEET_RHO_RES_GENERIC_PO = 48.2


def _write_gds(layout: kdb.Layout, path: Path) -> str:
    layout.write(str(path))
    return str(path)


def _make_sky130_poly_resistor_layout(top_name: str = "RES") -> kdb.Layout:
    """A single drawn sky130 poly resistor with a labelled, contacted head
    at each end -- the same fixture shape `tests/test_extract.py`'s
    `_make_poly_resistor_layout` uses (kept self-contained here rather than
    imported across test modules), extracting to `res_generic_po` with
    `R = L / W * sheet_rho = 6.0 / 1.0 * 48.2 = 289.2` ohm (issue #222)."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    poly = (66, 20)  # poly.drawing
    marker = (66, 13)  # poly.res
    contact = (66, 44)  # licon1.drawing
    metal = (67, 20)  # li1.drawing
    metal_label = (67, 5)  # li1.pin

    draw(*poly, _RES_BAR)
    draw(*marker, _RES_MARKED)
    for x, name in ((1500, "RA"), (10500, "RB")):
        draw(*contact, kdb.Box(x - 100, 400, x + 100, 600))
        draw(*metal, kdb.Box(x - 400, 200, x + 400, 800))
        label(*metal_label, name, x, 500)

    return layout


def _write_schematic_dut(path: Path, r_ohm: float = 289.2) -> Path:
    path.write_text(f".SUBCKT RES RA RB\nR1 RA RB {r_ohm}\n.ENDS RES\n")
    return path


def _write_testbench(
    path: Path, dut_path: Path, load_ohm: float = 1000.0, measurement_name: str = "vout"
) -> Path:
    path.write_text(
        f'.include "{dut_path}"\n'
        ".model res_generic_po r\n"
        "Vdd RA 0 DC 1.8\n"
        "Xres RA RB RES\n"
        f"Rload RB 0 {load_ohm}\n"
    )
    return path


def _write_request(
    path: Path, testbench_path: Path, measurement_name: str = "vout", node: str = "RB"
) -> Path:
    path.write_text(
        json.dumps(
            {
                "netlist": str(testbench_path),
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "measurements": [
                    {
                        "name": measurement_name,
                        "spice": f".meas tran {measurement_name} FIND v({node}) AT=1u",
                    }
                ],
            }
        )
    )
    return path


@pytest.fixture
def resistor_layout(tmp_path):
    return _write_gds(_make_sky130_poly_resistor_layout(), tmp_path / "res.gds")


def test_run_pex_requires_at_least_one_testbench(resistor_layout):
    with pytest.raises(PexError, match="at least one testbench"):
        run_pex(resistor_layout, [], "sky130")


def test_run_pex_testbench_missing_include_raises(tmp_path, resistor_layout):
    dut = tmp_path / "dut.spice"
    dut.write_text("R1 RA RB 289.2\n")  # no .include -- inlined directly
    tb = tmp_path / "testbench.spice"
    tb.write_text("R1 RA RB 289.2\nVdd RA 0 DC 1.8\nRload RB 0 1k\n")
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError, match=r"\.include"):
        run_pex(resistor_layout, [str(request)], "sky130")


def test_run_pex_testbench_netlist_not_found_raises(tmp_path, resistor_layout):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "does-not-exist.spice",
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "measurements": [],
            }
        )
    )

    with pytest.raises(PexError, match="netlist not found"):
        run_pex(resistor_layout, [str(request)], "sky130")


def test_run_pex_disagreeing_reference_netlists_raises_before_simulating(
    tmp_path, resistor_layout, monkeypatch
):
    """The consistency check runs *before* any `klt sim` call -- confirmed
    here by making `run_sim` itself unreachable (monkeypatched to raise if
    ever called) and still getting the documented `PexError`, not a real
    (and therefore `ngspice`-requiring) simulation attempt."""
    import klayout_tools.pex as pex_module

    def _unreachable_run_sim(*_args, **_kwargs):
        raise AssertionError(
            "run_sim must not be called before the DUT-consistency check"
        )

    monkeypatch.setattr(pex_module, "run_sim", _unreachable_run_sim)

    dut_a = _write_schematic_dut(tmp_path / "dut_a.spice")
    tb_a = _write_testbench(tmp_path / "tb_a.spice", dut_a)
    request_a = _write_request(tmp_path / "request_a.json", tb_a)

    dut_b = _write_schematic_dut(tmp_path / "dut_b.spice", r_ohm=300.0)
    tb_b = _write_testbench(tmp_path / "tb_b.spice", dut_b)
    request_b = _write_request(tmp_path / "request_b.json", tb_b)

    with pytest.raises(PexError, match="different schematic DUT netlists"):
        run_pex(resistor_layout, [str(request_a), str(request_b)], "sky130")


def test_run_pex_bad_deck_raises_extract_error_wrapped(tmp_path, resistor_layout):
    dut = _write_schematic_dut(tmp_path / "dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError, match="extraction failed"):
        run_pex(resistor_layout, [str(request)], "not-a-real-deck")


# --------------------------------------------------------------------------- #
# Integration: a real, self-contained `klt pex` run against `ngspice`
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_integration_run_pex_end_to_end(tmp_path, resistor_layout):
    """Extraction adds interconnect parasitic resistance/capacitance on top
    of the drawn device's own 289.2 ohm (issue #592's per-net star topology)
    -- so the extracted-side divider reads a measurably lower voltage than
    the ideal 289.2-ohm schematic reference, and that difference is exactly
    what `delta[]` should report."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert report["schema_version"] == 1
    assert report["status"] == "pass"
    assert report["layout"] == resistor_layout
    assert report["reference_netlist"] == str(dut)
    assert report["extraction"]["deck"] == "sky130"
    assert report["extraction"]["device_count"] == 1
    assert report["extraction"]["net_count"] == 2
    assert report["extraction"]["model"] is not None
    assert report["corner_count"] == 1
    assert report["passed"] == 1
    assert report["failed"] == 0
    assert report["errored"] == 0
    assert len(report["testbenches"]) == 1
    assert report["testbenches"][0]["measurement_names"] == ["vout"]

    (row,) = report["delta"]
    assert row["spec_row"] == "vout"
    assert row["corner_id"] == "default/novdd/27C"
    assert row["status"] == "pass"
    # Ideal schematic divider: 1.8 * 1000 / (1000 + 289.2).
    assert row["schematic_value"] == pytest.approx(1.8 * 1000 / 1289.2, rel=1e-3)
    # Extraction adds series interconnect R -- strictly less signal reaches
    # the load than the ideal schematic value.
    assert row["extracted_value"] < row["schematic_value"]
    assert row["delta_pct"] < 0

    assert report["provenance"]["deck"]["name"] == "sky130"
    assert report["provenance"]["input"]["content_hash"] is not None


@_SKIP_NO_NGSPICE
def test_integration_run_pex_multiple_testbenches(tmp_path, resistor_layout):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")

    tb_a = _write_testbench(tmp_path / "tb_gain.spice", dut, load_ohm=1000.0)
    request_a = _write_request(tmp_path / "request_gain.json", tb_a)

    tb_b = _write_testbench(tmp_path / "tb_psrr.spice", dut, load_ohm=2000.0)
    request_b = _write_request(tmp_path / "request_psrr.json", tb_b)

    report = run_pex(resistor_layout, [str(request_a), str(request_b)], "sky130")

    assert report["status"] == "pass"
    assert len(report["delta"]) == 2
    assert {row["spec_row"] for row in report["delta"]} == {
        "request_gain.vout",
        "request_psrr.vout",
    }
    assert len(report["testbenches"]) == 2


@_SKIP_NO_NGSPICE
def test_run_pex_critical_nets_threaded_through_to_extraction(
    tmp_path, resistor_layout
):
    """`--critical-net` (issue #976, Epic #709 Phase 2a) is forwarded from
    `run_pex` to `run_extract`, and echoed back in `extraction.critical_nets`
    -- a fully wired unit-of-work test, distinct from
    `tests/test_extract.py`'s own exact-value lateral-coupling geometry
    tests. `RB` (this fixture's one labelled met1/li1 net) has no same-layer
    neighbour, so this also exercises the "declared but geometrically
    unmatched" path end to end without raising -- `run_pex` must still
    complete and report the same result as the no-critical-nets baseline
    (this fixture has no lateral-coupling geometry to add)."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130", critical_nets=["RB"])

    assert report["extraction"]["critical_nets"] == ["RB"]
    assert report["status"] == "pass"


@_SKIP_NO_NGSPICE
def test_run_pex_distributed_rc_threaded_through_to_extraction(
    tmp_path, resistor_layout
):
    """`--distributed-rc` (issue #977, Epic #709 Phase 2b) is forwarded from
    `run_pex` to `run_extract`, and echoed back in
    `extraction.distributed_rc` -- a fully wired unit-of-work test, distinct
    from `tests/test_extract.py`'s own exact-value distributed-RC geometry
    tests and `test_run_pex_distributed_rc_canary` below's own fidelity
    proof. `RB` (this fixture's one labelled met1/li1 net) has exactly 1
    device terminal (its own resistor's `RB` pin) -- fewer than 2, so this
    also exercises the "named but nothing to chain, falls back to the star"
    path end to end without raising."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        critical_nets=["RB"],
        distributed_rc=True,
    )

    assert report["extraction"]["critical_nets"] == ["RB"]
    assert report["extraction"]["distributed_rc"] is True
    assert report["status"] == "pass"


def test_run_pex_distributed_rc_requires_critical_net(tmp_path, resistor_layout):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError, match="--distributed-rc requires --critical-net"):
        run_pex(resistor_layout, [str(request)], "sky130", distributed_rc=True)


@_SKIP_NO_NGSPICE
def test_run_pex_mom_rlc_net_threaded_through_to_extraction(tmp_path, resistor_layout):
    """`--mom-rlc-net`/`--mom-rlc-resistance-ohm`/`--mom-rlc-capacitance-ff`
    (issue #988, Epic #709 Phase 3a) are forwarded from `run_pex` to
    `run_extract`, and the substitution report is echoed back in
    `extraction.mom_rlc_override` -- a fully wired unit-of-work test, distinct
    from `tests/test_extract.py`'s own exact-value substitution tests. `RB`
    (this fixture's one labelled met1/li1 net, also this resistor's own load
    node) genuinely carries lumped-RC parasitics, so the override measurably
    changes the extracted-side re-simulation's own value versus the
    no-override baseline."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    baseline = run_pex(resistor_layout, [str(request)], "sky130")
    assert baseline["extraction"]["mom_rlc_override"] is None

    report = run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        mom_rlc_net="RB",
        mom_rlc_resistance_ohm=50_000.0,
        mom_rlc_capacitance_ff=500.0,
    )

    override = report["extraction"]["mom_rlc_override"]
    assert override is not None
    assert override["net"] == "RB"
    assert override["resistance_ohm"] == 50_000.0
    assert override["capacitance_ff"] == 500.0
    assert override["inductance_nh"] is None
    assert report["status"] == "pass"

    # A much larger series resistance on `RB` measurably moves the
    # extracted-side re-simulation's own value versus the no-override
    # baseline (both share the identical schematic-side value/testbench).
    baseline_row = baseline["delta"][0]
    override_row = report["delta"][0]
    assert baseline_row["schematic_value"] == override_row["schematic_value"]
    assert override_row["extracted_value"] != baseline_row["extracted_value"]


def _make_pex_lateral_coupling_layout(top_name: str = "TOP") -> kdb.Layout:
    """Two same-layer (met1) nets, ``AGR`` ("aggressor") and ``VIC``
    ("victim"), 0.1 um apart -- well within sky130 met1's own lateral-
    coupling lookback (``metal_sidewall_lookback_um[1]`` = 0.28 um) --
    purpose-built as the Phase 2a (issue #976) canary fixture below. Zero
    active devices and zero vertical (adjacent-metal-level) relationship
    between the two nets, so the *only* possible schematic-vs-extracted
    coupling path is the one ``--critical-net`` adds -- issue #760's
    already-shipped, unconditional vertical-overlap coupling contributes
    nothing here. Mirrors `tests/test_extract.py`'s own
    ``_make_lateral_coupling_layout`` geometry (kept self-contained here,
    per this module's existing no-cross-module-fixture-imports
    convention)."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(68, 20, kdb.Box(0, 0, 2000, 1000))  # met1, net AGR
    label(68, 5, "AGR", 1000, 500)
    draw(68, 20, kdb.Box(2100, 0, 4100, 1000))  # met1, net VIC -- 0.1 um gap
    label(68, 5, "VIC", 3100, 500)

    return layout


def _write_isolated_schematic_dut(path: Path) -> Path:
    """The Phase 1 schematic-level ideal for the canary below: ``AGR``/
    ``VIC`` are two entirely separate pins with **zero** devices between
    them -- no coupling of any kind, the textbook pre-#976 assumption
    lateral coupling closes part of the gap on."""
    path.write_text(".SUBCKT TOP AGR VIC\n.ENDS TOP\n")
    return path


def _write_lateral_coupling_testbench(path: Path, dut_path: Path) -> Path:
    """A fast (100 ps) step on ``AGR``; ``VIC`` is a genuine high-impedance
    node (Epic #709 Phase 2's own framing) -- a 1 Gohm pull to ground, no
    active driver -- so any coupling capacitance onto it from ``AGR`` shows
    up directly as a transient voltage kick. ``.global vsubs``/``Vsubs``
    ties the extraction's synthesized substrate net (``ground_net_name``,
    ``extract.py``) to a real 0 V reference -- required because this
    fixture has no diffusion/well geometry of its own to tie it down
    structurally, unlike a real chip's substrate contact network, so
    without it ``vsubs`` would float as one shared *internal* node of this
    single ``Xdut`` instance and create its own accidental AGR<->VIC
    coupling path via ``CAGR``/``CVIC`` in series -- an artifact of this
    minimal two-net fixture having no other substrate tie, not a real
    finding. ``.options rshunt=1e12`` matches the same floating-node
    convergence workaround ``examples/design-pipeline/09-sim.testbench.
    spice`` already documents for this deck's own composed-layout canary
    (issue #205)."""
    path.write_text(
        f'.include "{dut_path}"\n'
        ".options rshunt=1e12\n"
        ".global vsubs\n"
        "Vsubs vsubs 0 DC 0\n"
        "Vagr AGR 0 PULSE(0 1 0 100p 100p 100n 200n)\n"
        "Rvic_hiz VIC 0 1e9\n"
        "Xdut AGR VIC TOP\n"
    )
    return path


def _write_lateral_coupling_request(path: Path, testbench_path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "netlist": str(testbench_path),
                "analysis": {"kind": "tran", "args": "1p 5n"},
                "measurements": [
                    {
                        "name": "vic_kick",
                        "spice": ".meas tran vic_kick FIND V(VIC) AT=2n",
                    }
                ],
            }
        )
    )
    return path


@_SKIP_NO_NGSPICE
def test_run_pex_critical_net_lateral_coupling_canary(tmp_path):
    """**Phase 2a canary (issue #976, Epic #709)**: a real, ngspice-driven
    `klt pex` run demonstrating a measurable, explainable delta from
    coupling capacitance vs. Phase 1's lumped-RC-only baseline (issue
    #801/#802/#803) -- the acceptance criterion this test exists to prove,
    not merely a unit test of the CLI/library wiring (that is
    `test_run_pex_critical_nets_threaded_through_to_extraction` above).

    `AGR`/`VIC` (:func:`_make_pex_lateral_coupling_layout`) are two isolated
    met1 nets with zero devices and zero vertical relationship between
    them -- issue #760's already-shipped, unconditional vertical-overlap
    coupling contributes exactly nothing here, isolating this canary to
    issue #976's own lateral pass. `VIC` is a genuine high-impedance node
    (a 1 Gohm pull to ground, no active driver) -- the exact net class
    Epic #709 Phase 2's own text names as this phase's target ("high-
    impedance nodes").

    **Without `--critical-net`** (Phase 1's own `klt pex` baseline, issue
    #801/#802/#803, unchanged by this issue): the extracted netlist has no
    AGR<->VIC coupling path at all, so a 1 V step on `AGR` leaves `VIC`'s
    high-impedance node exactly at its 0 V bias -- `extracted_value == 0.0`,
    measured, not assumed.

    **With `--critical-net VIC`** (this issue): the identical step now
    visibly couples onto `VIC` through the lateral coupling capacitor the
    deck's `metal_sidewalls[1]` (met1) coefficient adds between the two
    nets' parasitic hub nodes -- `extracted_value` jumps to a real,
    nonzero, reproducible voltage shortly after the step.

    A worked-example (`examples/design-pipeline`'s `sky130-ota-5t` canary,
    `docs/cli/pex.md`) is not a fit for this proof: every one of its
    measurement points is an ideal-voltage-source-driven net hub (its own
    "Every row reads `delta_pct: 0.0`" explanation, PR #973), so it has no
    current/charge-carrying, high-impedance node for a coupling-capacitance
    delta to show up on. This test's fixture is purpose-built with exactly
    such a node instead, and is the reproducible, CI-verified canary this
    issue's acceptance criteria cite -- re-derived on every run rather than
    a one-off hand-captured evidence blob.
    """
    layout = _make_pex_lateral_coupling_layout()
    gds_path = _write_gds(layout, tmp_path / "lateral.gds")

    dut = _write_isolated_schematic_dut(tmp_path / "isolated_dut.spice")
    tb = _write_lateral_coupling_testbench(tmp_path / "coupling_tb.spice", dut)
    request = _write_lateral_coupling_request(tmp_path / "coupling.request.json", tb)

    baseline = run_pex(gds_path, [str(request)], "sky130")
    assert baseline["extraction"]["critical_nets"] == []
    baseline_row = baseline["delta"][0]
    assert baseline_row["extracted_value"] == pytest.approx(0.0, abs=1e-6)

    with_coupling = run_pex(gds_path, [str(request)], "sky130", critical_nets=["VIC"])
    assert with_coupling["extraction"]["critical_nets"] == ["VIC"]
    coupled_row = with_coupling["delta"][0]
    assert coupled_row["extracted_value"] == pytest.approx(0.129055, abs=1e-4)

    # The measurable, explainable delta itself: same testbench, same
    # geometry, same schematic reference -- the *only* variable is whether
    # `--critical-net` was given, and it visibly moves the extracted-side
    # measurement from "no coupling" to a real, nonzero coupling kick.
    assert coupled_row["extracted_value"] - baseline_row["extracted_value"] > 0.1


def _make_pex_distributed_rc_layout(top_name: str = "TOP") -> kdb.Layout:
    """Two sky130 poly resistors joined head-to-tail by a long (500 um) li1
    run -- ``SRC --R1(289.2 ohm)--> MID --R2(289.2 ohm)--> LOAD``, where
    ``MID`` is a genuinely internal net with real, substantial interconnect
    geometry of its own (2 device terminals: `R1`'s ``RB``, `R2`'s ``RA``),
    purpose-built as the Phase 2b (issue #977) canary fixture: a long, high-
    impedance routed net -- the exact shape Epic #709 Phase 2's own text
    names ("high-impedance nodes" / "a PLL loop filter"), and the shape
    `--distributed-rc` targets (2+ device terminals to chain into a ladder,
    unlike the Phase 2a canary's zero-device-terminal high-Z probe above).

    li1's own curated sheet resistance/capacitance (``ParasiticsDeck.metals[0]``,
    12.8 ohm/sq, 0.037 fF/um^2 area + 0.0407 fF/um perimeter) over a 500 um x
    0.6 um run gives ``MID`` roughly 11 kohm of series resistance and 53 fF
    of ground capacitance -- large enough that a fast (10 ps rise) step on
    ``SRC`` takes real, measurable time to reach ``MID``, which is exactly
    what distinguishes the star (all of `MID`'s capacitance at one hub, one
    resistor hop away) from the distributed ladder (`MID`'s capacitance
    split across its own node and its neighbour's, two resistor hops away
    from `SRC`) at an early sample point."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    poly = (66, 20)  # poly.drawing
    marker = (66, 13)  # poly.res
    contact = (66, 44)  # licon1.drawing
    li1 = (67, 20)  # li1.drawing
    li1_pin = (67, 5)  # li1.pin

    wire_len = 500_000  # 500 um, in database units (dbu = 0.001 um)

    # Resistor "R1": SRC -> MID (near end of the long li1 run).
    draw(*poly, kdb.Box(0, 0, 12000, 1000))
    draw(*marker, kdb.Box(3000, 0, 9000, 1000))
    draw(*contact, kdb.Box(1400, 400, 1600, 600))
    draw(*li1, kdb.Box(1100, 200, 1900, 800))
    label(*li1_pin, "SRC", 1500, 500)
    draw(*contact, kdb.Box(10400, 400, 10600, 600))
    draw(*li1, kdb.Box(10100, 200, 10100 + wire_len, 800))
    label(*li1_pin, "MID", 10500, 500)

    # Resistor "R2": MID (far end of the long li1 run) -> LOAD. Offset so
    # its own RA landing pad overlaps the wire's far edge (and nothing
    # else this resistor draws reaches back towards R1 or the wire).
    ox = (10100 + wire_len) - 800 - 1100
    draw(*poly, kdb.Box(0 + ox, 0, 12000 + ox, 1000))
    draw(*marker, kdb.Box(3000 + ox, 0, 9000 + ox, 1000))
    draw(*contact, kdb.Box(1400 + ox, 400, 1600 + ox, 600))
    draw(*li1, kdb.Box(1100 + ox, 200, 1900 + ox, 800))
    draw(*contact, kdb.Box(10400 + ox, 400, 10600 + ox, 600))
    draw(*li1, kdb.Box(10100 + ox, 200, 10900 + ox, 800))
    label(*li1_pin, "LOAD", 10500 + ox, 500)

    return layout


def _write_distributed_rc_schematic_dut(path: Path) -> Path:
    """The Phase 1 schematic-level ideal for the canary below: two 289.2 ohm
    resistors (:data:`_SHEET_RHO_RES_GENERIC_PO`'s own value, matching each
    drawn poly resistor's own body) in series through `MID` -- no
    interconnect R/C of any kind, so `V(MID)` tracks `V(SRC)` with zero
    delay (`Rload` below draws negligible current, so there is no resistive
    divider drop either)."""
    path.write_text(
        ".SUBCKT TOP LOAD MID SRC\nR1 SRC MID 289.2\nR2 MID LOAD 289.2\n.ENDS TOP\n"
    )
    return path


def _write_distributed_rc_testbench(path: Path, dut_path: Path) -> Path:
    """A fast (10 ps rise) 1 V step on `SRC`; `LOAD` is a genuine high-
    impedance node (a 1 Gohm pull to ground, no active driver), so the
    extracted-side `MID` node's own interconnect R/C is what determines how
    much of that step has reached `MID` at an early sample point --
    `.global vsubs`/`Vsubs` ties the extraction's synthesized substrate net
    to a real 0 V reference, the same convergence requirement
    `_write_lateral_coupling_testbench` above already documents (issue
    #205's floating-node workaround) -- without it every parasitic ground
    capacitor's `B` terminal floats, and `V(MID)` would track `V(SRC)`
    instantly regardless of any resistor in between (no real capacitive
    loading without a fixed reference)."""
    path.write_text(
        f'.include "{dut_path}"\n'
        ".model res_generic_po r\n"
        ".options rshunt=1e12\n"
        ".global vsubs\n"
        "Vsubs vsubs 0 DC 0\n"
        "Vsrc SRC 0 PULSE(0 1 0 10p 10p 2n 4n)\n"
        "Rload LOAD 0 1e9\n"
        "Xdut LOAD MID SRC TOP\n"
    )
    return path


def _write_distributed_rc_request(path: Path, testbench_path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "netlist": str(testbench_path),
                "analysis": {"kind": "tran", "args": "1p 60p"},
                "measurements": [
                    {
                        "name": "mid_50p",
                        "spice": ".meas tran mid_50p FIND V(MID) AT=50p",
                    }
                ],
            }
        )
    )
    return path


@_SKIP_NO_NGSPICE
def test_run_pex_distributed_rc_canary(tmp_path):
    """**Phase 2b canary (issue #977, Epic #709)**: a real, ngspice-driven
    `klt pex` run demonstrating a measurable, explainable delta between the
    single-lumped-star model (Phase 2a's own baseline -- `--critical-net`
    given, `--distributed-rc` not) and the distributed multi-segment ladder
    (this issue -- both flags given) on the identical layout, testbench, and
    schematic reference -- the acceptance criterion this test exists to
    prove, not merely a unit test of the CLI/library wiring or the JSON
    shape (that is `tests/test_extract.py`'s own exact-value distributed-RC
    tests).

    `MID` (:func:`_make_pex_distributed_rc_layout`) is a genuine internal,
    2-device-terminal, high-impedance routed net (~11 kohm / ~53 fF of its
    own li1 interconnect) -- the exact net class Epic #709 Phase 2's own
    text names ("high-impedance nodes", "a PLL loop filter"). A fast (10 ps
    rise) step on `SRC` reaches `MID` through either 1 resistor hop (the
    star: `SRC -> MID__t0 -> MID` with `MID`'s *entire* capacitance sitting
    at the hub) or 2 (the ladder: `SRC -> MID__t0` first charges its own
    *local* capacitance, *then* the ~11 kohm segment resistor feeds `MID`'s
    own remaining capacitance) -- an extra pole the star topology cannot
    represent, since it always attaches a net's full capacitance directly
    to its own hub.

    **`--critical-net MID`, no `--distributed-rc`** (Phase 2a's own model,
    unchanged by this issue): at `t=50 ps`, `V(MID)` has already risen to
    roughly 13% of the step -- the star's one-hop-to-the-full-capacitance
    path responds faster than the physical two-hop ladder below.

    **`--critical-net MID --distributed-rc`** (this issue): the identical
    step, geometry, and measurement instant now reads a measurably smaller
    `V(MID)` -- the ladder's extra pole genuinely delays how much charge has
    reached `MID` by this early sample point, a real second-order effect a
    single lumped hub cannot represent by construction. Both models are
    computed from the *exact same* underlying `resistance_ohm`/
    `capacitance_ff` totals (conservation verified directly in
    `tests/test_extract.py`), so this is a difference in **where** that R/C
    sits along the net, not in how much of it there is -- and the extracted-
    side JSON's own `parasitics.nets[].segments`/`terminals` entries
    (`extraction` is not itself part of this command's `delta[]` output, but
    is available from the same `klt extract`/`klt pex` run, see
    `docs/cli/extract.md`) let a reader attribute the delta to the exact
    segment/node responsible -- something the star topology's single opaque
    hub capacitor cannot offer. That is this issue's own "more explainable"
    fidelity improvement over Phase 2a's baseline, distinct from (and not
    contingent on) which model's delta happens to have the smaller
    magnitude at any one arbitrarily-chosen sample instant on a transient
    that both models converge to the identical final value on."""
    layout = _make_pex_distributed_rc_layout()
    gds_path = _write_gds(layout, tmp_path / "distributed.gds")

    dut = _write_distributed_rc_schematic_dut(tmp_path / "distributed_dut.spice")
    tb = _write_distributed_rc_testbench(tmp_path / "distributed_tb.spice", dut)
    request = _write_distributed_rc_request(tmp_path / "distributed.request.json", tb)

    baseline = run_pex(gds_path, [str(request)], "sky130", critical_nets=["MID"])
    assert baseline["extraction"]["critical_nets"] == ["MID"]
    assert baseline["extraction"]["distributed_rc"] is False
    baseline_row = baseline["delta"][0]
    assert baseline_row["extracted_value"] == pytest.approx(0.1294, abs=2e-3)

    distributed = run_pex(
        gds_path,
        [str(request)],
        "sky130",
        critical_nets=["MID"],
        distributed_rc=True,
    )
    assert distributed["extraction"]["critical_nets"] == ["MID"]
    assert distributed["extraction"]["distributed_rc"] is True
    distributed_row = distributed["delta"][0]
    assert distributed_row["extracted_value"] == pytest.approx(0.0999, abs=2e-3)

    # The measurable, explainable delta itself: same testbench, same
    # geometry, same schematic reference, same net-level R/C totals -- the
    # *only* variable is whether `--distributed-rc` was given, and it
    # visibly moves the extracted-side measurement, reflecting the ladder's
    # genuine extra propagation pole.
    assert baseline_row["extracted_value"] - distributed_row["extracted_value"] > 0.02
    assert baseline_row["schematic_value"] == pytest.approx(
        distributed_row["schematic_value"]
    )


@_SKIP_NO_NGSPICE
def test_cli_pex_json_matches_klt_signoff_pex_kind(tmp_path, resistor_layout, capsys):
    """`klt pex`'s real `--format json` output classifies as kind `"pex"` in
    `klt signoff` -- the compatibility bar the Curator enhancement on issue
    #801 required against issue #871's provisional shape."""
    from klayout_tools import signoff

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    exit_code = main(
        [
            "pex",
            str(resistor_layout),
            str(request),
            "--deck",
            "sky130",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)

    report_path = tmp_path / "pex_report.json"
    report_path.write_text(json.dumps(payload))

    aggregated = signoff.build_signoff([str(report_path)])
    assert aggregated["status"] == "pass"
    (check,) = aggregated["checks"]
    assert check["kind"] == "pex"
    assert check["passed"] is True


@_SKIP_NO_NGSPICE
def test_cli_pex_text_format(tmp_path, resistor_layout, capsys):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    exit_code = main(["pex", str(resistor_layout), str(request), "--deck", "sky130"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status: pass" in out
    assert "delta:" in out
    assert "vout @" in out


def test_cli_pex_error_exit_code_and_stderr(tmp_path, resistor_layout, capsys):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    exit_code = main(
        [
            "pex",
            str(resistor_layout),
            str(request),
            "--deck",
            "not-a-real-deck",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "pex"
    assert "extraction failed" in err["error"]["message"]
