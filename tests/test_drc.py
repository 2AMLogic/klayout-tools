"""Tests for `klt drc` and the `run_drc` library function.

Most fixtures are generated programmatically with `klayout.db` inside the
tests — no dependency on an external corpus, mirroring `tests/test_layers.py`.
The gf180mcu section additionally exercises the real corpus layouts checked
in under `tests/corpus/gf180mcu/` (see `tests/corpus/README.md`).
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.decks import get_deck
from klayout_tools.drc import DrcError, run_drc

# poly.width.1 (sky130 deck): minimum poly width is 150 dbu (0.15 um).
_POLY_WIDTH_THRESHOLD_DBU = 150

# poly2.width.1 (gf180mcu deck): minimum poly2 interconnect width is 180 dbu
# (0.18 um).
_GF180MCU_POLY2_WIDTH_THRESHOLD_DBU = 180

CORPUS_DIR = Path(__file__).parent / "corpus"
GF180MCU_CORPUS_FILES = sorted((CORPUS_DIR / "gf180mcu").glob("*.gds"))

REPO_ROOT = Path(__file__).parent.parent
# The literal, relative path baked into examples/drc/example.drc.json's "file"
# field. `run_drc` echoes exactly the path string it is given, so the test must
# pass this same relative string (CI runs pytest from the repo root).
EXAMPLE_GDS = "examples/drc/example.gds"
EXAMPLE_DRC_JSON = REPO_ROOT / "examples" / "drc" / "example.drc.json"


def _make_violation_layout() -> kdb.Layout:
    """A layout with one clear, seeded `poly.width.1` violation.

    A single elongated poly bar narrower than the 150 dbu threshold produces
    exactly one width-check violation (verified empirically: an elongated
    shape's `width_check` reports one edge pair per narrow run, unlike a
    square shape which reports one pair per violating edge direction).
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    # width 50 dbu < 150 dbu threshold, long enough that only the width
    # (not the end caps) triggers a violation.
    top.shapes(poly).insert(kdb.Box(0, 0, 50, 2000))
    return layout


def _make_clean_layout() -> kdb.Layout:
    """A layout with a poly shape wide enough to pass every deck rule."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    # width 200 dbu >= 150 dbu threshold.
    top.shapes(poly).insert(kdb.Box(0, 0, 200, 2000))
    return layout


def test_run_drc_reports_seeded_violation(tmp_path):
    path = tmp_path / "violation.gds"
    _make_violation_layout().write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["schema_version"] == 1
    assert report["file"] == str(path)
    assert report["deck"] == "sky130"
    assert report["dbu_um"] == 0.001
    assert report["status"] == "violations"
    assert report["violation_count"] == 1
    assert report["rule_counts"] == {"poly.width.1": 1}

    (violation,) = report["violations"]
    assert violation["rule"] == "poly.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "poly.drawing"
    assert violation["cell"] == "TOP"
    assert violation["bbox"] == {"left": 0, "bottom": 0, "right": 50, "top": 2000}
    assert violation["polygon"] == [[0, 0], [0, 2000], [50, 2000], [50, 0]]


def test_run_drc_clean(tmp_path):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    assert report["rule_counts"] == {}
    assert report["violations"] == []


def test_run_drc_deterministic_across_runs(tmp_path):
    path = tmp_path / "violation.gds"
    _make_violation_layout().write(str(path))

    first = run_drc(str(path), "sky130")
    second = run_drc(str(path), "sky130")

    assert first == second


def test_run_drc_raises_on_missing():
    with pytest.raises(DrcError):
        run_drc("/no/such/path/design.gds", "sky130")


def test_run_drc_raises_on_unknown_deck(tmp_path):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))

    with pytest.raises(DrcError):
        run_drc(str(path), "not-a-real-deck")


# ---------------------------------------------------------------------------
# coverage block (#189)
# ---------------------------------------------------------------------------


def _sky130_deck_layer_tuples() -> set[tuple[int, int]]:
    tuples: set[tuple[int, int]] = set()
    for rule in get_deck("sky130"):
        tuples.add(rule.layer)
        if rule.other_layer is not None:
            tuples.add(rule.other_layer)
    return tuples


def _fmt_layer(layer_tuple: tuple[int, int]) -> str:
    return f"{layer_tuple[0]}/{layer_tuple[1]}"


def test_run_drc_coverage_empty_for_layout_using_only_covered_layers(tmp_path):
    """A layout drawn entirely on a layer the sky130 deck has rules for
    reports an empty `layers_in_stream_without_rules` -- nothing was drawn
    outside the deck's coverage."""
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))  # poly.drawing (66/20) only

    report = run_drc(str(path), "sky130")

    assert report["coverage"]["layers_in_stream_without_rules"] == []
    assert "66/20" in report["coverage"]["layers_checked"]


