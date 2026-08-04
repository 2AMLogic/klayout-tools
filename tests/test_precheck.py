"""Tests for `klt precheck` and the `run_precheck` library function.

Fixtures are generated programmatically with `klayout.db` inside the tests --
no dependency on an external corpus, mirroring `tests/test_drc.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.precheck import CHECK_NAMES, PrecheckError, run_precheck

# A real sky130_fd_sc_hd GCD macro produced end to end by `klt synthesize` +
# `klt place-and-route` (real Yosys + real OpenROAD) -- the first exercise of
# `klt precheck` against machine-generated, macro-scale layout rather than
# the hand-drawn analog fixtures above. See tests/corpus/README.md's
# "Machine-generated macro-scale fixture" section for full provenance.
CORPUS_DIR = Path(__file__).parent / "corpus"
PLACE_AND_ROUTE_GDS = CORPUS_DIR / "place_and_route" / "gcd.gds.gz"


def _write(layout: kdb.Layout, tmp_path, name: str = "layout.gds") -> str:
    path = tmp_path / name
    layout.write(str(path))
    return str(path)


def _clean_layout() -> kdb.Layout:
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    top.shapes(poly).insert(kdb.Box(0, 0, 200, 2000))
    return layout


def test_run_precheck_all_checks_present_and_pass_on_clean_layout(tmp_path):
    path = _write(_clean_layout(), tmp_path)

    report = run_precheck(path)

    assert report["schema_version"] == 2
    assert report["file"] == path
    assert report["dbu_um"] == 0.001
    assert report["status"] == "pass"
    assert report["check_count"] == len(CHECK_NAMES)
    assert [c["name"] for c in report["checks"]] == list(CHECK_NAMES)
    for check in report["checks"]:
        assert check["status"] in {"pass", "skipped"}
        assert check["violation_count"] == 0
        assert check["violations"] == []


def test_run_precheck_deterministic_across_runs(tmp_path):
    path = _write(_clean_layout(), tmp_path)

    assert run_precheck(path) == run_precheck(path)


def test_run_precheck_raises_on_missing():
    with pytest.raises(PrecheckError):
        run_precheck("/no/such/path/design.gds")


# ---------------------------------------------------------------------------
# offgrid
# ---------------------------------------------------------------------------


def test_offgrid_skipped_without_grid_um(tmp_path):
    path = _write(_clean_layout(), tmp_path)

    report = run_precheck(path)
    (offgrid,) = [c for c in report["checks"] if c["name"] == "offgrid"]

    assert offgrid["status"] == "skipped"
    assert offgrid["skip_reason"] == "no --grid-um given"


def test_offgrid_flags_vertex_not_on_grid(tmp_path):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    top.shapes(poly).insert(kdb.Box(0, 0, 203, 100))  # 203 not a multiple of 5
    path = _write(layout, tmp_path)

    report = run_precheck(path, grid_um=0.005)
    (offgrid,) = [c for c in report["checks"] if c["name"] == "offgrid"]

    assert offgrid["status"] == "fail"
    assert offgrid["violation_count"] == 1
    (violation,) = offgrid["violations"]
    assert violation["cell"] == "TOP"
    assert violation["layer"] == "66/20"
    assert violation["bbox"] == {"left": 0, "bottom": 0, "right": 203, "top": 100}


def test_offgrid_passes_when_every_vertex_on_grid(tmp_path):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    top.shapes(poly).insert(kdb.Box(0, 0, 200, 100))  # both multiples of 5
    path = _write(layout, tmp_path)

    report = run_precheck(path, grid_um=0.005)
    (offgrid,) = [c for c in report["checks"] if c["name"] == "offgrid"]

    assert offgrid["status"] == "pass"


def test_offgrid_checks_through_instance_placement(tmp_path):
    """A shape on-grid in its own cell's local coordinates but placed by an
    off-grid instance transform is still flagged -- the check inspects
    absolute (top-cell) coordinates, not just each cell's local geometry."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    sub = layout.create_cell("SUB")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    sub.shapes(poly).insert(kdb.Box(0, 0, 100, 100))  # on-grid locally
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans(kdb.Vector(1003, 0))))
    path = _write(layout, tmp_path)

    report = run_precheck(path, grid_um=0.005)
    (offgrid,) = [c for c in report["checks"] if c["name"] == "offgrid"]

    assert offgrid["status"] == "fail"
    assert offgrid["violation_count"] == 1
    assert offgrid["violations"][0]["cell"] == "SUB"


