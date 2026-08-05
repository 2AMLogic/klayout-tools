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
from klayout_tools.decks import get_deck, get_extraction_deck
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
    # Top-level geometry sits inside no placed instance, so per-instance
    # attribution is (correctly) null and `cell` remains the only origin.
    assert violation["source_cell"] is None
    assert violation["source_path"] is None
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
# per-instance attribution: source_cell / source_path (#451)
# ---------------------------------------------------------------------------


def _make_hierarchical_violation_layout() -> kdb.Layout:
    """A hierarchical, multi-instance layout mirroring machine-generated
    standard-cell placement (e.g. an OpenROAD P&R run).

    A single ``sky130_fd_sc_hd__inv_2``-named leaf cell carries one seeded
    ``poly.width.1`` violation (a 50 dbu poly bar < the 150 dbu threshold).
    It is placed twice directly under the top cell (two occurrences of the
    *same* definition, so attribution must not conflate them) and once more,
    one level deeper, inside a ``block_a`` sub-block (so ``source_path`` has
    depth 2). A clean ``sky130_fd_sc_hd__buf_1`` cell is also placed to prove
    a violation-free instance is never attributed to.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))

    inv = layout.create_cell("sky130_fd_sc_hd__inv_2")
    inv.shapes(poly).insert(kdb.Box(0, 0, 50, 2000))  # 50 dbu < 150 -> violation

    buf = layout.create_cell("sky130_fd_sc_hd__buf_1")
    buf.shapes(poly).insert(kdb.Box(0, 0, 200, 2000))  # 200 dbu -> clean

    block = layout.create_cell("block_a")
    block.insert(kdb.CellInstArray(inv.cell_index(), kdb.Trans(kdb.Vector(0, 0))))

    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(inv.cell_index(), kdb.Trans(kdb.Vector(0, 0))))
    top.insert(kdb.CellInstArray(inv.cell_index(), kdb.Trans(kdb.Vector(5000, 0))))
    top.insert(kdb.CellInstArray(buf.cell_index(), kdb.Trans(kdb.Vector(10000, 0))))
    top.insert(kdb.CellInstArray(block.cell_index(), kdb.Trans(kdb.Vector(0, 5000))))
    return layout


def test_run_drc_attributes_violation_to_placed_instance(tmp_path):
    """Violations inside placed instances name the originating leaf cell in
    ``source_cell`` (not just the top cell in ``cell``), and ``source_path``
    records the full instance chain -- including a two-deep path for the
    nested block placement."""
    path = tmp_path / "hier.gds"
    _make_hierarchical_violation_layout().write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    # Three placements of the violating cell (two direct, one nested); the
    # clean buf placement contributes nothing.
    assert report["violation_count"] == 3
    assert report["rule_counts"] == {"poly.width.1": 3}

    for v in report["violations"]:
        # `cell` (the flattened top-cell attribution) is unchanged/back-compat.
        assert v["cell"] == "TOP"
        # ...but every violation now also names its originating leaf instance.
        assert v["source_cell"] == "sky130_fd_sc_hd__inv_2"

    # The nested placement carries a two-deep path; the two direct placements
    # carry a one-deep path -- and the two direct occurrences of the *same*
    # definition are not conflated (distinct bboxes at their own coordinates).
    by_bbox = {
        (v["bbox"]["left"], v["bbox"]["bottom"]): v for v in report["violations"]
    }
    assert by_bbox[(0, 0)]["source_path"] == ["sky130_fd_sc_hd__inv_2"]
    assert by_bbox[(5000, 0)]["source_path"] == ["sky130_fd_sc_hd__inv_2"]
    assert by_bbox[(0, 5000)]["source_path"] == ["block_a", "sky130_fd_sc_hd__inv_2"]


def test_run_drc_attribution_fields_deterministic(tmp_path):
    """The attribution fields are stable across repeated runs (no reliance on
    KLayout's internal instance-enumeration order)."""
    path = tmp_path / "hier.gds"
    _make_hierarchical_violation_layout().write(str(path))

    assert run_drc(str(path), "sky130") == run_drc(str(path), "sky130")


def test_attribute_to_instance_straddling_and_top_level_are_null():
    """`_attribute_to_instance` returns ``(None, None)`` when a violation bbox
    is contained in no single placement -- top-level geometry, or a bbox that
    straddles two adjacent instances (a violation *between* placements belongs
    to neither)."""
    from klayout_tools.drc import _attribute_to_instance

    layout = kdb.Layout()
    layout.dbu = 0.001
    li = layout.layer(66, 20)
    child = layout.create_cell("child")
    child.shapes(li).insert(kdb.Box(0, 0, 300, 300))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(kdb.Vector(0, 0))))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(kdb.Vector(400, 0))))

    # Fully inside the first placement -> attributed to `child`.
    assert _attribute_to_instance(top, kdb.Box(10, 10, 50, 50)) == (
        "child",
        ["child"],
    )
    # Straddles the boundary between both placements -> null.
    assert _attribute_to_instance(top, kdb.Box(250, 10, 450, 50)) == (None, None)
    # Top-level empty region, no placement nearby -> null.
    assert _attribute_to_instance(top, kdb.Box(1000, 1000, 1050, 1050)) == (
        None,
        None,
    )


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
        "provenance",
    }
    assert data["schema_version"] == 1

    prov = data["provenance"]
    assert set(prov.keys()) == {
        "klt_version",
        "klayout_version",
        "pdk",
        "deck",
        "input",
    }
    assert isinstance(prov["klt_version"], str)
    # klt drc resolves no PDK.
    assert prov["pdk"] is None
    assert prov["deck"]["name"] == "sky130"
    assert prov["deck"]["content_hash"].startswith("sha256:")
    # Issue #331: the input layout stream is now hashed too, distinct from
    # the deck's own content hash above.
    assert prov["input"]["content_hash"].startswith("sha256:")
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
            "source_cell",
            "source_path",
            "bbox",
            "polygon",
        }
        assert isinstance(entry["rule"], str)
        assert isinstance(entry["description"], str)
        assert isinstance(entry["check"], str)
        assert isinstance(entry["layer"], str)
        assert isinstance(entry["cell"], str)
        assert entry["source_cell"] is None or isinstance(entry["source_cell"], str)
        assert entry["source_path"] is None or (
            isinstance(entry["source_path"], list)
            and all(isinstance(v, str) for v in entry["source_path"])
        )
        # `source_cell` and `source_path` are null together or set together.
        assert (entry["source_cell"] is None) == (entry["source_path"] is None)
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