def test_run_drc_coverage_reports_uncovered_stream_layers(tmp_path):
    """A layout drawn entirely on a layer the sky130 deck has no rules for
    at all reports it in `layers_in_stream_without_rules`, and every deck
    rule is skipped -- the #189 reproducer: this used to report `"clean"`
    with no indication anything was skipped."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    unused = layout.layer(99, 0)
    layout.set_info(unused, kdb.LayerInfo(99, 0, "not.in.any.deck.rule"))
    top.shapes(unused).insert(kdb.Box(0, 0, 1000, 1000))
    path = tmp_path / "uncovered.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["coverage"]["layers_in_stream_without_rules"] == ["99/0"]
    assert report["coverage"]["layers_checked"] == []
    assert report["coverage"]["rules_skipped"] == sorted(
        rule.id for rule in get_deck("sky130")
    )


def test_run_drc_coverage_rules_skipped_matches_absent_layers(tmp_path):
    """`coverage.rules_skipped` lists exactly the deck rules whose layer(s)
    are absent from the stream -- here, only `poly.drawing` (66/20) is
    present, so every rule that references any other layer is skipped."""
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))  # poly.drawing (66/20) only

    report = run_drc(str(path), "sky130")

    present = {(66, 20)}
    expected_skipped = sorted(
        rule.id
        for rule in get_deck("sky130")
        if rule.layer not in present
        or (rule.other_layer is not None and rule.other_layer not in present)
    )
    assert report["coverage"]["rules_skipped"] == expected_skipped


def test_run_drc_coverage_empty_stream(tmp_path):
    """A layout with no layers registered at all (degenerate empty stream)
    has empty `layers_checked` and `layers_in_stream_without_rules`, and
    does not raise -- every deck rule is skipped."""
    layout = kdb.Layout()
    layout.create_cell("TOP")
    path = tmp_path / "empty.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["coverage"]["layers_checked"] == []
    assert report["coverage"]["layers_in_stream_without_rules"] == []
    assert set(report["coverage"]["deck_layers"]) == {
        _fmt_layer(t) for t in _sky130_deck_layer_tuples()
    }
    assert report["coverage"]["rules_skipped"] == sorted(
        rule.id for rule in get_deck("sky130")
    )


def test_run_drc_coverage_fully_covered_stream(tmp_path):
    """A layout drawing a shape on every layer the sky130 deck's rules
    reference reports `layers_checked` == `deck_layers`, an empty
    `layers_in_stream_without_rules`, and no rules skipped."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    # A GDS write drops layers with no shapes (empty layers do not round-trip
    # through the stream format), so every deck layer needs at least one
    # shape to be findable via `Layout.find_layer(...)` after re-reading.
    for layer_num, datatype in _sky130_deck_layer_tuples():
        layer_index = layout.layer(layer_num, datatype)
        top.shapes(layer_index).insert(kdb.Box(0, 0, 10, 10))
    path = tmp_path / "fully_covered.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["coverage"]["layers_in_stream_without_rules"] == []
    assert set(report["coverage"]["layers_checked"]) == set(
        report["coverage"]["deck_layers"]
    )
    assert report["coverage"]["rules_skipped"] == []


def test_json_contract(tmp_path, capsys):
    path = tmp_path / "violation.gds"
    _make_violation_layout().write(str(path))

    assert main(["drc", str(path), "--deck", "sky130", "--format", "json"]) == 3
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "file",
        "deck",
        "dbu_um",
        "status",
        "violation_count",
        "rule_counts",
        "violations",
        "coverage",
    }
    assert data["schema_version"] == 1
    assert isinstance(data["file"], str)
    assert data["deck"] == "sky130"
    assert isinstance(data["dbu_um"], float)
    assert data["status"] in {"clean", "violations"}
    assert isinstance(data["violation_count"], int)
    assert isinstance(data["rule_counts"], dict)
    assert isinstance(data["violations"], list)
    assert sum(data["rule_counts"].values()) == data["violation_count"]
    assert len(data["violations"]) == data["violation_count"]

    for entry in data["violations"]:
        assert set(entry.keys()) == {
            "rule",
            "description",
            "check",
            "layer",
            "cell",
            "bbox",
            "polygon",
        }
        assert isinstance(entry["rule"], str)
        assert isinstance(entry["description"], str)
        assert isinstance(entry["check"], str)
        assert isinstance(entry["layer"], str)
        assert isinstance(entry["cell"], str)
        assert set(entry["bbox"].keys()) == {"left", "bottom", "right", "top"}
        assert all(isinstance(v, int) for v in entry["bbox"].values())
        assert entry["polygon"] is None or isinstance(entry["polygon"], list)

    # Deterministic sort: (rule, cell, bbox.left, bbox.bottom).
    keys = [
        (e["rule"], e["cell"], e["bbox"]["left"], e["bbox"]["bottom"])
        for e in data["violations"]
    ]
    assert keys == sorted(keys)

    coverage = data["coverage"]
    assert set(coverage.keys()) == {
        "deck_layers",
        "layers_checked",
        "layers_in_stream_without_rules",
        "rules_skipped",
    }
    for field in coverage.values():
        assert isinstance(field, list)
        assert all(isinstance(v, str) for v in field)