def test_offgrid_raises_on_non_representable_grid(tmp_path):
    path = _write(_clean_layout(), tmp_path)

    with pytest.raises(PrecheckError):
        run_precheck(path, grid_um=0.0)


# ---------------------------------------------------------------------------
# zero_area
# ---------------------------------------------------------------------------


def test_zero_area_flags_degenerate_shape(tmp_path):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    top.shapes(poly).insert(kdb.Box(0, 0, 0, 100))  # zero width -> zero area
    path = _write(layout, tmp_path)

    report = run_precheck(path)
    (zero_area,) = [c for c in report["checks"] if c["name"] == "zero_area"]

    assert zero_area["status"] == "fail"
    assert zero_area["violation_count"] == 1
    assert zero_area["violations"][0]["cell"] == "TOP"
    assert zero_area["violations"][0]["layer"] == "66/20"


def test_zero_area_ignores_text_labels(tmp_path):
    """A text label is not a polygon and must never be reported as a
    zero-area shape -- see `pin_labels_over_drawing` for the check that
    actually validates labels."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    labels = layout.layer(66, 5)
    layout.set_info(labels, kdb.LayerInfo(66, 5, "poly.pin"))
    top.shapes(labels).insert(kdb.Text("A", kdb.Trans(kdb.Vector(0, 0))))
    path = _write(layout, tmp_path)

    report = run_precheck(path)
    (zero_area,) = [c for c in report["checks"] if c["name"] == "zero_area"]

    assert zero_area["status"] == "pass"


def test_zero_area_passes_for_normal_shape(tmp_path):
    path = _write(_clean_layout(), tmp_path)

    report = run_precheck(path)
    (zero_area,) = [c for c in report["checks"] if c["name"] == "zero_area"]

    assert zero_area["status"] == "pass"


# ---------------------------------------------------------------------------
# cell_names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_char", ["#", "/", "\\", ":", "*"])
def test_cell_names_flags_forbidden_character(tmp_path, bad_char):
    # A literal space is deliberately not exercised here: GDSII's own writer
    # legalises a space in a structure name to '$' at write time (a GDSII
    # format constraint, not a `klt precheck` behavior), so it can't survive
    # a write/re-read round trip -- the whitespace branch of
    # `_CELL_NAME_FORBIDDEN_CHARS` is exercised directly in
    # `test_cell_names_flags_whitespace_without_gds_round_trip` below instead.
    layout = kdb.Layout()
    layout.create_cell(f"TOP{bad_char}CELL")
    path = _write(layout, tmp_path)

    report = run_precheck(path)
    (cell_names,) = [c for c in report["checks"] if c["name"] == "cell_names"]

    assert cell_names["status"] == "fail"
    assert cell_names["violation_count"] == 1
    assert cell_names["violations"][0]["cell"] == f"TOP{bad_char}CELL"
    assert (
        bad_char in cell_names["violations"][0]["reason"]
        or repr(bad_char) in cell_names["violations"][0]["reason"]
    )


def test_cell_names_flags_whitespace_without_gds_round_trip():
    """Exercises the whitespace branch of `_CELL_NAME_FORBIDDEN_CHARS`
    directly against an in-memory layout (no write/re-read), since GDSII's
    writer itself legalises a literal space out of a structure name (see the
    note in `test_cell_names_flags_forbidden_character`)."""
    from klayout_tools.precheck import _check_cell_names

    layout = kdb.Layout()
    layout.create_cell("TOP CELL")

    result = _check_cell_names(layout)

    assert result["status"] == "fail"
    assert result["violations"][0]["cell"] == "TOP CELL"


def test_cell_names_passes_for_clean_names(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("sky130_fd_sc_hd__inv_1")
    path = _write(layout, tmp_path)

    report = run_precheck(path)
    (cell_names,) = [c for c in report["checks"] if c["name"] == "cell_names"]

    assert cell_names["status"] == "pass"


def test_cell_names_checks_whole_hierarchy_not_just_top(tmp_path):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    sub = layout.create_cell("SUB#bad")
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans()))
    path = _write(layout, tmp_path)

    report = run_precheck(path)
    (cell_names,) = [c for c in report["checks"] if c["name"] == "cell_names"]

    assert cell_names["status"] == "fail"
    assert cell_names["violations"][0]["cell"] == "SUB#bad"


# ---------------------------------------------------------------------------
# layer_whitelist
# ---------------------------------------------------------------------------


def test_layer_whitelist_skipped_without_allowed_layers(tmp_path):
    path = _write(_clean_layout(), tmp_path)

    report = run_precheck(path)
    (whitelist,) = [c for c in report["checks"] if c["name"] == "layer_whitelist"]

    assert whitelist["status"] == "skipped"
    assert whitelist["skip_reason"] == "no --allowed-layers given"


def test_layer_whitelist_flags_layer_outside_allowlist(tmp_path):
    path = _write(_clean_layout(), tmp_path)

    report = run_precheck(path, allowed_layers=[(65, 20)])  # excludes 66/20
    (whitelist,) = [c for c in report["checks"] if c["name"] == "layer_whitelist"]

    assert whitelist["status"] == "fail"
    assert whitelist["violation_count"] == 1
    assert whitelist["violations"][0]["layer"] == "66/20"
    assert whitelist["violations"][0]["shapes"] == 1


def test_layer_whitelist_passes_when_every_layer_is_allowed(tmp_path):
    path = _write(_clean_layout(), tmp_path)

    report = run_precheck(path, allowed_layers=[(66, 20)])
    (whitelist,) = [c for c in report["checks"] if c["name"] == "layer_whitelist"]

    assert whitelist["status"] == "pass"


def test_layer_whitelist_shapes_are_instance_weighted_through_nested_hierarchy(
    tmp_path,
):
    """Regression for issue #452: `shapes` must reflect true placed-shape
    prevalence -- weighted by each cell's total placement multiplicity
    across the *full* hierarchy -- not just how many shapes are drawn once
    per cell *definition*.

    SUB (1 shape on the checked layer) is placed as a 2x3 array inside MID
    (multiplicity 6), and MID is itself placed twice (as two separate
    single instances) inside TOP. Multiplicities compose
    *multiplicatively* along the nested placements: SUB's true placement
    count is 6 * 2 = 12, not 6 + 2 = 8 (additive) and not just 2 (only
    counting the top-level instance count of MID) -- so the expected
    `shapes` count is 12, while the old per-cell-definition logic would
    have reported 1 (SUB's own, unweighted shape count).
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    mid = layout.create_cell("MID")
    sub = layout.create_cell("SUB")

    reserved = layout.layer(71, 44)  # deliberately outside the allowlist below
    sub.shapes(reserved).insert(kdb.Box(0, 0, 100, 100))

    # SUB placed as a regular 2x3 array inside MID -> multiplicity 6.
    mid.insert(
        kdb.CellInstArray(
            sub.cell_index(),
            kdb.Trans(),
            kdb.Vector(1000, 0),
            kdb.Vector(0, 1000),
            2,
            3,
        )
    )
    # MID placed twice (two separate single instances) inside TOP ->
    # multiplicity 2 -- composing multiplicatively with SUB's own 6 for a
    # true placement count of 12, not 6 + 2 = 8.
    top.insert(kdb.CellInstArray(mid.cell_index(), kdb.Trans()))
    top.insert(kdb.CellInstArray(mid.cell_index(), kdb.Trans(kdb.Vector(5000, 0))))

    path = _write(layout, tmp_path)

    report = run_precheck(path, allowed_layers=[(66, 20)])  # excludes 71/44
    (whitelist,) = [c for c in report["checks"] if c["name"] == "layer_whitelist"]

    assert whitelist["status"] == "fail"
    (violation,) = whitelist["violations"]
    assert violation["layer"] == "71/44"
    assert violation["shapes"] == 12


def test_layer_whitelist_shapes_orphan_cell_counts_as_multiplicity_one(tmp_path):
    """A cell definition with no parent instances anywhere (never placed) is,
    by KLayout's own definition, a top cell (see `layout.top_cells()`) -- it
    exists exactly once, as itself, so it contributes its own unweighted
    shape count rather than zero."""
    layout = kdb.Layout()
    layout.create_cell("TOP")
    orphan = layout.create_cell("ORPHAN")
    reserved = layout.layer(71, 44)
    orphan.shapes(reserved).insert(kdb.Box(0, 0, 100, 100))

    path = _write(layout, tmp_path)

    report = run_precheck(path, allowed_layers=[(66, 20)])
    (whitelist,) = [c for c in report["checks"] if c["name"] == "layer_whitelist"]

    (violation,) = whitelist["violations"]
    assert violation["shapes"] == 1


# ---------------------------------------------------------------------------
# pin_labels_over_drawing
# ---------------------------------------------------------------------------


def _sky130_well_label_layout(*, stray: bool) -> kdb.Layout:
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    nwell = layout.layer(64, 20)
    layout.set_info(nwell, kdb.LayerInfo(64, 20, "nwell.drawing"))
    label = layout.layer(64, 5)
    layout.set_info(label, kdb.LayerInfo(64, 5, "nwell.pin"))
    top.shapes(nwell).insert(kdb.Box(0, 0, 500, 500))
    position = kdb.Vector(9000, 9000) if stray else kdb.Vector(250, 250)
    top.shapes(label).insert(kdb.Text("VPB", kdb.Trans(position)))
    return layout


def test_pin_labels_skipped_without_deck(tmp_path):
    path = _write(_sky130_well_label_layout(stray=True), tmp_path)

    report = run_precheck(path)
    (check,) = [c for c in report["checks"] if c["name"] == "pin_labels_over_drawing"]

    assert check["status"] == "skipped"
    assert check["skip_reason"] == "no --deck given"


def test_pin_labels_flags_label_not_over_drawing(tmp_path):
    path = _write(_sky130_well_label_layout(stray=True), tmp_path)

    report = run_precheck(path, deck="sky130")
    (check,) = [c for c in report["checks"] if c["name"] == "pin_labels_over_drawing"]

    assert check["status"] == "fail"
    assert check["violation_count"] == 1
    violation = check["violations"][0]
    assert violation["cell"] == "TOP"
    assert violation["layer"] == "64/5"
    assert violation["text"] == "VPB"
    assert violation["position"] == {"x": 9000, "y": 9000}


def test_pin_labels_passes_when_label_lands_on_drawing(tmp_path):
    path = _write(_sky130_well_label_layout(stray=False), tmp_path)

    report = run_precheck(path, deck="sky130")
    (check,) = [c for c in report["checks"] if c["name"] == "pin_labels_over_drawing"]

    assert check["status"] == "pass"

    # With a --deck given, provenance pins that deck with a content hash.
    assert report["provenance"]["deck"]["name"] == "sky130"
    assert report["provenance"]["deck"]["content_hash"].startswith("sha256:")


def test_pin_labels_raises_on_unknown_deck(tmp_path):
    path = _write(_sky130_well_label_layout(stray=False), tmp_path)

    with pytest.raises(PrecheckError):
        run_precheck(path, deck="not-a-real-deck")


def test_pin_labels_skips_when_deck_declares_no_label_layers(tmp_path, monkeypatch):
    """A deck with no `well_label`/`poly_label`/`metal_labels` entries at all
    has nothing to pair, so the check reports a skip rather than a vacuous
    pass. Both registered decks (sky130, gf180mcu) declare at least one label
    layer, so this exercises the branch via a monkeypatched bare deck rather
    than a real registered one."""
    import klayout_tools.precheck as precheck_module
    from klayout_tools.decks import ExtractionDeck

    bare_deck = ExtractionDeck(
        active=(1, 0), poly=(2, 0), nwell=(3, 0), contact=(4, 0), metals=((5, 0),)
    )
    monkeypatch.setattr(precheck_module, "get_extraction_deck", lambda name: bare_deck)

    path = _write(_clean_layout(), tmp_path)

    report = run_precheck(path, deck="whatever")
    (check,) = [c for c in report["checks"] if c["name"] == "pin_labels_over_drawing"]

    assert check["status"] == "skipped"
    assert "declares no label layers" in check["skip_reason"]


# ---------------------------------------------------------------------------
# overall status / CLI wiring
# ---------------------------------------------------------------------------


def test_overall_status_fails_if_any_check_fails(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("BAD#NAME")
    path = _write(layout, tmp_path)

    report = run_precheck(path)

    assert report["status"] == "fail"


def test_overall_status_ignores_skipped_checks(tmp_path):
    path = _write(_clean_layout(), tmp_path)

    report = run_precheck(path)

    assert report["status"] == "pass"
    assert any(c["status"] == "skipped" for c in report["checks"])


def test_json_contract(tmp_path, capsys):
    layout = kdb.Layout()
    layout.create_cell("BAD#NAME")
    path = _write(layout, tmp_path)

    assert main(["precheck", str(path), "--format", "json"]) == 3
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "file",
        "dbu_um",
        "status",
        "check_count",
        "checks",
        "provenance",
    }
    assert data["schema_version"] == 2
    assert data["status"] == "fail"
    assert data["check_count"] == len(CHECK_NAMES)

    prov = data["provenance"]
    assert set(prov.keys()) == {
        "klt_version",
        "klayout_version",
        "pdk",
        "deck",
        "input",
    }
    assert isinstance(prov["klt_version"], str)
    assert prov["pdk"] is None
    # No --deck was given, so no extraction deck is resolved.
    assert prov["deck"] is None
    # Issue #331 added `provenance.input`, but `precheck` wasn't in scope for
    # it -- stays null here.
    assert prov["input"] is None

    for check in data["checks"]:
        assert set(check.keys()) == {
            "name",
            "status",
            "violation_count",
            "violations",
            "skip_reason",
        }
        assert check["status"] in {"pass", "fail", "skipped"}


def test_exit_code_pass(tmp_path):
    path = _write(_clean_layout(), tmp_path)
    assert main(["precheck", str(path), "--format", "json"]) == 0


def test_exit_code_fail(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("BAD#NAME")
    path = _write(layout, tmp_path)
    assert main(["precheck", str(path), "--format", "json"]) == 3


def test_default_format_is_text(tmp_path, capsys):
    layout = kdb.Layout()
    layout.create_cell("BAD#NAME")
    path = _write(layout, tmp_path)

    assert main(["precheck", str(path)]) == 3
    out = capsys.readouterr().out
    assert "status: fail" in out
    assert "cell_names: fail" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_grid_um_flag(tmp_path, capsys):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    top.shapes(poly).insert(kdb.Box(0, 0, 203, 100))
    path = _write(layout, tmp_path)

    assert main(["precheck", str(path), "--grid-um", "0.005", "--format", "json"]) == 3
    data = json.loads(capsys.readouterr().out)
    (offgrid,) = [c for c in data["checks"] if c["name"] == "offgrid"]
    assert offgrid["status"] == "fail"


def test_cli_deck_flag_unknown_deck_is_application_error(tmp_path, capsys):
    path = _write(_clean_layout(), tmp_path)

    assert (
        main(["precheck", str(path), "--deck", "not-a-real-deck", "--format", "json"])
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "precheck"


def test_cli_allowed_layers_inline_json(tmp_path, capsys):
    path = _write(_clean_layout(), tmp_path)

    assert (
        main(
            [
                "precheck",
                str(path),
                "--allowed-layers",
                "[[65, 20]]",
                "--format",
                "json",
            ]
        )
        == 3
    )
    data = json.loads(capsys.readouterr().out)
    (whitelist,) = [c for c in data["checks"] if c["name"] == "layer_whitelist"]
    assert whitelist["status"] == "fail"
    assert whitelist["violations"][0]["layer"] == "66/20"


def test_cli_allowed_layers_file(tmp_path, capsys):
    path = _write(_clean_layout(), tmp_path)
    allowed_path = tmp_path / "allowed.json"
    allowed_path.write_text(json.dumps([[66, 20]]))

    assert (
        main(
            [
                "precheck",
                str(path),
                "--allowed-layers",
                str(allowed_path),
                "--format",
                "json",
            ]
        )
        == 0
    )


def test_cli_allowed_layers_bad_json_is_application_error(tmp_path, capsys):
    path = _write(_clean_layout(), tmp_path)

    assert (
        main(
            [
                "precheck",
                str(path),
                "--allowed-layers",
                "not json",
                "--format",
                "json",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "precheck"


# --------------------------------------------------------------------------- #
# OpenROAD-produced, macro-scale standard-cell fixture (issue #436)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not PLACE_AND_ROUTE_GDS.is_file(),
    reason="no OpenROAD-produced place-and-route corpus fixture checked in",
)
def test_openroad_gcd_fixture_runs_full_check_battery():
    """`klt precheck` against the real, OpenROAD-produced GCD macro-scale
    fixture required no verb-side fix (#436): every check in the battery
    runs (none skipped, `--grid-um`/`--deck` both supplied) and passes
    cleanly against thousands of instances and real routing-layer usage."""
    report = run_precheck(str(PLACE_AND_ROUTE_GDS), grid_um=0.005, deck="sky130")

    assert report["schema_version"] == 2
    assert report["file"] == str(PLACE_AND_ROUTE_GDS)
    assert report["check_count"] == len(CHECK_NAMES)
    assert [c["name"] for c in report["checks"]] == list(CHECK_NAMES)

    checks_by_name = {c["name"]: c for c in report["checks"]}
    # `layer_whitelist` is the only check this fixture doesn't opt into
    # (no --allowed-layers given) -- every other check runs for real.
    for name in CHECK_NAMES:
        if name == "layer_whitelist":
            assert checks_by_name[name]["status"] == "skipped"
        else:
            assert checks_by_name[name]["status"] in {"pass", "fail"}

    # Regression pin: a real OpenROAD route-stage output should be on-grid,
    # free of degenerate (zero-area) shapes, and free of forbidden cell-name
    # characters -- KLayout's own DEF->GDS merge and the sky130 standard-cell
    # library views are both well-formed by construction.
    assert report["status"] == "pass"


@pytest.mark.skipif(
    not PLACE_AND_ROUTE_GDS.is_file(),
    reason="no OpenROAD-produced place-and-route corpus fixture checked in",
)
def test_openroad_gcd_fixture_layer_whitelist_shapes_are_instance_weighted():
    """Regression for issue #452, pinned against the real, hundreds-of-
    standard-cell-instances OpenROAD fixture (not just the small hand-drawn
    synthetic case above): a deliberately-excluded real layer's `shapes`
    count must equal the *actual* number of placed shapes on that layer
    across the whole design -- independently verified via a full recursive
    `begin_shapes_rec()` flatten of the top cell -- not the far smaller
    per-cell-definition count the old (buggy) logic reported (which would
    only sum each *distinct* standard-cell view's own shapes once,
    regardless of how many times OpenROAD placed it)."""
    layout = kdb.Layout()
    layout.read(str(PLACE_AND_ROUTE_GDS))
    top = layout.top_cell()

    # 67/44 (a real metal-routing layer in this fixture) is drawn inside
    # many distinct standard-cell definitions, each placed many times --
    # exactly the hierarchical-weighting case issue #452 reports.
    layer_index = layout.find_layer(67, 44)
    assert layer_index is not None

    expected_flat_count = 0
    iterator = top.begin_shapes_rec(layer_index)
    while not iterator.at_end():
        expected_flat_count += 1
        iterator.next()
    # Sanity check this really is a macro-scale, multi-instance count and
    # not a coincidental single-placement layer.
    assert expected_flat_count > 1000

    old_per_definition_count = sum(
        cell.shapes(layer_index).size() for cell in layout.each_cell()
    )
    assert old_per_definition_count < expected_flat_count

    report = run_precheck(
        str(PLACE_AND_ROUTE_GDS),
        allowed_layers=[(1, 0)],  # deliberately excludes every real layer
    )
    (whitelist,) = [c for c in report["checks"] if c["name"] == "layer_whitelist"]
    violations_by_layer = {v["layer"]: v for v in whitelist["violations"]}

    assert violations_by_layer["67/44"]["shapes"] == expected_flat_count