def test_provenance_input_hash_tracks_layout_bytes(tmp_path):
    """Issue #331: `provenance.input.content_hash` identifies the *stream* a
    report was produced from -- two byte-identical layouts hash the same,
    and a real geometry change (which a stale committed report would
    otherwise not reveal) produces a different hash."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(64, 20)).insert(kdb.Box(0, 0, 1000, 1000))

    path_a = tmp_path / "a.gds"
    path_b = tmp_path / "b.gds"
    layout.write(str(path_a))
    layout.write(str(path_b))

    report_a = run_drc(str(path_a), "sky130")
    report_b = run_drc(str(path_b), "sky130")
    assert (
        report_a["provenance"]["input"]["content_hash"]
        == report_b["provenance"]["input"]["content_hash"]
    )

    # Now mutate one file's geometry and re-run: the hash must change even
    # though the deck/version fields do not.
    top.shapes(layout.layer(64, 20)).insert(kdb.Box(2000, 2000, 3000, 3000))
    layout.write(str(path_b))
    report_b_modified = run_drc(str(path_b), "sky130")

    assert (
        report_b_modified["provenance"]["input"]["content_hash"]
        != report_a["provenance"]["input"]["content_hash"]
    )
    assert (
        report_b_modified["provenance"]["klt_version"]
        == report_a["provenance"]["klt_version"]
    )


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
            "source_cell",
            "source_path",
            "bbox",
            "polygon",
        }


# --------------------------------------------------------------------------- #
# OpenROAD-produced, macro-scale standard-cell fixture (issue #436)
# --------------------------------------------------------------------------- #

# A real sky130_fd_sc_hd GCD macro produced end to end by `klt synthesize` +
# `klt place-and-route` (real Yosys + real OpenROAD) -- the first exercise of
# `klt drc` against machine-generated, macro-scale layout rather than the
# hand-drawn analog fixtures above. See
# tests/corpus/place_and_route/README.md (in tests/corpus/README.md's
# "Machine-generated macro-scale fixture" section) for full provenance.
PLACE_AND_ROUTE_GDS = CORPUS_DIR / "place_and_route" / "gcd.gds.gz"


@pytest.mark.skipif(
    not PLACE_AND_ROUTE_GDS.is_file(),
    reason="no OpenROAD-produced place-and-route corpus fixture checked in",
)
def test_openroad_gcd_fixture_produces_well_formed_report():
    """`klt drc --deck sky130` against the real, OpenROAD-produced GCD
    macro-scale fixture required no verb-side fix (#436): it runs cleanly
    (no crash) against thousands of instances, one level of real hierarchy,
    and routing-layer usage the hand-drawn analog corpus never exercises."""
    report = run_drc(str(PLACE_AND_ROUTE_GDS), "sky130")

    assert report["schema_version"] == 1
    assert report["file"] == str(PLACE_AND_ROUTE_GDS)
    assert report["deck"] == "sky130"
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
            "source_cell",
            "source_path",
            "bbox",
            "polygon",
        }

    # Macro-scale sanity: real routing-layer coverage the hand-drawn analog
    # corpus above never exercises (a handful of drawn layers vs. multiple
    # metal/via layers here).
    assert len(report["coverage"]["layers_checked"]) >= 5

    # Regression pin against this specific, static, committed fixture (see
    # docs/cli/drc.md's "Macro-scale, machine-generated (standard-cell)
    # layout" section for why a handful of real diff.enclosing.licon.1
    # violations at standard-cell row boundaries is an expected, understood
    # property of this input -- not a `klt drc` defect).
    assert report["violation_count"] == 4
    assert report["rule_counts"] == {"diff.enclosing.licon.1": 4}

    # Per-instance attribution (#451), verified against a *real* placed macro:
    # each violation's flattened `cell` is the top cell, but `source_cell`
    # names the originating standard cell -- here all four are inside a placed
    # `sky130_fd_sc_hd__and3_1` instance, not merely "somewhere under gcd".
    for entry in report["violations"]:
        assert entry["cell"] == "gcd"
        assert entry["source_cell"] == "sky130_fd_sc_hd__and3_1"
        assert entry["source_path"] == ["sky130_fd_sc_hd__and3_1"]


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


# --- via1.width.1/via1.space.1 .. via4.width.1/via4.space.1 (#546) -------
#
# DRM 7.14 Vian, "Vn.1" (min/max size, 0.26um / 260 dbu) and "Vn.2a"
# (ordinary space, 0.26um / 260 dbu) for each of Via1 (35/0), Via2 (38/0),
# Via3 (40/0), Via4 (41/0). Before #546, none of these four layers had any
# rule constraining their own size/spacing -- Via4 only ever appeared as the
# `other_layer` of `mim.enclosing.via4.1` (the MiM cap's virtual-bottom-plate
# overlap rule), and Via1-Via3 had no reference in this deck at all despite
# being part of `EXTRACTION_DECK.vias`.
#
# Mirrors the metal2/metal3/metal5 (#188) violation+clean test pattern
# above: an isolated bar narrower than the threshold trips `*.width.1`, two
# bars closer than the threshold trip `*.space.1`, and a bar/pair at or above
# the threshold is clean. `Vn.1`'s max-size half (a fixed 0.26 x 0.26um
# square) is *not* exercised here -- see
# `test_run_drc_gf180mcu_via4_reproducer_from_issue` below, which documents
# that known, pre-existing gap (the same one `contact.width.1`/`CO.1`
# already carries) rather than silently assuming it away.

_VIA_LAYER_RULES = [
    ("via1", (35, 0), "Via1"),
    ("via2", (38, 0), "Via2"),
    ("via3", (40, 0), "Via3"),
    ("via4", (41, 0), "Via4"),
]


@pytest.mark.parametrize("rule_prefix,layer,layer_name", _VIA_LAYER_RULES)
def test_run_drc_gf180mcu_via_width_violation(tmp_path, rule_prefix, layer, layer_name):
    """A via bar narrower than the 260 dbu (0.26 um) `via{n}.width.1`
    threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via = layout.layer(*layer)
    layout.set_info(via, kdb.LayerInfo(layer[0], layer[1], layer_name))
    top.shapes(via).insert(kdb.Box(0, 0, 100, 2000))  # 100 dbu < 260
    path = tmp_path / f"{rule_prefix}_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    rule_id = f"{rule_prefix}.width.1"
    assert report["status"] == "violations"
    assert report["rule_counts"] == {rule_id: 1}
    (violation,) = report["violations"]
    assert violation["rule"] == rule_id
    assert violation["check"] == "width"
    assert violation["layer"] == layer_name


@pytest.mark.parametrize("rule_prefix,layer,layer_name", _VIA_LAYER_RULES)
def test_run_drc_gf180mcu_via_space_violation(tmp_path, rule_prefix, layer, layer_name):
    """Two via bars closer than the 260 dbu `via{n}.space.1` threshold trip
    exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via = layout.layer(*layer)
    layout.set_info(via, kdb.LayerInfo(layer[0], layer[1], layer_name))
    top.shapes(via).insert(kdb.Box(0, 0, 2000, 4000))
    top.shapes(via).insert(kdb.Box(2100, 0, 4000, 4000))  # 100 dbu gap < 260
    path = tmp_path / f"{rule_prefix}_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    rule_id = f"{rule_prefix}.space.1"
    assert report["status"] == "violations"
    assert report["rule_counts"] == {rule_id: 1}
    (violation,) = report["violations"]
    assert violation["rule"] == rule_id
    assert violation["check"] == "space"
    assert violation["layer"] == layer_name


@pytest.mark.parametrize("rule_prefix,layer,layer_name", _VIA_LAYER_RULES)
def test_run_drc_gf180mcu_via_clean(tmp_path, rule_prefix, layer, layer_name):
    """A via bar wide enough (and, in isolation, far enough from any other
    shape) to satisfy `via{n}.width.1`/`via{n}.space.1` passes."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via = layout.layer(*layer)
    layout.set_info(via, kdb.LayerInfo(layer[0], layer[1], layer_name))
    top.shapes(via).insert(kdb.Box(0, 0, 300, 2000))  # 300 >= 260
    path = tmp_path / f"{rule_prefix}_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_gf180mcu_via4_reproducer_from_issue(tmp_path):
    """The issue's own reproducer (#546): two abutting 780 dbu (0.78 um)
    Via4 cuts, each 3x the DRM's 260 dbu (0.26 um) `Vn.1` min/max size.

    Because the two cuts are drawn touching (zero gap, per the issue's own
    ``[1,1,1.78,1.78]`` / ``[1.78,1,2.56,1.78]`` geometry), `Region`'s default
    merged semantics (used throughout this engine, see `_run_check` /
    `run_drc`) merges them into one plain 1560 x 780 dbu rectangle before any
    check runs -- so neither `via4.width.1` (each local width is still >=
    260 dbu) nor `via4.space.1` (no facing edge pair remains once merged)
    can flag it. Catching this specific case requires enforcing `Vn.1`'s
    *max*-size half, which -- like `contact.width.1`/`CO.1` before it -- our
    `width_check` primitive structurally cannot do (it is a minimum-width
    lower bound only); see Via1-4's rule comments and the module docstring's
    "Sixteen rules...approximate" note in `gf180mcu.py` for the same,
    already-documented gap. This test pins that honestly: it is *not* a
    regression to fix here, and a real max-size-check primitive is tracked
    as a follow-up (see this issue's own PR description) rather than
    silently assumed away.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("via_bad")
    metal4 = layout.layer(46, 0)
    layout.set_info(metal4, kdb.LayerInfo(46, 0, "Metal4"))
    metal5 = layout.layer(81, 0)
    layout.set_info(metal5, kdb.LayerInfo(81, 0, "Metal5"))
    via4 = layout.layer(41, 0)
    layout.set_info(via4, kdb.LayerInfo(41, 0, "Via4"))
    top.shapes(metal4).insert(kdb.Box(0, 0, 10000, 10000))
    top.shapes(metal5).insert(kdb.Box(0, 0, 10000, 10000))
    top.shapes(via4).insert(kdb.Box(1000, 1000, 1780, 1780))
    top.shapes(via4).insert(kdb.Box(1780, 1000, 2560, 1780))
    path = tmp_path / "via_bad.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    # Documents current, known behaviour -- see the docstring above.
    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_gf180mcu_via_layers_now_covered_by_own_rules(tmp_path):
    """The issue's own coverage acceptance criterion (#546): a stream
    drawing genuine Via1-4 geometry shows all four via layers in
    `coverage.layers_checked`, backed by each via's own `via{n}.width.1`/
    `via{n}.space.1` rules -- not solely by `mim.enclosing.via4.1` (which
    only ever referenced Via4 as a foreign-layer enclosure target, never
    Via1-Via3, and never constrained any via's own size/spacing)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for layer, name in [
        ((35, 0), "Via1"),
        ((38, 0), "Via2"),
        ((40, 0), "Via3"),
        ((41, 0), "Via4"),
    ]:
        li = layout.layer(*layer)
        layout.set_info(li, kdb.LayerInfo(layer[0], layer[1], name))
        top.shapes(li).insert(kdb.Box(0, 0, 300, 300))  # 300 >= 260, isolated
    path = tmp_path / "vias_covered.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    for layer_str in ("35/0", "38/0", "40/0", "41/0"):
        assert layer_str in report["coverage"]["layers_checked"]
        assert layer_str not in report["coverage"]["layers_in_stream_without_rules"]
    for rule_id in (
        "via1.width.1",
        "via1.space.1",
        "via2.width.1",
        "via2.space.1",
        "via3.width.1",
        "via3.space.1",
        "via4.width.1",
        "via4.space.1",
    ):
        assert rule_id not in report["coverage"]["rules_skipped"]


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


# --- mim.enclosing.via4.1 (MIMTM.2) (#345) --------------------------------
#
# MIMTM.2 ("min. MiM bottom-plate overlap of Via4", 0.4um / 400 dbu) is
# scoped to the MiM stack's "virtual bottom plate" -- FuseTop sized/oversized
# by 1.06um (1060 dbu), restricted to wherever Metal4 already overlaps the
# *unsized* FuseTop -- via `DerivedLayer`, not raw Metal4. For a FuseTop
# shape at (2000, 2000)-(18000, 18000) sitting inside a Metal4 shape at
# (0, 0)-(20000, 20000), the derived virtual bottom plate is
# (940, 940)-(19060, 19060) (FuseTop's box each expanded by 1060 dbu).


def test_run_drc_gf180mcu_mim_enclosing_via4_violation(tmp_path):
    """A Via4 shape whose right edge sits exactly on the virtual bottom
    plate's own right edge (19060, i.e. 0 dbu margin, well under the 400 dbu
    `mim.enclosing.via4.1` threshold) trips exactly one violation."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal4 = layout.layer(46, 0)
    layout.set_info(metal4, kdb.LayerInfo(46, 0, "Metal4"))
    fusetop = layout.layer(75, 0)
    layout.set_info(fusetop, kdb.LayerInfo(75, 0, "FuseTop"))
    via4 = layout.layer(41, 0)
    layout.set_info(via4, kdb.LayerInfo(41, 0, "Via4"))
    top.shapes(metal4).insert(kdb.Box(0, 0, 20000, 20000))
    top.shapes(fusetop).insert(kdb.Box(2000, 2000, 18000, 18000))
    # Right edge at 19060 == the virtual bottom plate's own right edge.
    top.shapes(via4).insert(kdb.Box(18900, 9900, 19060, 10100))
    path = tmp_path / "mim_enclosing_via4_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"]["mim.enclosing.via4.1"] == 1
    (violation,) = [
        v for v in report["violations"] if v["rule"] == "mim.enclosing.via4.1"
    ]
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "Metal4"


def test_run_drc_gf180mcu_mim_enclosing_via4_clean(tmp_path):
    """A Via4 shape centred well inside the virtual bottom plate (>= 400 dbu
    margin on every side) passes. Sized 300x300 dbu (>= the 260 dbu
    `via4.width.1` threshold added by #546, so this stays a clean fixture for
    `mim.enclosing.via4.1` specifically rather than incidentally tripping the
    via's own min-size rule too)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal4 = layout.layer(46, 0)
    layout.set_info(metal4, kdb.LayerInfo(46, 0, "Metal4"))
    fusetop = layout.layer(75, 0)
    layout.set_info(fusetop, kdb.LayerInfo(75, 0, "FuseTop"))
    via4 = layout.layer(41, 0)
    layout.set_info(via4, kdb.LayerInfo(41, 0, "Via4"))
    top.shapes(metal4).insert(kdb.Box(0, 0, 20000, 20000))
    top.shapes(fusetop).insert(kdb.Box(2000, 2000, 18000, 18000))
    top.shapes(via4).insert(kdb.Box(9850, 9850, 10150, 10150))  # centred, 300x300
    path = tmp_path / "mim_enclosing_via4_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_gf180mcu_mim_enclosing_via4_ordinary_routing_clean(tmp_path):
    """Negative control (issue #345's own acceptance criterion): a genuine,
    cleanly-enclosed MiM cap coexists with ordinary Metal4/Via4/Metal5
    routing *elsewhere* on the same layout -- a routing via whose Metal4 pad
    it lands on has no FuseTop anywhere nearby, with only a 50 dbu margin
    (far under the 400 dbu MIMTM.2 threshold, typical of ordinary routing-via
    enclosure, not a MiM cap's). Because the virtual bottom plate is scoped
    to Metal4 that already overlaps a FuseTop shape, the routing via's Metal4
    pad never becomes part of the derived region, so this must **not**
    report `mim.enclosing.via4.1` for it -- confirming the fix does not
    reintroduce the "flags all Metal4/Via4/Metal5 routing" false-positive
    risk the module docstring warns an unscoped version of this rule would
    cause."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal4 = layout.layer(46, 0)
    layout.set_info(metal4, kdb.LayerInfo(46, 0, "Metal4"))
    fusetop = layout.layer(75, 0)
    layout.set_info(fusetop, kdb.LayerInfo(75, 0, "FuseTop"))
    via4 = layout.layer(41, 0)
    layout.set_info(via4, kdb.LayerInfo(41, 0, "Via4"))
    metal5 = layout.layer(81, 0)
    layout.set_info(metal5, kdb.LayerInfo(81, 0, "Metal5"))

    # Genuine MiM cap, cleanly enclosed (same geometry as the clean case above).
    top.shapes(metal4).insert(kdb.Box(0, 0, 20000, 20000))
    top.shapes(fusetop).insert(kdb.Box(2000, 2000, 18000, 18000))
    top.shapes(via4).insert(kdb.Box(9900, 9900, 10100, 10100))

    # Ordinary routing, far from any FuseTop: a small Metal4 landing pad with
    # a Via4 up to Metal5, at typical routing-via clearance (50 dbu) -- no
    # MiM structure anywhere nearby.
    top.shapes(metal4).insert(kdb.Box(100000, 100000, 100300, 100300))
    top.shapes(via4).insert(kdb.Box(100050, 100050, 100250, 100250))
    top.shapes(metal5).insert(kdb.Box(100000, 100000, 100300, 100300))

    path = tmp_path / "mim_enclosing_via4_ordinary_routing.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert "mim.enclosing.via4.1" not in report["coverage"]["rules_skipped"]
    assert report["rule_counts"].get("mim.enclosing.via4.1", 0) == 0


# --- #318: enclosing_check/enclosed_check silently pass on zero overlap --


def test_run_drc_gf180mcu_mim_enclosing_fusetop_outside_tab(tmp_path):
    """Issue #318's exact reproducer: a legal 0.6 um `mim.enclosing.fusetop.1`
    enclosure everywhere *except* a small FuseTop tab that pokes entirely
    outside Metal4 (0.8 um past its right edge, touching -- not overlapping --
    the legally-enclosed body). `Region.enclosing_check` alone only measures
    facing edges of shapes already within striking distance of each other, so
    it reports nothing for the tab; the fix must additionally flag the part
    of FuseTop that has escaped Metal4 entirely, under the same rule id."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal4 = layout.layer(46, 0)
    layout.set_info(metal4, kdb.LayerInfo(46, 0, "Metal4"))
    fusetop = layout.layer(75, 0)
    layout.set_info(fusetop, kdb.LayerInfo(75, 0, "FuseTop"))
    top.shapes(metal4).insert(kdb.Box(0, 0, 20000, 20000))
    # Legal 600 dbu (0.6 um) margin on every side of the main body...
    top.shapes(fusetop).insert(kdb.Box(600, 600, 19400, 19400))
    # ...plus a tab that touches the main body's right edge (so it merges
    # into one connected FuseTop shape) but sticks 800 dbu past Metal4's own
    # right edge (20000) with zero overlap over that stretch.
    top.shapes(fusetop).insert(kdb.Box(19400, 9000, 20800, 10000))
    path = tmp_path / "mim_enclosing_outside_tab.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"]["mim.enclosing.fusetop.1"] >= 1
    outside_violations = [
        v
        for v in report["violations"]
        if v["rule"] == "mim.enclosing.fusetop.1"
        and v["bbox"]["right"] > 20000  # only the escaped tab pokes past x=20000
    ]
    assert len(outside_violations) == 1
    (violation,) = outside_violations
    assert violation["check"] == "enclosing"
    assert violation["bbox"] == {
        "left": 20000,
        "bottom": 9000,
        "right": 20800,
        "top": 10000,
    }
    assert violation["polygon"] is not None


def test_run_drc_gf180mcu_mim_enclosing_fusetop_marginal_plus_outside(tmp_path):
    """A FuseTop shape that both under-encloses on one edge (a marginal
    `enclosing_check` edge-pair violation) *and* has a separate tab entirely
    outside Metal4 on another edge reports two distinct violations for the
    same rule id -- the pre-existing marginal-distance detection and the new
    zero-overlap detection are additive, not a replacement, and don't
    double-count the same geometry."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal4 = layout.layer(46, 0)
    layout.set_info(metal4, kdb.LayerInfo(46, 0, "Metal4"))
    fusetop = layout.layer(75, 0)
    layout.set_info(fusetop, kdb.LayerInfo(75, 0, "FuseTop"))
    top.shapes(metal4).insert(kdb.Box(0, 0, 20000, 20000))
    # Left margin is only 100 dbu (< 600 threshold) -> one edge-pair violation.
    # Right/top/bottom margins are a legal 600 dbu.
    top.shapes(fusetop).insert(kdb.Box(100, 600, 19400, 19400))
    # A second, separate tab poking entirely outside Metal4's right edge.
    top.shapes(fusetop).insert(kdb.Box(19400, 9000, 20800, 10000))
    path = tmp_path / "mim_enclosing_marginal_plus_outside.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    fusetop_violations = [
        v for v in report["violations"] if v["rule"] == "mim.enclosing.fusetop.1"
    ]
    # One marginal edge-pair violation (left edge) + one outside-area
    # violation (the tab) -- not merged, not duplicated.
    assert report["rule_counts"]["mim.enclosing.fusetop.1"] == len(fusetop_violations)
    assert len(fusetop_violations) == 2
    right_edges = sorted(v["bbox"]["right"] for v in fusetop_violations)
    # The marginal violation's edge pair is bounded within Metal4 (right edge
    # <= 20000); the outside violation's bbox extends past it (20800).
    assert right_edges[0] <= 20000
    assert right_edges[1] == 20800


def test_run_drc_synthetic_enclosed_check_flags_zero_overlap(tmp_path, monkeypatch):
    """Symmetric coverage for `check="enclosed"` (#318): neither shipped deck
    currently has an `"enclosed"` rule (only `"enclosing"`), so this exercises
    the dispatch directly against a minimal synthetic one-rule deck via
    monkeypatch, mirroring the `"enclosing"` reproducer above but with the
    enclosed/enclosing roles swapped (`layer` is the enclosed side here,
    `other_layer` the enclosing side)."""
    from klayout_tools.decks import DrcRule

    synthetic_deck = [
        DrcRule(
            id="inner.enclosed.outer.1",
            description="synthetic: inner must be enclosed by outer by >= 500 dbu",
            layer=(10, 0),  # inner (the enclosed layer)
            other_layer=(20, 0),  # outer (the enclosing layer)
            check="enclosed",
            threshold_dbu=500,
        )
    ]
    monkeypatch.setattr("klayout_tools.drc.get_deck", lambda name: synthetic_deck)
    monkeypatch.setattr("klayout_tools.drc.get_nominal_dbu", lambda name: 0.001)
    monkeypatch.setattr("klayout_tools.drc.get_layer_names", lambda name: {})

    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    outer = layout.layer(20, 0)
    inner = layout.layer(10, 0)
    top.shapes(outer).insert(kdb.Box(0, 0, 20000, 20000))
    # Legally-enclosed main body...
    top.shapes(inner).insert(kdb.Box(600, 600, 19400, 19400))
    # ...plus a tab entirely outside `outer`, touching the main body.
    top.shapes(inner).insert(kdb.Box(19400, 9000, 20800, 10000))
    path = tmp_path / "synthetic_enclosed_outside_tab.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert report["status"] == "violations"
    assert report["rule_counts"]["inner.enclosed.outer.1"] >= 1
    (outside_violation,) = [
        v for v in report["violations"] if v["bbox"]["right"] > 20000
    ]
    assert outside_violation["check"] == "enclosed"
    assert outside_violation["bbox"] == {
        "left": 20000,
        "bottom": 9000,
        "right": 20800,
        "top": 10000,
    }


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

    `run_drc`'s output is deterministic apart from the shared `provenance`
    block, whose `klt_version`/`klayout_version` are environment-dependent by
    design -- so that additive block is stripped before comparison and the
    committed fixture stays free of environment-dependent fields (its shape is
    exercised by `test_json_contract` instead). If this fails, either the DRC
    output shape or the sky130 deck rules changed; regenerate the fixture per
    the header of `examples/drc/generate.py`.
    """
    expected = json.loads(EXAMPLE_DRC_JSON.read_text())
    actual = run_drc(EXAMPLE_GDS, "sky130")

    provenance = actual.pop("provenance")
    assert provenance["deck"]["name"] == "sky130"
    assert provenance["deck"]["content_hash"].startswith("sha256:")
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


# ---------------------------------------------------------------------------
# sky130 met2 / via1 rule coverage (#513)
#
# #511 extended `EXTRACTION_DECK.metals`/`.vias` to a third connectivity
# level (met2.drawing 69/20, via.drawing 68/44 as the met1<->met2 via) but
# left `DECK` with no rules on either layer -- these tests exercise the six
# rules added to close that gap: `met2.width.1`, `met2.space.1`,
# `via.width.1`, `via.space.1`, `met1.enclosing.via.1`,
# `met2.enclosing.via.1`.
# ---------------------------------------------------------------------------


def test_run_drc_sky130_met2_width_violation(tmp_path):
    """A met2 bar narrower than the 140 dbu (0.14 um) `met2.width.1`
    threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met2 = layout.layer(69, 20)
    layout.set_info(met2, kdb.LayerInfo(69, 20, "met2.drawing"))
    top.shapes(met2).insert(kdb.Box(0, 0, 100, 2000))  # 100 dbu < 140
    path = tmp_path / "met2_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met2.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met2.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "met2.drawing"


def test_run_drc_sky130_met2_space_violation(tmp_path):
    """Two met2 bars closer than the 140 dbu `met2.space.1` threshold trip
    exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met2 = layout.layer(69, 20)
    layout.set_info(met2, kdb.LayerInfo(69, 20, "met2.drawing"))
    top.shapes(met2).insert(kdb.Box(0, 0, 2000, 4000))
    top.shapes(met2).insert(kdb.Box(2100, 0, 4000, 4000))  # 100 dbu gap < 140
    path = tmp_path / "met2_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met2.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met2.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "met2.drawing"


def test_run_drc_sky130_met2_clean(tmp_path):
    """A met2 bar wide enough to satisfy `met2.width.1` passes."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met2 = layout.layer(69, 20)
    layout.set_info(met2, kdb.LayerInfo(69, 20, "met2.drawing"))
    top.shapes(met2).insert(kdb.Box(0, 0, 200, 2000))  # 200 >= 140
    path = tmp_path / "met2_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sky130_via_width_violation(tmp_path):
    """A via1 (`via.drawing`, 68/44) shape narrower than the 150 dbu
    (0.15 um) `via.width.1` threshold trips exactly one violation.

    Elongated (not square), mirroring `_make_violation_layout`'s own note:
    an elongated shape's `width_check` reports one edge pair per narrow run,
    while a square shape reports one pair per violating edge direction (two,
    for a uniformly-undersized square)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via = layout.layer(68, 44)
    layout.set_info(via, kdb.LayerInfo(68, 44, "via.drawing"))
    top.shapes(via).insert(kdb.Box(0, 0, 100, 2000))  # 100 dbu < 150
    path = tmp_path / "via_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"via.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "via.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "via.drawing"


def test_run_drc_sky130_via_width_clean(tmp_path):
    """A via1 shape at least 150 dbu wide passes `via.width.1`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via = layout.layer(68, 44)
    layout.set_info(via, kdb.LayerInfo(68, 44, "via.drawing"))
    top.shapes(via).insert(kdb.Box(0, 0, 150, 150))  # 150 >= 150
    path = tmp_path / "via_width_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sky130_via_space_violation(tmp_path):
    """Two via1 shapes closer than the 170 dbu (0.17 um) `via.space.1`
    threshold trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via = layout.layer(68, 44)
    layout.set_info(via, kdb.LayerInfo(68, 44, "via.drawing"))
    top.shapes(via).insert(kdb.Box(0, 0, 150, 150))
    top.shapes(via).insert(kdb.Box(250, 0, 400, 150))  # 100 dbu gap < 170
    path = tmp_path / "via_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"via.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "via.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "via.drawing"


def test_run_drc_sky130_met1_enclosing_via_violation(tmp_path):
    """A via1 shape hanging off the edge of its met1 landing pad by less
    than the 55 dbu (0.055 um) `met1.enclosing.via.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met1 = layout.layer(68, 20)
    layout.set_info(met1, kdb.LayerInfo(68, 20, "met1.drawing"))
    via = layout.layer(68, 44)
    layout.set_info(via, kdb.LayerInfo(68, 44, "via.drawing"))
    top.shapes(met1).insert(kdb.Box(0, 0, 1000, 1000))
    # 400 dbu margin on 3 sides, only 10 dbu (< 55) margin on the right.
    top.shapes(via).insert(kdb.Box(400, 400, 990, 600))
    path = tmp_path / "met1_enclosing_via_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met1.enclosing.via.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met1.enclosing.via.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "met1.drawing"


def test_run_drc_sky130_met1_enclosing_via_clean(tmp_path):
    """A via1 shape enclosed by its met1 landing pad with >= 55 dbu margin
    on every side passes `met1.enclosing.via.1`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met1 = layout.layer(68, 20)
    layout.set_info(met1, kdb.LayerInfo(68, 20, "met1.drawing"))
    via = layout.layer(68, 44)
    layout.set_info(via, kdb.LayerInfo(68, 44, "via.drawing"))
    top.shapes(met1).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(via).insert(kdb.Box(400, 400, 600, 600))  # 400 margin >= 55
    path = tmp_path / "met1_enclosing_via_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sky130_met2_enclosing_via_violation(tmp_path):
    """A via1 shape hanging off the edge of its met2 landing pad by less
    than the 55 dbu (0.055 um) `met2.enclosing.via.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met2 = layout.layer(69, 20)
    layout.set_info(met2, kdb.LayerInfo(69, 20, "met2.drawing"))
    via = layout.layer(68, 44)
    layout.set_info(via, kdb.LayerInfo(68, 44, "via.drawing"))
    top.shapes(met2).insert(kdb.Box(0, 0, 1000, 1000))
    # 400 dbu margin on 3 sides, only 10 dbu (< 55) margin on the right.
    top.shapes(via).insert(kdb.Box(400, 400, 990, 600))
    path = tmp_path / "met2_enclosing_via_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met2.enclosing.via.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met2.enclosing.via.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "met2.drawing"


def test_run_drc_sky130_met2_enclosing_via_clean(tmp_path):
    """A via1 shape enclosed by its met2 landing pad with >= 55 dbu margin
    on every side passes `met2.enclosing.via.1`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met2 = layout.layer(69, 20)
    layout.set_info(met2, kdb.LayerInfo(69, 20, "met2.drawing"))
    via = layout.layer(68, 44)
    layout.set_info(via, kdb.LayerInfo(68, 44, "via.drawing"))
    top.shapes(met2).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(via).insert(kdb.Box(400, 400, 600, 600))  # 400 margin >= 55
    path = tmp_path / "met2_enclosing_via_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sky130_met2_via_layers_now_covered(tmp_path):
    """The #513 reproducer: a layout drawing well-formed met2/via1 geometry
    (previously invisible to `DECK`) now shows up in
    `coverage.layers_checked` and no longer appears in
    `coverage.layers_in_stream_without_rules` -- the acceptance criterion
    the issue itself states."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met1 = layout.layer(68, 20)
    layout.set_info(met1, kdb.LayerInfo(68, 20, "met1.drawing"))
    met2 = layout.layer(69, 20)
    layout.set_info(met2, kdb.LayerInfo(69, 20, "met2.drawing"))
    via = layout.layer(68, 44)
    layout.set_info(via, kdb.LayerInfo(68, 44, "via.drawing"))
    top.shapes(met1).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(met2).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(via).insert(kdb.Box(400, 400, 600, 600))
    path = tmp_path / "met2_via_covered.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert "68/44" in report["coverage"]["layers_checked"]
    assert "69/20" in report["coverage"]["layers_checked"]
    assert "68/44" not in report["coverage"]["layers_in_stream_without_rules"]
    assert "69/20" not in report["coverage"]["layers_in_stream_without_rules"]


# --- pad.enclosing.metal5.1 (PAD.4) (#545) --------------------------------
#
# PAD.4 ("Top layer metal overlap of pad opening", 2.0um / 2000 dbu) is
# scoped to the 5LM variant this deck already exclusively models: Metal5
# (81/0) must enclose Pad (37/0) -- the passivation opening -- by >= 2000
# dbu on every side.


def test_run_drc_gf180mcu_pad_enclosing_metal5_violation(tmp_path):
    """The issue's own reproducer: a Pad (37/0) opening completely outside
    its Metal5 (81/0) shape (no overlap at all) trips
    `pad.enclosing.metal5.1` instead of reporting `status: clean`."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal5 = layout.layer(81, 0)
    layout.set_info(metal5, kdb.LayerInfo(81, 0, "Metal5"))
    pad = layout.layer(37, 0)
    layout.set_info(pad, kdb.LayerInfo(37, 0, "Pad"))
    top.shapes(metal5).insert(kdb.Box(0, 0, 68000, 68000))
    top.shapes(pad).insert(kdb.Box(-2000, -2000, 70000, 70000))
    path = tmp_path / "pad_enclosing_metal5_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"]["pad.enclosing.metal5.1"] == 1
    (violation,) = [
        v for v in report["violations"] if v["rule"] == "pad.enclosing.metal5.1"
    ]
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "Metal5"


def test_run_drc_gf180mcu_pad_enclosing_metal5_clean(tmp_path):
    """A Pad opening enclosed by exactly the DRM's own 2.0um (2000 dbu)
    minimum margin of Metal5 on every side passes -- the boundary case."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal5 = layout.layer(81, 0)
    layout.set_info(metal5, kdb.LayerInfo(81, 0, "Metal5"))
    pad = layout.layer(37, 0)
    layout.set_info(pad, kdb.LayerInfo(37, 0, "Pad"))
    top.shapes(metal5).insert(kdb.Box(0, 0, 68000, 68000))
    top.shapes(pad).insert(kdb.Box(2000, 2000, 66000, 66000))  # 2000 margin >= 2000
    path = tmp_path / "pad_enclosing_metal5_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_gf180mcu_pad_layer_now_covered(tmp_path):
    """The issue's own acceptance criterion: a stream drawing well-formed
    Metal5/Pad geometry now shows `37/0` in `coverage.layers_checked` and no
    longer in `coverage.layers_in_stream_without_rules`."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal5 = layout.layer(81, 0)
    layout.set_info(metal5, kdb.LayerInfo(81, 0, "Metal5"))
    pad = layout.layer(37, 0)
    layout.set_info(pad, kdb.LayerInfo(37, 0, "Pad"))
    top.shapes(metal5).insert(kdb.Box(0, 0, 68000, 68000))
    top.shapes(pad).insert(kdb.Box(2000, 2000, 66000, 66000))
    path = tmp_path / "pad_layer_covered.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert "37/0" in report["coverage"]["layers_checked"]
    assert "37/0" not in report["coverage"]["layers_in_stream_without_rules"]


def test_sky130_met2_via_extraction_levels_have_drc_width_and_space_coverage():
    """Narrow instance of the issue's own "more generally" invariant
    suggestion (#513): the specific connectivity level this issue adds DRC
    coverage for -- met2 (69/20) and via1/met1<->met2 via (68/44), both new
    to `EXTRACTION_DECK.metals`/`.vias` as of #511 -- each have at least one
    `"width"` and one `"space"` rule in `DECK`, so this issue's own fix is
    verified structurally, not just via the violation-triggering fixtures
    above.

    Deliberately **not** generalised to every layer in
    `EXTRACTION_DECK.metals`/`.vias` (e.g. sky130's own `mcon.drawing`,
    67/44, has a `mcon.space.1` rule but no `mcon.width.1` rule, and
    gf180mcu's own `Metal4` has a `space` rule -- `mim.space.1`, approximated
    from the MiM capacitor's `MIMTM.1` -- but no dedicated `width` rule) or to
    every registered deck -- both predate this issue and are out of scope for
    it; asserting the invariant that broadly would fail on those unrelated
    gaps rather than verify this issue's fix. See #513 for a candidate
    follow-on generalising this check across every layer and every
    registered deck. (gf180mcu's own `Via1`-`Via4` closed this same class of
    gap in #546: each now has both a `width` and a `space` rule.)
    """
    deck = get_deck("sky130")
    extraction = get_extraction_deck("sky130")

    assert (69, 20) in extraction.metals  # met2.drawing
    assert (68, 44) in extraction.vias  # via.drawing (met1<->met2)

    width_layers = {rule.layer for rule in deck if rule.check == "width"}
    space_layers = {rule.layer for rule in deck if rule.check == "space"}

    for layer in ((69, 20), (68, 44)):
        assert layer in width_layers, f"{layer} has no width rule in DECK"
        assert layer in space_layers, f"{layer} has no space rule in DECK"