def test_json_contract_clean(tmp_path, capsys):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))

    assert main(["drc", str(path), "--deck", "sky130", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert data["status"] == "clean"
    assert data["violation_count"] == 0
    assert data["rule_counts"] == {}
    assert data["violations"] == []


def test_default_format_is_text(tmp_path, capsys):
    path = tmp_path / "violation.gds"
    _make_violation_layout().write(str(path))

    assert main(["drc", str(path), "--deck", "sky130"]) == 3
    out = capsys.readouterr().out
    assert "status:" in out
    assert "poly.width.1" in out
    # Not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_text_format_reports_unchecked_layers_summary(tmp_path, capsys):
    """`--format text` gains a one-line coverage summary when
    `layers_in_stream_without_rules` is non-empty (#189)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    unused = layout.layer(99, 0)
    layout.set_info(unused, kdb.LayerInfo(99, 0, "not.in.any.deck.rule"))
    top.shapes(unused).insert(kdb.Box(0, 0, 1000, 1000))
    path = tmp_path / "uncovered.gds"
    layout.write(str(path))

    assert main(["drc", str(path), "--deck", "sky130"]) == 0
    out = capsys.readouterr().out

    assert "status: clean" in out
    assert "unchecked layers in stream: 1" in out


def test_text_format_no_coverage_summary_when_fully_checked(tmp_path, capsys):
    """No coverage summary line when `layers_in_stream_without_rules` is
    empty -- the summary is only a courtesy for the non-empty case, not
    noise on every run."""
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))  # poly.drawing (66/20) only

    assert main(["drc", str(path), "--deck", "sky130"]) == 0
    out = capsys.readouterr().out

    assert "unchecked layers in stream" not in out


def test_exit_code_clean(tmp_path):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    assert main(["drc", str(path), "--deck", "sky130", "--format", "json"]) == 0


def test_exit_code_violations(tmp_path):
    path = tmp_path / "violation.gds"
    _make_violation_layout().write(str(path))
    assert main(["drc", str(path), "--deck", "sky130", "--format", "json"]) == 3


def test_unknown_deck(tmp_path, capsys):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))

    assert main(["drc", str(path), "--deck", "not-a-real-deck"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "klt drc" in captured.err
    assert "unknown deck" in captured.err


def test_unknown_deck_json_format(tmp_path, capsys):
    """An unknown deck is an application error, so it uses the same envelope."""
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))

    assert (
        main(["drc", str(path), "--deck", "not-a-real-deck", "--format", "json"]) == 1
    )
    captured = capsys.readouterr()

    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "drc"
    assert "unknown deck" in error["error"]["message"]


# ---------------------------------------------------------------------------
# gf180mcu deck
# ---------------------------------------------------------------------------


def _make_gf180mcu_violation_layout() -> kdb.Layout:
    """A layout with one clear, seeded `poly2.width.1` violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly2 = layout.layer(30, 0)
    layout.set_info(poly2, kdb.LayerInfo(30, 0, "Poly2"))
    # width 60 dbu < 180 dbu threshold, long enough that only the width
    # (not the end caps) triggers a violation.
    top.shapes(poly2).insert(kdb.Box(0, 0, 60, 2000))
    return layout


def _make_gf180mcu_clean_layout() -> kdb.Layout:
    """A layout with a poly2 shape wide enough to pass every deck rule."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly2 = layout.layer(30, 0)
    layout.set_info(poly2, kdb.LayerInfo(30, 0, "Poly2"))
    # width 300 dbu >= 240 dbu (poly2.space.1's shape-to-shape spacing isn't
    # exercised by a single shape) and >= 180 dbu threshold.
    top.shapes(poly2).insert(kdb.Box(0, 0, 300, 2000))
    return layout


def test_run_drc_gf180mcu_reports_seeded_violation(tmp_path):
    path = tmp_path / "violation.gds"
    _make_gf180mcu_violation_layout().write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["schema_version"] == 1
    assert report["deck"] == "gf180mcu"
    assert report["dbu_um"] == 0.001
    assert report["status"] == "violations"
    assert report["violation_count"] == 1
    assert report["rule_counts"] == {"poly2.width.1": 1}

    (violation,) = report["violations"]
    assert violation["rule"] == "poly2.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "Poly2"
    assert violation["cell"] == "TOP"
    assert violation["bbox"] == {"left": 0, "bottom": 0, "right": 60, "top": 2000}


def test_run_drc_gf180mcu_clean(tmp_path):
    path = tmp_path / "clean.gds"
    _make_gf180mcu_clean_layout().write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    assert report["rule_counts"] == {}
    assert report["violations"] == []


def test_run_drc_gf180mcu_missing_layer_does_not_crash(tmp_path):
    """A layout that doesn't have every layer the gf180mcu deck references
    (e.g. no Contact, no Metal1) still runs cleanly -- rules whose layer is
    absent from the stream are simply skipped, mirroring how the sky130
    deck already handles this (see `run_drc`'s `find_layer` guard)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly2 = layout.layer(30, 0)
    layout.set_info(poly2, kdb.LayerInfo(30, 0, "Poly2"))
    top.shapes(poly2).insert(kdb.Box(0, 0, 300, 2000))
    path = tmp_path / "partial.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["schema_version"] == 1
    assert report["deck"] == "gf180mcu"
    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_gf180mcu_json_contract(tmp_path, capsys):
    path = tmp_path / "violation.gds"
    _make_gf180mcu_violation_layout().write(str(path))

    assert main(["drc", str(path), "--deck", "gf180mcu", "--format", "json"]) == 3
    data = json.loads(capsys.readouterr().out)

    assert data["schema_version"] == 1
    assert data["deck"] == "gf180mcu"
    assert data["status"] == "violations"
    assert sum(data["rule_counts"].values()) == data["violation_count"]
    assert len(data["violations"]) == data["violation_count"]


@pytest.mark.skipif(
    not GF180MCU_CORPUS_FILES, reason="no gf180mcu corpus files checked in"
)
def test_gf180mcu_corpus_is_non_empty():
    """Guard against a silently-empty corpus (e.g. a broken glob)."""
    assert len(GF180MCU_CORPUS_FILES) >= 1


@pytest.mark.parametrize(
    "layout_path", GF180MCU_CORPUS_FILES, ids=[p.name for p in GF180MCU_CORPUS_FILES]
)
def test_gf180mcu_corpus_layout_produces_well_formed_report(layout_path: Path):
    """`klt drc --deck gf180mcu` against a real gf180mcu standard-cell
    layout (from `tests/corpus/gf180mcu/`, see #4/#20) produces a
    well-formed report -- exercising the deck against real GDSII, not just
    synthetic seeded fixtures."""
    report = run_drc(str(layout_path), "gf180mcu")

    assert report["schema_version"] == 1
    assert report["file"] == str(layout_path)
    assert report["deck"] == "gf180mcu"
    assert report["dbu_um"] == 0.001
    assert report["status"] in {"clean", "violations"}
    assert isinstance(report["violation_count"], int)
    assert report["violation_count"] == sum(report["rule_counts"].values())
    assert len(report["violations"]) == report["violation_count"]

    for entry in report["violations"]:
        assert set(entry.keys()) == {
            "rule",
            "description",
            "check",
            "layer",
            "cell",
            "bbox",
            "polygon",
        }


def _make_gf180mcu_four_layer_clean_layout() -> kdb.Layout:
    """A layout drawn only on the four originally-covered layers
    (Poly2/Comp/Contact/Metal1), sized to satisfy every one of those
    layers' rules. Regression fixture (see #157): confirms the well/tap and
    BJT rules added on top of the original 10 don't fire on layouts that
    never draw Nwell or DRC_BJT geometry.
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    poly2 = layout.layer(30, 0)
    layout.set_info(poly2, kdb.LayerInfo(30, 0, "Poly2"))
    top.shapes(poly2).insert(kdb.Box(0, 0, 300, 2000))

    comp = layout.layer(22, 0)
    layout.set_info(comp, kdb.LayerInfo(22, 0, "Comp"))
    top.shapes(comp).insert(kdb.Box(1000, 0, 1300, 2000))

    contact = layout.layer(33, 0)
    layout.set_info(contact, kdb.LayerInfo(33, 0, "Contact"))
    top.shapes(contact).insert(kdb.Box(2000, 0, 2300, 300))

    metal1 = layout.layer(34, 0)
    layout.set_info(metal1, kdb.LayerInfo(34, 0, "Metal1"))
    top.shapes(metal1).insert(kdb.Box(3000, 0, 3300, 2000))

    return layout


def test_run_drc_gf180mcu_four_layer_layout_still_clean(tmp_path):
    """Regression check (#157): a layout using only the current four layers
    (Poly2/Comp/Contact/Metal1) still reports `"status": "clean"` now that
    the deck also has well/tap (Nwell) and BJT (DRC_BJT) rules -- those
    rules require their own layers (Nwell / DRC_BJT), which are absent from
    this layout, so they must not introduce any new violations here."""
    path = tmp_path / "four_layer_clean.gds"
    _make_gf180mcu_four_layer_clean_layout().write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    assert report["rule_counts"] == {}
    assert report["violations"] == []


# --- nwell.space.1 -----------------------------------------------------


def test_run_drc_gf180mcu_nwell_space_violation(tmp_path):
    """Two Nwell shapes closer than the 600 dbu (0.6 um) `nwell.space.1`
    threshold trip exactly one violation (elongated shapes so only the
    spacing, not the end caps, triggers)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    nwell = layout.layer(21, 0)
    layout.set_info(nwell, kdb.LayerInfo(21, 0, "Nwell"))
    top.shapes(nwell).insert(kdb.Box(0, 0, 2000, 4000))
    top.shapes(nwell).insert(kdb.Box(2100, 0, 4000, 4000))  # 100 dbu gap < 600
    path = tmp_path / "nwell_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"nwell.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "nwell.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "Nwell"


def test_run_drc_gf180mcu_nwell_space_clean(tmp_path):
    """Two Nwell shapes spaced exactly at the 600 dbu threshold pass."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    nwell = layout.layer(21, 0)
    layout.set_info(nwell, kdb.LayerInfo(21, 0, "Nwell"))
    top.shapes(nwell).insert(kdb.Box(0, 0, 2000, 4000))
    top.shapes(nwell).insert(kdb.Box(2600, 0, 4000, 4000))  # 600 dbu gap == threshold
    path = tmp_path / "nwell_space_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


# --- nwell.enclosing.comp.1 ---------------------------------------------


def test_run_drc_gf180mcu_nwell_enclosing_comp_violation(tmp_path):
    """A COMP shape inside an Nwell shape, but with less than the 120 dbu
    (0.12 um) `nwell.enclosing.comp.1` margin on one edge, trips exactly one
    violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    nwell = layout.layer(21, 0)
    layout.set_info(nwell, kdb.LayerInfo(21, 0, "Nwell"))
    comp = layout.layer(22, 0)
    layout.set_info(comp, kdb.LayerInfo(22, 0, "Comp"))
    top.shapes(nwell).insert(kdb.Box(0, 0, 2000, 2000))
    # 300 dbu margin on 3 sides, only 50 dbu (< 120) margin on the right.
    top.shapes(comp).insert(kdb.Box(300, 300, 1950, 1700))
    path = tmp_path / "nwell_enclosing_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"nwell.enclosing.comp.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "nwell.enclosing.comp.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "Nwell"


def test_run_drc_gf180mcu_nwell_enclosing_comp_clean(tmp_path):
    """A COMP shape enclosed by Nwell with >= 120 dbu margin on every side
    passes."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    nwell = layout.layer(21, 0)
    layout.set_info(nwell, kdb.LayerInfo(21, 0, "Nwell"))
    comp = layout.layer(22, 0)
    layout.set_info(comp, kdb.LayerInfo(22, 0, "Comp"))
    top.shapes(nwell).insert(kdb.Box(0, 0, 2000, 2000))
    top.shapes(comp).insert(kdb.Box(300, 300, 1700, 1700))  # 300 dbu margin >= 120
    path = tmp_path / "nwell_enclosing_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


# --- bjt.separation.comp.1 ------------------------------------------------


def test_run_drc_gf180mcu_bjt_separation_violation(tmp_path):
    """A DRC_BJT shape closer than the 100 dbu (0.1 um)
    `bjt.separation.comp.1` threshold to a COMP shape trips exactly one
    violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    bjt = layout.layer(127, 5)
    layout.set_info(bjt, kdb.LayerInfo(127, 5, "DRC_BJT"))
    comp = layout.layer(22, 0)
    layout.set_info(comp, kdb.LayerInfo(22, 0, "Comp"))
    top.shapes(bjt).insert(kdb.Box(0, 0, 500, 500))
    top.shapes(comp).insert(kdb.Box(550, 0, 1000, 500))  # 50 dbu gap < 100
    path = tmp_path / "bjt_separation_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"bjt.separation.comp.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "bjt.separation.comp.1"
    assert violation["check"] == "separation"
    assert violation["layer"] == "DRC_BJT"


def test_run_drc_gf180mcu_bjt_separation_clean(tmp_path):
    """A DRC_BJT shape spaced exactly at the 100 dbu threshold from a COMP
    shape passes."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    bjt = layout.layer(127, 5)
    layout.set_info(bjt, kdb.LayerInfo(127, 5, "DRC_BJT"))
    comp = layout.layer(22, 0)
    layout.set_info(comp, kdb.LayerInfo(22, 0, "Comp"))
    top.shapes(bjt).insert(kdb.Box(0, 0, 500, 500))
    top.shapes(comp).insert(kdb.Box(600, 0, 1000, 500))  # 100 dbu gap == threshold
    path = tmp_path / "bjt_separation_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


# --- metal2.width.1 / metal2.space.1 (#188) -----------------------------


def test_run_drc_gf180mcu_metal2_width_violation(tmp_path):
    """A Metal2 bar narrower than the 280 dbu (0.28 um) `metal2.width.1`
    threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal2 = layout.layer(36, 0)
    layout.set_info(metal2, kdb.LayerInfo(36, 0, "Metal2"))
    top.shapes(metal2).insert(kdb.Box(0, 0, 100, 2000))  # 100 dbu < 280
    path = tmp_path / "metal2_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"metal2.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "metal2.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "Metal2"


def test_run_drc_gf180mcu_metal2_space_violation(tmp_path):
    """Two Metal2 bars closer than the 280 dbu `metal2.space.1` threshold
    trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal2 = layout.layer(36, 0)
    layout.set_info(metal2, kdb.LayerInfo(36, 0, "Metal2"))
    top.shapes(metal2).insert(kdb.Box(0, 0, 2000, 4000))
    top.shapes(metal2).insert(kdb.Box(2100, 0, 4000, 4000))  # 100 dbu gap < 280
    path = tmp_path / "metal2_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"metal2.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "metal2.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "Metal2"


def test_run_drc_gf180mcu_metal2_clean(tmp_path):
    """A Metal2 bar wide enough to satisfy `metal2.width.1` passes."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal2 = layout.layer(36, 0)
    layout.set_info(metal2, kdb.LayerInfo(36, 0, "Metal2"))
    top.shapes(metal2).insert(kdb.Box(0, 0, 300, 2000))  # 300 >= 280
    path = tmp_path / "metal2_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


# --- metal3.width.1 / metal3.space.1 (#188) -----------------------------


def test_run_drc_gf180mcu_metal3_width_violation(tmp_path):
    """A Metal3 bar narrower than the 280 dbu `metal3.width.1` threshold
    trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal3 = layout.layer(42, 0)
    layout.set_info(metal3, kdb.LayerInfo(42, 0, "Metal3"))
    top.shapes(metal3).insert(kdb.Box(0, 0, 100, 2000))  # 100 dbu < 280
    path = tmp_path / "metal3_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"metal3.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "metal3.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "Metal3"


def test_run_drc_gf180mcu_metal3_space_violation(tmp_path):
    """Two Metal3 bars closer than the 280 dbu `metal3.space.1` threshold
    trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal3 = layout.layer(42, 0)
    layout.set_info(metal3, kdb.LayerInfo(42, 0, "Metal3"))
    top.shapes(metal3).insert(kdb.Box(0, 0, 2000, 4000))
    top.shapes(metal3).insert(kdb.Box(2100, 0, 4000, 4000))  # 100 dbu gap < 280
    path = tmp_path / "metal3_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"metal3.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "metal3.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "Metal3"


def test_run_drc_gf180mcu_metal3_clean(tmp_path):
    """A Metal3 bar wide enough to satisfy `metal3.width.1` passes."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal3 = layout.layer(42, 0)
    layout.set_info(metal3, kdb.LayerInfo(42, 0, "Metal3"))
    top.shapes(metal3).insert(kdb.Box(0, 0, 300, 2000))  # 300 >= 280
    path = tmp_path / "metal3_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


# --- metal5.width.1 / metal5.space.1 (#188) -----------------------------


def test_run_drc_gf180mcu_metal5_width_violation(tmp_path):
    """A Metal5 bar narrower than the 280 dbu `metal5.width.1` threshold
    trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal5 = layout.layer(81, 0)
    layout.set_info(metal5, kdb.LayerInfo(81, 0, "Metal5"))
    top.shapes(metal5).insert(kdb.Box(0, 0, 100, 2000))  # 100 dbu < 280
    path = tmp_path / "metal5_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"metal5.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "metal5.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "Metal5"


def test_run_drc_gf180mcu_metal5_space_violation(tmp_path):
    """Two Metal5 bars closer than the 280 dbu `metal5.space.1` threshold
    trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal5 = layout.layer(81, 0)
    layout.set_info(metal5, kdb.LayerInfo(81, 0, "Metal5"))
    top.shapes(metal5).insert(kdb.Box(0, 0, 2000, 4000))
    top.shapes(metal5).insert(kdb.Box(2100, 0, 4000, 4000))  # 100 dbu gap < 280
    path = tmp_path / "metal5_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"metal5.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "metal5.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "Metal5"


def test_run_drc_gf180mcu_metal5_clean(tmp_path):
    """A Metal5 bar wide enough to satisfy `metal5.width.1` passes."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal5 = layout.layer(81, 0)
    layout.set_info(metal5, kdb.LayerInfo(81, 0, "Metal5"))
    top.shapes(metal5).insert(kdb.Box(0, 0, 300, 2000))  # 300 >= 280
    path = tmp_path / "metal5_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


# --- metaltop.width.1 / metaltop.space.1 (#188) --------------------------


def test_run_drc_gf180mcu_metaltop_width_violation(tmp_path):
    """A MetalTop bar narrower than the 360 dbu (0.36 um) `metaltop.width.1`
    threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metaltop = layout.layer(53, 0)
    layout.set_info(metaltop, kdb.LayerInfo(53, 0, "MetalTop"))
    top.shapes(metaltop).insert(kdb.Box(0, 0, 100, 2000))  # 100 dbu < 360
    path = tmp_path / "metaltop_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"metaltop.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "metaltop.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "MetalTop"


def test_run_drc_gf180mcu_metaltop_space_violation(tmp_path):
    """Two MetalTop bars closer than the 380 dbu (0.38 um) `metaltop.space.1`
    threshold trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metaltop = layout.layer(53, 0)
    layout.set_info(metaltop, kdb.LayerInfo(53, 0, "MetalTop"))
    top.shapes(metaltop).insert(kdb.Box(0, 0, 2000, 4000))
    top.shapes(metaltop).insert(kdb.Box(2100, 0, 4000, 4000))  # 100 dbu gap < 380
    path = tmp_path / "metaltop_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"metaltop.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "metaltop.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "MetalTop"


def test_run_drc_gf180mcu_metaltop_clean(tmp_path):
    """A MetalTop bar wide enough to satisfy `metaltop.width.1` passes."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metaltop = layout.layer(53, 0)
    layout.set_info(metaltop, kdb.LayerInfo(53, 0, "MetalTop"))
    top.shapes(metaltop).insert(kdb.Box(0, 0, 400, 2000))  # 400 >= 360
    path = tmp_path / "metaltop_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


# --- mim.space.1 (MIMTM.1) / mim.enclosing.fusetop.1 (MIMTM.3) (#188) ----


def test_run_drc_gf180mcu_mim_space_violation(tmp_path):
    """Two Metal4 bars closer than the 1200 dbu (1.2 um) `mim.space.1`
    threshold trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal4 = layout.layer(46, 0)
    layout.set_info(metal4, kdb.LayerInfo(46, 0, "Metal4"))
    top.shapes(metal4).insert(kdb.Box(0, 0, 2000, 4000))
    top.shapes(metal4).insert(kdb.Box(2300, 0, 4000, 4000))  # 300 dbu gap < 1200
    path = tmp_path / "mim_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"mim.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "mim.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "Metal4"


def test_run_drc_gf180mcu_mim_space_clean(tmp_path):
    """Two Metal4 bars spaced exactly at the 1200 dbu threshold pass."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal4 = layout.layer(46, 0)
    layout.set_info(metal4, kdb.LayerInfo(46, 0, "Metal4"))
    top.shapes(metal4).insert(kdb.Box(0, 0, 2000, 4000))
    top.shapes(metal4).insert(kdb.Box(3200, 0, 5000, 4000))  # 1200 dbu gap == threshold
    path = tmp_path / "mim_space_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_gf180mcu_mim_enclosing_fusetop_violation(tmp_path):
    """A FuseTop shape hanging off the edge of its Metal4 bottom plate by
    less than the 600 dbu (0.6 um) `mim.enclosing.fusetop.1` margin trips
    exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal4 = layout.layer(46, 0)
    layout.set_info(metal4, kdb.LayerInfo(46, 0, "Metal4"))
    fusetop = layout.layer(75, 0)
    layout.set_info(fusetop, kdb.LayerInfo(75, 0, "FuseTop"))
    top.shapes(metal4).insert(kdb.Box(0, 0, 5000, 5000))
    # 1000 dbu margin on 3 sides, only 100 dbu (< 600) margin on the right.
    top.shapes(fusetop).insert(kdb.Box(1000, 1000, 4900, 4000))
    path = tmp_path / "mim_enclosing_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"mim.enclosing.fusetop.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "mim.enclosing.fusetop.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "Metal4"


def test_run_drc_gf180mcu_mim_enclosing_fusetop_clean(tmp_path):
    """A FuseTop shape enclosed by its Metal4 bottom plate with >= 600 dbu
    margin on every side passes."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal4 = layout.layer(46, 0)
    layout.set_info(metal4, kdb.LayerInfo(46, 0, "Metal4"))
    fusetop = layout.layer(75, 0)
    layout.set_info(fusetop, kdb.LayerInfo(75, 0, "FuseTop"))
    top.shapes(metal4).insert(kdb.Box(0, 0, 5000, 5000))
    top.shapes(fusetop).insert(kdb.Box(1000, 1000, 4000, 4000))  # 1000 margin >= 600
    path = tmp_path / "mim_enclosing_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_gf180mcu_reproducer_from_issue(tmp_path):
    """The exact reproducer geometry from issue #188: illegal MiM-stack and
    upper-metal geometry that used to report `"status": "clean"` (no rule
    coverage at all on these layers) now reports the expected violations.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("PROBE")
    m4 = layout.layer(46, 0)
    layout.set_info(m4, kdb.LayerInfo(46, 0, "Metal4"))
    ft = layout.layer(75, 0)
    layout.set_info(ft, kdb.LayerInfo(75, 0, "FuseTop"))
    v4 = layout.layer(41, 0)
    layout.set_info(v4, kdb.LayerInfo(41, 0, "Via4"))
    m5 = layout.layer(81, 0)
    layout.set_info(m5, kdb.LayerInfo(81, 0, "Metal5"))
    m2 = layout.layer(36, 0)
    layout.set_info(m2, kdb.LayerInfo(36, 0, "Metal2"))
    m3 = layout.layer(42, 0)
    layout.set_info(m3, kdb.LayerInfo(42, 0, "Metal3"))

    top.shapes(m4).insert(kdb.Box(0, 0, 5000, 5000))  # bottom plate
    top.shapes(m4).insert(kdb.Box(5300, 0, 7300, 5000))  # 0.3 um away
    top.shapes(ft).insert(kdb.Box(4800, 200, 6000, 4800))  # top plate hangs off
    top.shapes(v4).insert(kdb.Box(-100, 500, 100, 700))  # via straddles edge
    top.shapes(m5).insert(kdb.Box(4900, 2000, 7000, 3000))
    top.shapes(m2).insert(kdb.Box(0, 6000, 4000, 6050))  # 0.05 um wide
    top.shapes(m3).insert(kdb.Box(0, 6200, 4000, 6250))  # 0.05 um wide

    path = tmp_path / "probe.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["violation_count"] > 0
    assert "mim.space.1" in report["rule_counts"]
    assert "mim.enclosing.fusetop.1" in report["rule_counts"]
    assert "metal2.width.1" in report["rule_counts"]
    assert "metal3.width.1" in report["rule_counts"]


def test_gf180mcu_corpus_cli_json_matches_run_drc(capsys):
    """`klt drc <file> --deck gf180mcu --format json` (the CLI path) agrees
    with the `run_drc` library call for at least one real corpus file."""
    layout_path = GF180MCU_CORPUS_FILES[0]

    expected = run_drc(str(layout_path), "gf180mcu")
    exit_code = main(
        ["drc", str(layout_path), "--deck", "gf180mcu", "--format", "json"]
    )
    actual = json.loads(capsys.readouterr().out)

    assert exit_code == (0 if expected["status"] == "clean" else 3)
    assert actual == expected


def test_example_gds_matches_committed_json():
    """Drift guard: `run_drc` on the checked-in worked example (see
    `docs/cli/drc.md`) still produces exactly the committed
    `examples/drc/example.drc.json`.

    `run_drc`'s output is fully deterministic -- `violations` is sorted and
    there are no timestamp/environment-dependent fields -- so no
    normalization is needed. If this fails, either the DRC output shape or
    the sky130 deck rules changed; regenerate the fixture per the header of
    `examples/drc/generate.py`.
    """
    expected = json.loads(EXAMPLE_DRC_JSON.read_text())
    actual = run_drc(EXAMPLE_GDS, "sky130")

    assert actual == expected


# ---------------------------------------------------------------------------
# dbu invariance (#172): the same physical geometry must produce the same
# DRC verdict regardless of the stream's own database unit. Rule thresholds
# are authored against each deck's nominal dbu (0.001 um for both sky130 and
# gf180mcu, see decks/sky130.py and decks/gf180mcu.py) but `run_drc` must
# rescale them to whatever dbu the layout under test actually uses.
# ---------------------------------------------------------------------------

# dbu_um values chosen (matching the issue's own repro table) so that the
# physical widths/lengths below divide evenly -- no rounding ambiguity, so
# bbox comparisons after conversion to physical units are exact.
_DBU_INVARIANCE_DBU_VALUES = [0.001, 0.005, 0.0005]


def _make_sky130_violation_layout_at_dbu(dbu_um: float) -> kdb.Layout:
    """Same physical geometry as `_make_violation_layout` (a poly bar 0.05 um
    wide, 2.0 um long -- narrower than `poly.width.1`'s 0.15 um threshold),
    written at an arbitrary `dbu_um`."""
    layout = kdb.Layout()
    layout.dbu = dbu_um
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    width = round(0.05 / dbu_um)
    length = round(2.0 / dbu_um)
    top.shapes(poly).insert(kdb.Box(0, 0, width, length))
    return layout


def _make_gf180mcu_violation_layout_at_dbu(dbu_um: float) -> kdb.Layout:
    """Same physical geometry as `_make_gf180mcu_violation_layout` (a Poly2
    bar 0.06 um wide, 2.0 um long -- narrower than `poly2.width.1`'s 0.18 um
    threshold), written at an arbitrary `dbu_um`."""
    layout = kdb.Layout()
    layout.dbu = dbu_um
    top = layout.create_cell("TOP")
    poly2 = layout.layer(30, 0)
    layout.set_info(poly2, kdb.LayerInfo(30, 0, "Poly2"))
    width = round(0.06 / dbu_um)
    length = round(2.0 / dbu_um)
    top.shapes(poly2).insert(kdb.Box(0, 0, width, length))
    return layout


def _assert_reports_match_modulo_dbu(
    reference: dict, dbu_um: float, other: dict
) -> None:
    """Assert `other` (from a layout written at `dbu_um`) reports the exact
    same violations as `reference` (from the deck's nominal-dbu layout),
    once bounding boxes are converted from database units back to physical
    micrometres."""
    assert other["status"] == reference["status"]
    assert other["violation_count"] == reference["violation_count"]
    assert other["rule_counts"] == reference["rule_counts"]
    assert len(other["violations"]) == len(reference["violations"])

    for expected, actual in zip(
        reference["violations"], other["violations"], strict=True
    ):
        assert actual["rule"] == expected["rule"]
        assert actual["description"] == expected["description"]
        assert actual["check"] == expected["check"]
        assert actual["layer"] == expected["layer"]
        assert actual["cell"] == expected["cell"]

        expected_bbox_um = {k: v * 0.001 for k, v in expected["bbox"].items()}
        actual_bbox_um = {k: v * dbu_um for k, v in actual["bbox"].items()}
        assert actual_bbox_um == expected_bbox_um


@pytest.mark.parametrize("dbu_um", _DBU_INVARIANCE_DBU_VALUES)
def test_run_drc_sky130_dbu_invariant(tmp_path, dbu_um):
    """`poly.width.1` (sky130) reports the identical violation for the same
    physical geometry whether the stream's `dbu_um` is 0.001 (the deck's
    nominal dbu), 0.005, or 0.0005 (#172's own repro table)."""
    reference_path = tmp_path / "reference.gds"
    _make_sky130_violation_layout_at_dbu(0.001).write(str(reference_path))
    reference = run_drc(str(reference_path), "sky130")
    assert reference["status"] == "violations"
    assert reference["violation_count"] == 1

    path = tmp_path / f"dbu_{dbu_um}.gds"
    _make_sky130_violation_layout_at_dbu(dbu_um).write(str(path))
    report = run_drc(str(path), "sky130")

    assert report["dbu_um"] == dbu_um
    _assert_reports_match_modulo_dbu(reference, dbu_um, report)


@pytest.mark.parametrize("dbu_um", _DBU_INVARIANCE_DBU_VALUES)
def test_run_drc_gf180mcu_dbu_invariant(tmp_path, dbu_um):
    """`poly2.width.1` (gf180mcu) reports the identical violation for the
    same physical geometry whether the stream's `dbu_um` is 0.001 (the
    deck's nominal dbu), 0.005, or 0.0005 (#172's own repro table)."""
    reference_path = tmp_path / "reference.gds"
    _make_gf180mcu_violation_layout_at_dbu(0.001).write(str(reference_path))
    reference = run_drc(str(reference_path), "gf180mcu")
    assert reference["status"] == "violations"
    assert reference["violation_count"] == 1

    path = tmp_path / f"dbu_{dbu_um}.gds"
    _make_gf180mcu_violation_layout_at_dbu(dbu_um).write(str(path))
    report = run_drc(str(path), "gf180mcu")

    assert report["dbu_um"] == dbu_um
    _assert_reports_match_modulo_dbu(reference, dbu_um, report)
