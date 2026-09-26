"""Arc-by-arc accuracy of `klt characterize` against a vendor Liberty file
(issue #2503, closing #2498's first milestone).

Two tiers:

- **Harness unit tests** (always run) exercise
  `klayout_tools.liberty_compare` on small synthetic libraries: the Liberty
  reader, point-by-point deltas, the ``max(rel * |ref|, abs)`` tolerance,
  interpolation onto a different grid, leakage matching by input state
  (including a vendor ``when`` that names the output pin), and -- the
  manual test plan's concern -- that nothing is silently left out of the
  comparison: every absent group lands in ``missing`` and every field
  reports how many points it actually compared.
- **The vendor comparison** (skipped unless the real IHP-Open-PDK install
  with compiled OSDI models and `ngspice` are present) characterizes a
  handful of `sg13g2_stdcell` cells with `klt characterize` and asserts every
  arc's delay/transition/power delta, and every leakage state, falls within
  `liberty_compare.DEFAULT_TOLERANCES` against the vendor's own
  `sg13g2_stdcell_typ_1p20V_25C.lib`. CI does not fetch the PDK (it is
  hundreds of MB, see `pdks/README.md`), so this tier runs on a developer
  host that has run `scripts/fetch-ihp-sg13g2.sh` and
  `scripts/fetch-sg13g2-sim-toolchain.sh` -- the same local-only posture
  `tests/test_pdk.py`'s real-PDK tier takes.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import characterize, liberty_compare
from klayout_tools.cli import main

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


# --------------------------------------------------------------------------- #
# Synthetic Liberty fixtures
# --------------------------------------------------------------------------- #


def _values(rows: list[list[float]]) -> str:
    return ", \\\n".join('"' + ", ".join(f"{v:g}" for v in row) + '"' for row in rows)


def _table(kind: str, rows: list[list[float]], i1=(0.1, 0.2), i2=(0.01, 0.02)):
    return (
        f"      {kind} (tmpl) {{\n"
        f'        index_1 ("{", ".join(f"{v:g}" for v in i1)}");\n'
        f'        index_2 ("{", ".join(f"{v:g}" for v in i2)}");\n'
        f"        values ( \\\n{_values(rows)} \\\n        );\n"
        "      }\n"
    )


def _inverter_lib(
    *,
    scale: float = 1.0,
    power_scale: float = 1.0,
    leakage: tuple[float, float] = (40.0, 80.0),
    leakage_whens: tuple[str, str] = ("!A", "A"),
    with_power: bool = True,
    with_leakage: bool = True,
    i1=(0.1, 0.2),
    i2=(0.01, 0.02),
    extra_cells: str = "",
) -> str:
    delay = [[0.10 * scale, 0.20 * scale], [0.15 * scale, 0.25 * scale]]
    slew = [[0.05 * scale, 0.12 * scale], [0.08 * scale, 0.16 * scale]]
    energy = [[0.002 * power_scale, 0.0021 * power_scale], [0.003, 0.0031]]
    timing = (
        "      timing () {\n"
        '        related_pin : "A";\n'
        "        timing_sense : negative_unate;\n"
        + _table("cell_rise", delay, i1, i2)
        + _table("cell_fall", delay, i1, i2)
        + _table("rise_transition", slew, i1, i2)
        + _table("fall_transition", slew, i1, i2)
        + "      }\n"
    )
    power = (
        (
            "      internal_power () {\n"
            '        related_pin : "A";\n'
            + _table("rise_power", energy, i1, i2)
            + _table("fall_power", energy, i1, i2)
            + "      }\n"
        )
        if with_power
        else ""
    )
    leak = (
        (
            f"    cell_leakage_power : {sum(leakage) / 2:g};\n"
            f"    leakage_power () {{ value : {leakage[0]:g}; "
            f'when : "{leakage_whens[0]}"; }}\n'
            f"    leakage_power () {{ value : {leakage[1]:g}; "
            f'when : "{leakage_whens[1]}"; }}\n'
        )
        if with_leakage
        else ""
    )
    return (
        "/* synthetic */\n"
        "library (synthetic) {\n"
        '  time_unit : "1ns";\n'
        "  capacitive_load_unit (1, pf);\n"
        "  cell (inv) {\n"
        "    area : 1;\n"
        + leak
        + '    pin (A) { direction : "input"; capacitance : 0.002; }\n'
        "    pin (Y) {\n"
        '      direction : "output";\n'
        '      function : "!A";\n' + timing + power + "    }\n"
        "  }\n" + extra_cells + "}\n"
    )


def _write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


# --------------------------------------------------------------------------- #
# Harness unit tests (no simulator, no PDK)
# --------------------------------------------------------------------------- #


def test_reader_parses_groups_attributes_and_tables(tmp_path):
    path = _write(tmp_path, "lib.lib", _inverter_lib())
    cells = liberty_compare.load_cells(path)

    inv = cells["inv"]
    assert inv.inputs == ("A",) and inv.outputs == ("Y",)
    assert inv.functions == {"Y": "!A"}
    assert set(inv.timing[("Y", "A")]) == set(liberty_compare.TIMING_FIELDS)
    assert set(inv.power[("Y", "A")]) == set(liberty_compare.POWER_FIELDS)
    assert inv.timing[("Y", "A")]["cell_rise"].values == ((0.1, 0.2), (0.15, 0.25))
    assert inv.leakage == [("!A", 40.0), ("A", 80.0)]
    assert inv.cell_leakage_power == 60.0


def test_reader_refuses_a_file_with_no_library(tmp_path):
    path = _write(tmp_path, "empty.lib", "/* nothing */\n")
    with pytest.raises(liberty_compare.LibertyCompareError, match="no library"):
        liberty_compare.load_cells(path)


def test_reader_refuses_an_unclosed_group(tmp_path):
    path = _write(tmp_path, "broken.lib", "library (x) {\n  cell (inv) {\n")
    with pytest.raises(liberty_compare.LibertyCompareError, match="never closed"):
        liberty_compare.load_cells(path)


def test_identical_libraries_compare_exactly(tmp_path):
    ours = _write(tmp_path, "ours.lib", _inverter_lib())
    ref = _write(tmp_path, "ref.lib", _inverter_lib())

    result = liberty_compare.compare_libraries(ours, ref)

    summary = result["summary"]
    assert summary["within_tolerance"] is True
    assert summary["missing"] == []
    assert set(summary["fields"]) == set(liberty_compare.FIELDS)
    for name, field in summary["fields"].items():
        assert field["max_abs_delta"] == 0, name
    # Every table point, every leakage state, and the cell total compared.
    for name in liberty_compare.TIMING_FIELDS + liberty_compare.POWER_FIELDS:
        assert summary["fields"][name]["compared"] == 4
    assert summary["fields"]["leakage_power"]["compared"] == 2
    assert summary["fields"]["cell_leakage_power"]["compared"] == 1


def test_a_delay_outside_tolerance_is_reported_point_by_point(tmp_path):
    ours = _write(tmp_path, "ours.lib", _inverter_lib(scale=1.5))
    ref = _write(tmp_path, "ref.lib", _inverter_lib())

    result = liberty_compare.compare_libraries(ours, ref)

    field = result["summary"]["fields"]["cell_rise"]
    assert field["within_tolerance"] is False
    assert field["violations"] == 4
    assert field["max_rel_delta"] == pytest.approx(0.5)
    table = result["cells"][0]["arcs"][0]["tables"]["cell_rise"]
    worst = table["worst"]
    assert worst["ours"] == pytest.approx(1.5 * worst["reference"])
    # 50% off against the default relative bound.
    bound = liberty_compare.DEFAULT_TOLERANCES["cell_rise"]["rel"]
    assert worst["tolerance_used"] == pytest.approx(0.5 / bound)
    assert worst["within_tolerance"] is False
    assert result["summary"]["within_tolerance"] is False


def test_the_absolute_floor_absorbs_near_zero_references(tmp_path):
    """A 50% relative miss on a 2 fJ entry is inside a 5 fJ absolute floor --
    the reason the tolerance is `max(rel * |ref|, abs)`."""
    ours = _write(tmp_path, "ours.lib", _inverter_lib(power_scale=1.5))
    ref = _write(tmp_path, "ref.lib", _inverter_lib())

    tight = liberty_compare.compare_libraries(
        ours, ref, tolerances={"rise_power": {"rel": 0.1, "abs": 0.0}}
    )
    floored = liberty_compare.compare_libraries(
        ours, ref, tolerances={"rise_power": {"rel": 0.1, "abs": 0.005}}
    )

    assert tight["summary"]["fields"]["rise_power"]["within_tolerance"] is False
    assert floored["summary"]["fields"]["rise_power"]["within_tolerance"] is True


def test_a_different_grid_is_interpolated_from_the_reference(tmp_path):
    """Our grid at the reference's axis midpoints: the reference values are
    bilinear in both axes here, so interpolation reproduces them exactly."""
    ref = _write(tmp_path, "ref.lib", _inverter_lib())
    # Build "ours" on midpoints with the bilinear-interpolated values.
    ours_text = _inverter_lib(i1=(0.15, 0.2), i2=(0.015, 0.02))
    # Reference cell_rise at (0.1|0.2) x (0.01|0.02) is [[0.1, 0.2], [0.15,
    # 0.25]]; at the midpoint grid that is [[0.175, 0.225], [0.2, 0.25]].
    ours_text = ours_text.replace(
        '"0.1, 0.2", \\\n"0.15, 0.25"', '"0.175, 0.225", \\\n"0.2, 0.25"'
    )
    ours = _write(tmp_path, "ours.lib", ours_text)

    result = liberty_compare.compare_libraries(ours, ref)

    table = result["cells"][0]["arcs"][0]["tables"]["cell_rise"]
    assert table["interpolated"] is True
    assert table["max_abs_delta"] == pytest.approx(0.0, abs=1e-12)


def test_a_subset_of_the_reference_grid_is_read_directly(tmp_path):
    """Our axes a subset of the reference's (the vendor comparison's 3x3 out
    of IHP's 7x7): every point is a reference node, so nothing is
    interpolated and the reference value is read verbatim."""

    def timing_only(rows, i1, i2):
        return (
            "library (synthetic) {\n"
            "  cell (inv) {\n"
            '    pin (A) { direction : "input"; }\n'
            '    pin (Y) {\n      direction : "output";\n      function : "!A";\n'
            '      timing () {\n        related_pin : "A";\n'
            + _table("cell_rise", rows, i1, i2)
            + "      }\n    }\n  }\n}\n"
        )

    # The reference's first and third axis entries are our two; the middle
    # row/column (9s) would poison any interpolation that touched it.
    ref = _write(
        tmp_path,
        "ref.lib",
        timing_only(
            [[0.1, 9, 0.2], [9, 9, 9], [0.15, 9, 0.25]],
            (0.1, 0.15, 0.2),
            (0.01, 0.015, 0.02),
        ),
    )
    ours = _write(
        tmp_path,
        "ours.lib",
        timing_only([[0.1, 0.2], [0.15, 0.25]], (0.1, 0.2), (0.01, 0.02)),
    )

    result = liberty_compare.compare_libraries(ours, ref)

    table = result["cells"][0]["arcs"][0]["tables"]["cell_rise"]
    assert table["interpolated"] is False
    assert table["max_abs_delta"] == 0


def test_leakage_matches_a_vendor_when_that_names_the_output(tmp_path):
    """IHP writes `when : "A&!Y"` (the output state too); ours writes the
    input-only `"A"`. They must still be matched by input state."""
    ours = _write(tmp_path, "ours.lib", _inverter_lib())
    ref = _write(tmp_path, "ref.lib", _inverter_lib(leakage_whens=("!A&Y", "A&!Y")))

    result = liberty_compare.compare_libraries(ours, ref)

    states = result["cells"][0]["leakage"]["states"]
    assert [(s["when"], s["reference_when"]) for s in states] == [
        ("!A", "!A&Y"),
        ("A", "A&!Y"),
    ]
    assert result["summary"]["missing"] == []


def test_leakage_out_of_tolerance_is_reported(tmp_path):
    ours = _write(tmp_path, "ours.lib", _inverter_lib(leakage=(40.0, 40.0)))
    ref = _write(tmp_path, "ref.lib", _inverter_lib())

    result = liberty_compare.compare_libraries(ours, ref)

    field = result["summary"]["fields"]["leakage_power"]
    assert field["violations"] == 1
    assert field["worst"]["where"] == "inv leakage_power A"


def test_nothing_absent_is_silently_omitted(tmp_path):
    """The manual test plan's concern, automated: a reference with no power
    or leakage groups must surface as `missing`, the fields must report zero
    compared points, and the overall verdict must not be `within_tolerance`
    -- "never compared" is not "within tolerance"."""
    ours = _write(tmp_path, "ours.lib", _inverter_lib())
    ref = _write(
        tmp_path, "ref.lib", _inverter_lib(with_power=False, with_leakage=False)
    )

    result = liberty_compare.compare_libraries(ours, ref)

    summary = result["summary"]
    assert "inv: internal_power A->Y: not in the reference" in summary["missing"]
    assert "inv: cell_leakage_power: not in the reference" in summary["missing"]
    assert any("leakage_power when" in entry for entry in summary["missing"])
    for name in ("rise_power", "fall_power", "leakage_power", "cell_leakage_power"):
        assert summary["fields"][name]["compared"] == 0
        assert summary["fields"][name]["within_tolerance"] is False
    assert summary["within_tolerance"] is False


def test_a_cell_absent_from_the_reference_is_missing_not_an_error(tmp_path):
    extra = (
        "  cell (buf) {\n"
        '    pin (A) { direction : "input"; }\n'
        '    pin (X) { direction : "output"; function : "A"; }\n'
        "  }\n"
    )
    ours = _write(tmp_path, "ours.lib", _inverter_lib(extra_cells=extra))
    ref = _write(tmp_path, "ref.lib", _inverter_lib())

    result = liberty_compare.compare_libraries(ours, ref)

    assert "buf: not in the reference library" in result["summary"]["missing"]


def test_asking_for_a_cell_ours_lacks_is_an_error(tmp_path):
    ours = _write(tmp_path, "ours.lib", _inverter_lib())
    with pytest.raises(liberty_compare.LibertyCompareError, match="nand2"):
        liberty_compare.compare_libraries(ours, ours, cells=["nand2"])


def test_only_requested_reference_cells_are_interpreted(tmp_path):
    """A vendor file's sequential cells carry scalar/1D tables the 2D
    comparison does not model; restricting to our cells must never trip on
    them."""
    sequential = (
        "  cell (dff) {\n"
        "    pin (Q) {\n"
        '      direction : "output";\n'
        "      internal_power () {\n"
        '        related_pin : "CLK";\n'
        '        rise_power (scalar) { values ("0.001"); }\n'
        "      }\n"
        "    }\n"
        "  }\n"
    )
    ours = _write(tmp_path, "ours.lib", _inverter_lib())
    ref = _write(tmp_path, "ref.lib", _inverter_lib(extra_cells=sequential))

    result = liberty_compare.compare_libraries(ours, ref)

    assert result["summary"]["within_tolerance"] is True


def test_documented_tolerance_table_matches_the_enforced_bounds():
    """The tolerance is *documented* and *enforced*: the table in
    docs/cli/characterize.md must state exactly the bounds the vendor
    comparison asserts, so neither can drift without the other."""
    import re

    doc = (
        Path(__file__).resolve().parents[1] / "docs" / "cli" / "characterize.md"
    ).read_text()
    rows = {
        match.group(1): (float(match.group(2)), float(match.group(3)))
        for match in re.finditer(
            r"^\| `(\w+)` \| ([0-9.]+) \| ([0-9.]+) (?:ns|pJ|pW) \|", doc, re.M
        )
    }
    assert set(rows) == set(liberty_compare.FIELDS)
    for name, bound in liberty_compare.DEFAULT_TOLERANCES.items():
        assert rows[name] == (bound["rel"], bound["abs"]), name


def test_default_tolerances_cover_every_compared_field():
    assert set(liberty_compare.DEFAULT_TOLERANCES) == set(liberty_compare.FIELDS)
    for bound in liberty_compare.DEFAULT_TOLERANCES.values():
        assert set(bound) == {"rel", "abs"}
        assert bound["rel"] > 0 and bound["abs"] >= 0


# --------------------------------------------------------------------------- #
# The vendor comparison: real sg13g2_stdcell cells vs. IHP's own .lib
# --------------------------------------------------------------------------- #


def _find_ihp_sg13g2() -> Path | None:
    """The fetched IHP-Open-PDK `ihp-sg13g2` variant: `$KLT_IHP_SG13G2_ROOT`
    (the variant directory itself), else `pdks/ihp-open-pdk/ihp-sg13g2` in
    this checkout, else in the checkout this worktree hangs off (a Loom
    worktree under `.loom/worktrees/` shares the main checkout's gitignored
    `pdks/`)."""
    candidates = []
    override = os.environ.get("KLT_IHP_SG13G2_ROOT")
    if override:
        candidates.append(Path(override))
    repo = Path(__file__).resolve().parents[1]
    candidates.append(repo / "pdks" / "ihp-open-pdk" / "ihp-sg13g2")
    if repo.parent.name == "worktrees" and repo.parent.parent.name == ".loom":
        candidates.append(repo.parents[2] / "pdks" / "ihp-open-pdk" / "ihp-sg13g2")
    for root in candidates:
        if (root / "libs.tech" / "ngspice" / "osdi" / "psp103.osdi").is_file() and (
            root / "libs.ref" / "sg13g2_stdcell" / "lib"
        ).is_dir():
            return root
    return None


IHP_SG13G2 = _find_ihp_sg13g2()
HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_VENDOR = pytest.mark.skipif(
    IHP_SG13G2 is None or not HAVE_NGSPICE,
    reason=(
        "needs ngspice plus a fetched IHP-Open-PDK with compiled OSDI models "
        "(scripts/fetch-ihp-sg13g2.sh + scripts/fetch-sg13g2-sim-toolchain.sh, "
        "or KLT_IHP_SG13G2_ROOT=<.../ihp-sg13g2>)"
    ),
)

#: The "handful": both unate polarities, one- and two-input cells, series
#: and parallel stacks on each rail.
VENDOR_CELLS = (
    ("sg13g2_inv_1", ("A",), ("Y", "!(A)")),
    ("sg13g2_buf_1", ("A",), ("X", "A")),
    ("sg13g2_nand2_1", ("A", "B"), ("Y", "!(A*B)")),
    ("sg13g2_nor2_1", ("A", "B"), ("Y", "!(A+B)")),
)

#: A 3x3 subset of the vendor's own 7x7 `TIMING_DELAY_7x7ds1` axes -- every
#: point is a vendor grid point, so the comparison reads the vendor value
#: directly (no interpolation), while the four cells' sweep stays a few
#: minutes of ngspice rather than a quarter hour. It spans the fast/slow and
#: light/heavy corners plus the interior where the timing delta peaks.
VENDOR_GRID = {
    "input_transition_ns": [0.0186, 0.3294, 1.263],
    "output_load_pf": [0.001, 0.0648, 0.3],
}


def _vendor_request(root: Path) -> dict:
    netlist = root / "libs.ref" / "sg13g2_stdcell" / "spice" / "sg13g2_stdcell.spice"
    osdi = root / "libs.tech" / "ngspice" / "osdi"
    return {
        "cells": [
            {
                "name": name,
                "netlist": str(netlist),
                "pins": [{"name": pin, "direction": "input"} for pin in inputs]
                + [{"name": output[0], "direction": "output", "function": output[1]}],
                "power_pins": {"vdd": "VDD", "gnd": "VSS"},
            }
            for name, inputs, output in VENDOR_CELLS
        ],
        "corner": {
            "name": "typ_1p20V_25C",
            "process": "mos_tt",
            "supply_v": 1.2,
            "temperature_c": 25,
        },
        "grid": VENDOR_GRID,
        "models": {
            "lib": str(root / "libs.tech" / "ngspice" / "models" / "cornerMOSlv.lib")
        },
        "options": {
            "osdi_preload": [
                str(osdi / name)
                for name in (
                    "psp103.osdi",
                    "psp103_nqs.osdi",
                    "r3_cmc.osdi",
                    "mosvar.osdi",
                )
            ],
        },
    }


@pytest.fixture(scope="module")
def vendor_run(tmp_path_factory):
    assert IHP_SG13G2 is not None
    workdir = tmp_path_factory.mktemp("vendor")
    request = workdir / "request.json"
    request.write_text(json.dumps(_vendor_request(IHP_SG13G2)))
    reference = (
        IHP_SG13G2
        / "libs.ref"
        / "sg13g2_stdcell"
        / "lib"
        / "sg13g2_stdcell_typ_1p20V_25C.lib"
    )
    report = characterize.run_characterize(str(request), compare_to=str(reference))
    return report, workdir, reference


@_SKIP_NO_VENDOR
def test_vendor_comparison_covers_every_field_of_every_arc(vendor_run):
    """No field silently omitted: every arc of every cell compared on all six
    tables, and every leakage state matched to a vendor group."""
    report, _, _ = vendor_run
    comparison = report["comparison"]
    summary = comparison["summary"]

    assert summary["missing"] == [], summary["missing"]
    points = len(VENDOR_GRID["input_transition_ns"]) * len(
        VENDOR_GRID["output_load_pf"]
    )
    arc_count = sum(len(inputs) for _, inputs, _ in VENDOR_CELLS)
    for name in liberty_compare.TIMING_FIELDS + liberty_compare.POWER_FIELDS:
        assert summary["fields"][name]["compared"] == points * arc_count, name
    states = sum(2 ** len(inputs) for _, inputs, _ in VENDOR_CELLS)
    assert summary["fields"]["leakage_power"]["compared"] == states
    assert summary["fields"]["cell_leakage_power"]["compared"] == len(VENDOR_CELLS)
    for cell in comparison["cells"]:
        for arc in cell["arcs"]:
            assert set(arc["tables"]) == set(
                liberty_compare.TIMING_FIELDS + liberty_compare.POWER_FIELDS
            )
            for table in arc["tables"].values():
                assert table["interpolated"] is False


@_SKIP_NO_VENDOR
@pytest.mark.parametrize("field", liberty_compare.FIELDS)
def test_vendor_comparison_is_within_the_documented_tolerance(vendor_run, field):
    """The acceptance bar: arc-by-arc deltas against IHP's own library fall
    inside `DEFAULT_TOLERANCES` (see docs/cli/characterize.md for how each
    bound was chosen). Parametrized per field so a regression names the
    field it broke."""
    report, _, _ = vendor_run
    entry = report["comparison"]["summary"]["fields"][field]

    assert entry["compared"] > 0
    assert entry["within_tolerance"], (
        f"{field}: {entry['violations']} point(s) outside "
        f"{entry['tolerance']}; worst {json.dumps(entry['worst'])}"
    )


@_SKIP_NO_VENDOR
def test_vendor_batch_lib_round_trips_through_statime(vendor_run):
    report, _, _ = vendor_run
    assert report["liberty"]["cell_count"] == len(VENDOR_CELLS)
    assert report["liberty"]["roundtrip"]["status"] != "fail"


@_SKIP_NO_VENDOR
def test_vendor_comparison_via_the_cli(vendor_run, capsys):
    """The same comparison through `klt characterize --compare-to`, reusing
    the emitted `.lib` would not exercise the CLI path -- so re-run only the
    cheapest cell (`--cell sg13g2_inv_1`) and check the text report names
    every compared field."""
    _, workdir, reference = vendor_run
    exit_code = main(
        [
            "characterize",
            str(workdir / "request.json"),
            "-o",
            str(workdir / "cli"),
            "--cell",
            "sg13g2_inv_1",
            "--compare-to",
            str(reference),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "comparison against:" in out
    for field in liberty_compare.FIELDS:
        assert f"  {field}: " in out
    assert "NOT COMPARED" not in out
