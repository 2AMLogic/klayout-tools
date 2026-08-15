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
from klayout_tools.decks import (
    get_deck,
    get_extraction_deck,
    get_unmodeled_voltage_markers,
)
from klayout_tools.drc import DrcError, run_drc

# poly.width.1 (sky130 deck): minimum poly width is 150 dbu (0.15 um).
_POLY_WIDTH_THRESHOLD_DBU = 150

# poly2.width.1 (gf180mcu deck): minimum poly2 interconnect width is 180 dbu
# (0.18 um).
_GF180MCU_POLY2_WIDTH_THRESHOLD_DBU = 180

CORPUS_DIR = Path(__file__).parent / "corpus"
GF180MCU_CORPUS_FILES = sorted((CORPUS_DIR / "gf180mcu").glob("*.gds"))
SKY130_CORPUS_FILES = sorted((CORPUS_DIR / "sky130").glob("*.gds"))

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
# --top (issue #554)
# ---------------------------------------------------------------------------


def _make_multi_top_layout() -> kdb.Layout:
    """Two independent top cells: ``BAD`` seeds one ``poly.width.1``
    violation, ``GOOD`` is clean -- so ``--top`` scoping is observable."""
    layout = kdb.Layout()
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))

    good = layout.create_cell("GOOD")
    good.shapes(poly).insert(kdb.Box(0, 0, 200, 2000))  # 200 dbu -> clean

    bad = layout.create_cell("BAD")
    bad.shapes(poly).insert(kdb.Box(0, 0, 50, 2000))  # 50 dbu -> violation

    return layout


def test_top_scopes_violations_to_named_cell(tmp_path):
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    report_good = run_drc(str(path), "sky130", top="GOOD")
    assert report_good["status"] == "clean"
    assert report_good["violation_count"] == 0

    report_bad = run_drc(str(path), "sky130", top="BAD")
    assert report_bad["status"] == "violations"
    assert report_bad["violation_count"] == 1
    assert {v["cell"] for v in report_bad["violations"]} == {"BAD"}


def test_top_unknown_cell_raises(tmp_path):
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    with pytest.raises(DrcError, match="top cell not found in stream: NOPE"):
        run_drc(str(path), "sky130", top="NOPE")


def test_top_omitted_checks_every_top_cell(tmp_path):
    """Default (no ``--top``) behaviour is unchanged: every top cell is
    checked, so the seeded ``BAD`` violation still surfaces."""
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    report = run_drc(str(path), "sky130")
    assert report["violation_count"] == 1
    assert {v["cell"] for v in report["violations"]} == {"BAD"}


def test_cli_top_flag_scopes_report(tmp_path, capsys):
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    assert main(["drc", str(path), "--deck", "sky130", "--top", "GOOD"]) == 0
    capsys.readouterr()

    assert main(["drc", str(path), "--deck", "sky130", "--top", "BAD"]) == 3


def test_cli_unknown_top_exits_one(tmp_path, capsys):
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    assert main(["drc", str(path), "--deck", "sky130", "--top", "NOPE"]) == 1
    err = capsys.readouterr().err
    assert "top cell not found in stream: NOPE" in err


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


def test_run_drc_coverage_deck_scope_matches_sky130_rule_scopes(tmp_path):
    """`coverage.deck_scope` (#566) is every distinct non-empty `DrcRule.scope`
    across the sky130 deck's rules, deduplicated and sorted -- a static
    property of the deck, independent of what's actually drawn in the input
    stream (mirrors `deck_layers`, not `layers_checked`)."""
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))  # poly.drawing (66/20) only

    report = run_drc(str(path), "sky130")

    expected_scope = sorted({rule.scope for rule in get_deck("sky130") if rule.scope})
    assert expected_scope  # sanity: the sky130 deck does declare scopes
    assert report["coverage"]["deck_scope"] == expected_scope


def test_run_drc_coverage_deck_scope_matches_gf180mcu_rule_scopes(tmp_path):
    """Same invariant as the sky130 case above, for the gf180mcu deck."""
    layout = kdb.Layout()
    layout.create_cell("TOP")
    path = tmp_path / "empty.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    expected_scope = sorted({rule.scope for rule in get_deck("gf180mcu") if rule.scope})
    assert expected_scope
    assert report["coverage"]["deck_scope"] == expected_scope


def test_run_drc_coverage_deck_scope_static_regardless_of_input(tmp_path):
    """`deck_scope` does not depend on what the input stream actually draws
    (unlike `layers_checked`) -- an empty stream and a fully-covered stream
    against the same deck report the identical `deck_scope`, and adding a
    single seeded violation does not change it either."""
    empty_path = tmp_path / "empty.gds"
    empty_layout = kdb.Layout()
    empty_layout.create_cell("TOP")
    empty_layout.write(str(empty_path))

    violation_path = tmp_path / "violation.gds"
    _make_violation_layout().write(str(violation_path))

    empty_report = run_drc(str(empty_path), "sky130")
    violation_report = run_drc(str(violation_path), "sky130")

    assert (
        empty_report["coverage"]["deck_scope"]
        == violation_report["coverage"]["deck_scope"]
    )


def test_drc_rule_scope_defaults_to_empty_string_and_is_excluded_from_deck_scope():
    """Edge case (#566): a `DrcRule` with no assigned `scope` (the dataclass
    default, `""`) does not contribute an entry to `coverage.deck_scope` --
    verified directly against the dataclass default rather than a monkeypatched
    deck, since every rule in both shipped decks already declares a scope."""
    from klayout_tools.decks import DrcRule

    unscoped = DrcRule(
        id="unscoped.test.1",
        description="a rule that declines to declare a DRM scope",
        layer=(1, 0),
        check="width",
        threshold_dbu=100,
    )
    assert unscoped.scope == ""


def test_run_drc_coverage_deck_scope_empty_for_deck_with_zero_declared_scopes(
    tmp_path, monkeypatch
):
    """Edge case (#546/#566 test plan): a deck whose rules declare no `scope`
    at all reports an empty `coverage.deck_scope`, not an error."""
    import klayout_tools.drc as drc_module
    from klayout_tools.decks import DrcRule

    unscoped_deck = [
        DrcRule(
            id="poly.width.1",
            description="minimum poly width",
            layer=(66, 20),
            check="width",
            threshold_dbu=150,
            # scope left at its default ("") -- deliberately no DRM claim.
        ),
    ]
    monkeypatch.setattr(drc_module, "get_deck", lambda name: unscoped_deck)

    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["coverage"]["deck_scope"] == []


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
        "voltage_domain_warnings",
        "deck_scope",
    }
    for key, field in coverage.items():
        assert isinstance(field, list)
        if key == "voltage_domain_warnings":
            for entry in field:
                assert set(entry.keys()) == {"marker", "description"}
                assert isinstance(entry["marker"], str)
                assert isinstance(entry["description"], str)
        else:
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


def _make_gf180mcu_dualgate_layout(*, overlap: bool) -> kdb.Layout:
    """Issue #552's own DRC reproducer: a 0.25um-wide `Comp` stripe (illegal
    at 5V/6V -- `DF.1a_MV` requires 0.30um -- but legal at this deck's only
    modelled 3.3V column) with an `Nplus` shape, both inside `Dualgate`.

    ``overlap=False`` moves the `Dualgate` shape far away from the `Comp`/
    `Nplus` geometry instead -- present in the stream, but touching no
    checked geometry -- the false-positive-avoidance counterfactual."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        li = layout.layer(layer, datatype)
        layout.set_info(li, kdb.LayerInfo(layer, datatype))
        top.shapes(li).insert(box)

    draw(22, 0, kdb.Box(0, 0, 250, 3000))  # Comp, 0.25um wide
    draw(32, 0, kdb.Box(-400, -400, 650, 3400))  # Nplus
    if overlap:
        draw(55, 0, kdb.Box(-1000, -1000, 1250, 4000))  # Dualgate, overlapping
    else:
        draw(55, 0, kdb.Box(100_000, 100_000, 101_000, 101_000))  # far away
    return layout


def test_run_drc_gf180mcu_dualgate_marker_warns_when_overlapping_checked_layer(
    tmp_path,
):
    """Issue #552's own reproducer: geometry drawn inside `Dualgate` (the
    5V/6V thick-oxide marker) is checked against this deck's only modelled
    (3.3V) thresholds and reports `status: clean` -- exactly as before this
    fix -- but `coverage.voltage_domain_warnings` is the new loud signal
    that the checked column may not be the right one for this geometry."""
    path = tmp_path / "mv_bad.gds"
    _make_gf180mcu_dualgate_layout(overlap=True).write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    expected_description = get_unmodeled_voltage_markers("gf180mcu")[(55, 0)]
    assert report["coverage"]["voltage_domain_warnings"] == [
        {"marker": "55/0", "description": expected_description}
    ]
    # `Dualgate` itself carries no rules of its own -- it remains an
    # unrecognised layer alongside the new, more specific warning above.
    assert "55/0" in report["coverage"]["layers_in_stream_without_rules"]


def test_run_drc_gf180mcu_dualgate_marker_no_warning_without_overlap(tmp_path):
    """Counterfactual: `Dualgate` present in the stream but never interacting
    with any layer this run actually checked produces no warning -- the gate
    is "interacts with a checked layer", not bare presence, so a marker shape
    with nothing behind it never produces a false-positive warning."""
    path = tmp_path / "mv_no_overlap.gds"
    _make_gf180mcu_dualgate_layout(overlap=False).write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["coverage"]["voltage_domain_warnings"] == []
    assert "55/0" in report["coverage"]["layers_in_stream_without_rules"]


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

    # Regression pin against this specific, static, committed fixture. This
    # pin was 4 `diff.enclosing.licon.1` violations until #995: all four were
    # the same false positive -- `sky130_fd_sc_hd__and3_1` draws its own
    # `diff` as two abutting, unmerged rectangles, and each flagged licon1
    # cut sits 25 dbu from that internal seam while enclosed by ~925 dbu of
    # the *merged* diff region. Every one of the four edge pairs was 25 dbu
    # wide, inside a single cell instance -- not, as
    # docs/cli/drc.md previously recorded, real geometry at a standard-cell
    # row boundary that filler-cell insertion would absorb.
    assert report["violation_count"] == 0
    assert report["rule_counts"] == {}
    assert report["status"] == "clean"


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
    margin on every side) passes.

    Sized 300x300 dbu (>= the 260 dbu `via4.width.1` minimum added by #546)
    rather than the smaller placeholder size used before that rule existed,
    so this fixture stays genuinely `"clean"` under the now-fuller via4 rule
    coverage, not just under `mim.enclosing.via4.1` alone.
    """
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


# --- via1.width.1/space.1 .. via4.width.1/space.1 (Vn.1/Vn.2a, #546) ------
#
# Before #546, `DECK` had no `width`/`space` rule at all for Via1 (35/0),
# Via2 (38/0), Via3 (40/0), or Via4 (41/0) -- Via4 was referenced only as
# `mim.enclosing.via4.1`'s `other_layer` (the MiM cap's top-plate via), which
# made `coverage.layers_checked` misreport it as "checked" even though
# nothing constrained its own size or spacing (see the issue's own repro).
# Via1-Via3 were not deck layers at all. One parametrized violation/clean
# pair per via layer below (mirroring the metal2/metal3/metal5 pattern
# above), plus the structural coverage assertion and the issue's own
# reproducer.

_GF180MCU_VIA_LAYERS = [
    ("via1", (35, 0), "Via1"),
    ("via2", (38, 0), "Via2"),
    ("via3", (40, 0), "Via3"),
    ("via4", (41, 0), "Via4"),
]


@pytest.mark.parametrize("rule_prefix,layer_tuple,layer_name", _GF180MCU_VIA_LAYERS)
def test_run_drc_gf180mcu_via_width_violation(
    rule_prefix, layer_tuple, layer_name, tmp_path
):
    """A via square narrower than the 260 dbu (0.26 um) `<via>.width.1`
    threshold trips exactly one violation (the minimum-width half of the
    official "Vn.1" min/max size rule -- see `via1.width.1`'s docstring in
    `gf180mcu.py` for why only the minimum half is enforced)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    via = layout.layer(*layer_tuple)
    layout.set_info(via, kdb.LayerInfo(layer_tuple[0], layer_tuple[1], layer_name))
    top.shapes(via).insert(kdb.Box(0, 0, 100, 2000))  # 100 dbu < 260
    path = tmp_path / f"{rule_prefix}_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {f"{rule_prefix}.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == f"{rule_prefix}.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == layer_name


@pytest.mark.parametrize("rule_prefix,layer_tuple,layer_name", _GF180MCU_VIA_LAYERS)
def test_run_drc_gf180mcu_via_space_violation(
    rule_prefix, layer_tuple, layer_name, tmp_path
):
    """Two via squares closer than the 260 dbu `<via>.space.1` threshold
    (but not touching -- see the module docstring's note on why this deck's
    engine cannot distinguish an ordinary two-via gap from a >=4x4 via
    array's tighter "Vn.2b" spacing) trip exactly one violation."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    via = layout.layer(*layer_tuple)
    layout.set_info(via, kdb.LayerInfo(layer_tuple[0], layer_tuple[1], layer_name))
    top.shapes(via).insert(kdb.Box(0, 0, 300, 300))
    top.shapes(via).insert(kdb.Box(400, 0, 700, 300))  # 100 dbu gap < 260
    path = tmp_path / f"{rule_prefix}_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {f"{rule_prefix}.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == f"{rule_prefix}.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == layer_name


@pytest.mark.parametrize("rule_prefix,layer_tuple,layer_name", _GF180MCU_VIA_LAYERS)
def test_run_drc_gf180mcu_via_clean(rule_prefix, layer_tuple, layer_name, tmp_path):
    """A single, properly-sized (>= 260 dbu), properly-isolated via square
    passes both `<via>.width.1` and `<via>.space.1`."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    via = layout.layer(*layer_tuple)
    layout.set_info(via, kdb.LayerInfo(layer_tuple[0], layer_tuple[1], layer_name))
    top.shapes(via).insert(kdb.Box(0, 0, 300, 300))  # 300 >= 260
    path = tmp_path / f"{rule_prefix}_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_gf180mcu_via_layers_have_drc_width_and_space_coverage():
    """Structural check (mirrors #513's own
    `test_sky130_met2_via_extraction_levels_have_drc_width_and_space_coverage`):
    each of Via1-Via4 -- the same four via layers `EXTRACTION_DECK.vias`
    already routes connectivity through -- has at least one `"width"` and one
    `"space"` rule in `DECK`, verified structurally rather than only via the
    violation-triggering fixtures above."""
    deck = get_deck("gf180mcu")
    for _rule_prefix, layer_tuple, _layer_name in _GF180MCU_VIA_LAYERS:
        width_rules = [r for r in deck if r.layer == layer_tuple and r.check == "width"]
        space_rules = [r for r in deck if r.layer == layer_tuple and r.check == "space"]
        assert len(width_rules) >= 1, f"no width rule for via layer {layer_tuple}"
        assert len(space_rules) >= 1, f"no space rule for via layer {layer_tuple}"


def test_run_drc_gf180mcu_via_layers_now_covered_by_their_own_rules(tmp_path):
    """The #546 reproducer's coverage claim: a layout drawing well-formed
    Via1-Via4 geometry shows all four via layers in `coverage.layers_checked`
    (previously true for none of them -- Via1-Via3 were not deck layers at
    all, and Via4 (41/0) was a deck layer only via `mim.enclosing.via4.1`'s
    `other_layer`), and each is now backed by its own `<via>.width.1`/
    `<via>.space.1` rule, not solely by `mim.enclosing.via4.1`."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for _rule_prefix, layer_tuple, layer_name in _GF180MCU_VIA_LAYERS:
        li = layout.layer(*layer_tuple)
        layout.set_info(li, kdb.LayerInfo(layer_tuple[0], layer_tuple[1], layer_name))
        top.shapes(li).insert(kdb.Box(0, 0, 300, 300))  # legally sized, isolated
    path = tmp_path / "via_layers_covered.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    for _rule_prefix, layer_tuple, _layer_name in _GF180MCU_VIA_LAYERS:
        formatted = f"{layer_tuple[0]}/{layer_tuple[1]}"
        assert formatted in report["coverage"]["layers_checked"]
        assert formatted not in report["coverage"]["layers_in_stream_without_rules"]

    deck = get_deck("gf180mcu")
    via4_rule_ids = {r.id for r in deck if r.layer == (41, 0)}
    assert via4_rule_ids != {"mim.enclosing.via4.1"}, (
        "via4 (41/0) must be backed by its own via4.width.1/via4.space.1 "
        "rules, not solely by mim.enclosing.via4.1's other_layer reference"
    )
    assert "via4.width.1" in via4_rule_ids
    assert "via4.space.1" in via4_rule_ids


def test_run_drc_gf180mcu_via4_reproducer_from_issue_546(tmp_path):
    """A close variant of issue #546's own reproducer (two Via4 cuts drawn at
    ~3x the legal 0.26 um size, per `Vn.1`) now reports a violation instead
    of `status: clean`.

    The issue's literal geometry draws the two cuts exactly *abutting* (a
    shared edge, zero gap) -- `kdb.Region` merges touching same-layer shapes
    by default (its `merged_semantics` default), so those two 780x780 dbu
    squares become a single 1560x780 dbu merged rectangle whose minimum width
    (780 dbu) is well above the 260 dbu minimum, and `space_check` sees one
    polygon, not two, so it reports no space violation either -- the same
    "our width_check enforces only a minimum, never the official rule's
    fixed-size maximum" approximation `contact.width.1`/`via1.width.1` (etc.)
    already document, just triggered by two abutting oversized cuts merging
    into one shape rather than by a single one. Widening the gap to 100 dbu
    (<< the 260 dbu `via4.space.1` minimum) keeps every dimension from the
    issue's own reproducer (each cut still 780x780 dbu, ~3x the legal size)
    while producing two genuinely separate polygons that `via4.space.1`
    catches -- the same underlying "illegal via geometry reports clean" bug
    the issue reports, demonstrated through the one approximation
    (`space_check`) this deck's rules can enforce rather than the one
    (a min/max `width_check`) the curator's own implementation guidance
    said not to add as part of this issue.
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
    # 100 dbu gap (not the issue's literal 0 dbu abutment -- see docstring
    # above) so the two cuts stay distinct polygons for via4.space.1.
    top.shapes(via4).insert(kdb.Box(1880, 1000, 2660, 1780))

    path = tmp_path / "via_bad.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["violation_count"] > 0
    assert report["rule_counts"].get("via4.space.1", 0) >= 1


# --- conductor-over-cut enclosure (CO.6, V1.3a, Vn.3b/Vn.4a, #551) --------
#
# Before #551 the deck checked the layers *below* a cut but never the
# conductor *above* it: Contact (33/0) had `poly2.enclosing.contact.1` and
# `comp.enclosing.contact.1` but no Metal1 rule, and Via1-Via4 had size and
# spacing rules (#544/#546) but no metal-enclosure rule in either direction.
# A contact whose landing metal missed one of its edges therefore reported
# `status: clean` (the issue's own reproducer, exercised verbatim by
# `test_run_drc_gf180mcu_co6_reproducer_from_issue_551` below).
#
# One parametrized escape/marginal/clean set below covers all nine new
# rules, reusing the `_GF180MCU_VIA_LAYERS` fixture pattern (#544/#546)
# rather than one hand-written test per level. Entry shape:
# (rule id, conductor layer, conductor name, cut layer, cut name, threshold).

_GF180MCU_CUT_ENCLOSURE_RULES = [
    ("metal1.enclosing.contact.1", (34, 0), "Metal1", (33, 0), "Contact", 5),
    ("metal1.enclosing.via1.1", (34, 0), "Metal1", (35, 0), "Via1", 0),
    ("metal2.enclosing.via1.1", (36, 0), "Metal2", (35, 0), "Via1", 10),
    ("metal2.enclosing.via2.1", (36, 0), "Metal2", (38, 0), "Via2", 10),
    ("metal3.enclosing.via2.1", (42, 0), "Metal3", (38, 0), "Via2", 10),
    ("metal3.enclosing.via3.1", (42, 0), "Metal3", (40, 0), "Via3", 10),
    ("metal4.enclosing.via3.1", (46, 0), "Metal4", (40, 0), "Via3", 10),
    ("metal4.enclosing.via4.1", (46, 0), "Metal4", (41, 0), "Via4", 10),
    ("metal5.enclosing.via4.1", (81, 0), "Metal5", (41, 0), "Via4", 10),
]

# Only the non-zero-threshold rules can produce a *marginal* (covered, but
# by less than the threshold) violation; V1.3a's 0.0 um requirement has no
# marginal case by construction.
_GF180MCU_CUT_ENCLOSURE_RULES_WITH_MARGIN = [
    entry for entry in _GF180MCU_CUT_ENCLOSURE_RULES if entry[5] > 0
]


def _gf180mcu_cut_stack(tmp_path, name, conductor, cut, conductor_box, cut_box):
    """Write a two-layer conductor-over-cut layout and return its path."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    cond_layer = layout.layer(*conductor)
    layout.set_info(cond_layer, kdb.LayerInfo(conductor[0], conductor[1], "conductor"))
    cut_layer = layout.layer(*cut)
    layout.set_info(cut_layer, kdb.LayerInfo(cut[0], cut[1], "cut"))
    top.shapes(cond_layer).insert(conductor_box)
    top.shapes(cut_layer).insert(cut_box)
    path = tmp_path / f"{name}.gds"
    layout.write(str(path))
    return path


@pytest.mark.parametrize(
    "rule_id,conductor,conductor_name,cut,cut_name,threshold_dbu",
    _GF180MCU_CUT_ENCLOSURE_RULES,
    ids=[entry[0] for entry in _GF180MCU_CUT_ENCLOSURE_RULES],
)
def test_run_drc_gf180mcu_cut_enclosure_escape_violation(
    rule_id, conductor, conductor_name, cut, cut_name, threshold_dbu, tmp_path
):
    """The defect class #551 reports: a legally-sized cut whose conductor
    misses one of its edges entirely (100 dbu of the cut escapes to the
    left) trips exactly one violation of the conductor-over-cut rule.

    This is the only failure mode `metal1.enclosing.via1.1` can have (its
    "V1.3a" threshold is a literal 0.0 um), and it is the mode the issue's
    own reproducer exhibits, so it is exercised for every rule."""
    path = _gf180mcu_cut_stack(
        tmp_path,
        f"{rule_id}_escape",
        conductor,
        cut,
        kdb.Box(1100, 800, 1600, 1600),  # misses the cut's left 100 dbu
        kdb.Box(1000, 1000, 1300, 1300),  # 300 dbu, legal for Contact and Vian
    )

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {rule_id: 1}
    (violation,) = report["violations"]
    assert violation["rule"] == rule_id
    assert violation["check"] == "enclosing"
    # The reporting identity of an enclosure rule is its *enclosing* layer.
    assert violation["layer"] == conductor_name


@pytest.mark.parametrize(
    "rule_id,conductor,conductor_name,cut,cut_name,threshold_dbu",
    _GF180MCU_CUT_ENCLOSURE_RULES_WITH_MARGIN,
    ids=[entry[0] for entry in _GF180MCU_CUT_ENCLOSURE_RULES_WITH_MARGIN],
)
def test_run_drc_gf180mcu_cut_enclosure_marginal_violation(
    rule_id, conductor, conductor_name, cut, cut_name, threshold_dbu, tmp_path
):
    """The subtler failure mode: the conductor *does* cover the cut, but by
    one dbu less than the rule's threshold on one side -- caught by
    `Region.enclosing_check`'s facing-edge measurement rather than by the
    zero-overlap escape term the test above exercises."""
    margin = threshold_dbu - 1
    path = _gf180mcu_cut_stack(
        tmp_path,
        f"{rule_id}_marginal",
        conductor,
        cut,
        kdb.Box(1000 - margin, 800, 1600, 1600),
        kdb.Box(1000, 1000, 1300, 1300),
    )

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {rule_id: 1}
    (violation,) = report["violations"]
    assert violation["rule"] == rule_id
    assert violation["check"] == "enclosing"
    assert violation["layer"] == conductor_name


@pytest.mark.parametrize(
    "rule_id,conductor,conductor_name,cut,cut_name,threshold_dbu",
    _GF180MCU_CUT_ENCLOSURE_RULES,
    ids=[entry[0] for entry in _GF180MCU_CUT_ENCLOSURE_RULES],
)
def test_run_drc_gf180mcu_cut_enclosure_clean(
    rule_id, conductor, conductor_name, cut, cut_name, threshold_dbu, tmp_path
):
    """A properly-landed cut -- 200 dbu of conductor on every side, far above
    every threshold in this family (0-10 dbu) -- reports clean."""
    path = _gf180mcu_cut_stack(
        tmp_path,
        f"{rule_id}_clean",
        conductor,
        cut,
        kdb.Box(800, 800, 1600, 1600),
        kdb.Box(1000, 1000, 1300, 1300),
    )

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_gf180mcu_via_layers_have_drc_enclosure_coverage():
    """Structural check (mirrors
    `test_gf180mcu_via_layers_have_drc_width_and_space_coverage` above, per
    #551's own test plan): each of Via1-Via4 is now the `other_layer` of at
    least one `"enclosing"` rule *in both directions* -- one conductor below
    the cut and one above it -- in addition to its existing `"width"`/
    `"space"` rules. Before #551 every via level had size and spacing rules
    but nothing constraining the metal that lands on it."""
    deck = get_deck("gf180mcu")
    # Which drawn conductor sits below / above each via level (5LM variant).
    stack = {
        (35, 0): ((34, 0), (36, 0)),  # Via1: Metal1 -> Metal2
        (38, 0): ((36, 0), (42, 0)),  # Via2: Metal2 -> Metal3
        (40, 0): ((42, 0), (46, 0)),  # Via3: Metal3 -> Metal4
        (41, 0): ((46, 0), (81, 0)),  # Via4: Metal4 -> Metal5
    }
    for _rule_prefix, via_layer, _layer_name in _GF180MCU_VIA_LAYERS:
        enclosing = [
            r
            for r in deck
            if r.check == "enclosing"
            and r.other_layer == via_layer
            and r.derived_layer is None
        ]
        assert len(enclosing) >= 1, f"no enclosing rule for via layer {via_layer}"
        below, above = stack[via_layer]
        conductors = {r.layer for r in enclosing}
        assert below in conductors, f"no below-cut enclosure rule for {via_layer}"
        assert above in conductors, f"no above-cut enclosure rule for {via_layer}"


def test_gf180mcu_contact_has_conductor_above_enclosure_coverage():
    """The narrower half of #551: the Contact layer (33/0) was already the
    `other_layer` of two *below*-the-cut enclosure rules (`CO.3` Poly2 and
    `CO.4` Comp) and of none above it. `CO.6` (Metal1) closes that."""
    deck = get_deck("gf180mcu")
    contact_enclosers = {
        r.layer for r in deck if r.check == "enclosing" and r.other_layer == (33, 0)
    }
    assert (30, 0) in contact_enclosers  # Poly2, CO.3 (pre-existing)
    assert (22, 0) in contact_enclosers  # Comp, CO.4 (pre-existing)
    assert (34, 0) in contact_enclosers  # Metal1, CO.6 (#551)


def test_run_drc_gf180mcu_co6_reproducer_from_issue_551(tmp_path):
    """Issue #551's own reproducer, verbatim: a legally-sized 0.22 um contact
    on COMP whose Metal1 strap misses its left edge by 0.1 um. The PDK's own
    KLayout deck reports `CO.6` on this stream; before #551 `klt drc --deck
    gf180mcu` reported `status: clean`."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("co6_bad")
    for layer_tuple, name, box in (
        ((22, 0), "Comp", kdb.Box(0, 0, 3000, 3000)),
        ((32, 0), "Nplus", kdb.Box(-400, -400, 3400, 3400)),
        ((33, 0), "Contact", kdb.Box(1000, 1000, 1220, 1220)),
        ((34, 0), "Metal1", kdb.Box(1100, 800, 1500, 1500)),
    ):
        li = layout.layer(*layer_tuple)
        layout.set_info(li, kdb.LayerInfo(layer_tuple[0], layer_tuple[1], name))
        top.shapes(li).insert(box)

    path = tmp_path / "co6_bad.gds"
    layout.write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"].get("metal1.enclosing.contact.1", 0) >= 1


@pytest.mark.parametrize(
    "layout_path", GF180MCU_CORPUS_FILES, ids=[p.name for p in GF180MCU_CORPUS_FILES]
)
def test_gf180mcu_corpus_no_false_conductor_over_cut_violations(layout_path: Path):
    """Regression guard (#551's own test plan): the new conductor-over-cut
    rules must not fire against correct-by-construction geometry. Real
    gf180mcu standard cells land Metal1 on Contact with exactly the `CO.6`
    0.005 um margin -- one dbu of over-transcription (0.01 um) would flag
    every contact in every one of these cells."""
    report = run_drc(str(layout_path), "gf180mcu")

    for rule_id, *_ in _GF180MCU_CUT_ENCLOSURE_RULES:
        assert report["rule_counts"].get(rule_id, 0) == 0, (
            f"{rule_id} false-positives on corpus cell {layout_path.name}"
        )


# --- sky130 li1.enclosing.licon1.1 (li.5's zero-margin floor, #551) -------


def _sky130_li_licon_stack(tmp_path, name, li_box, licon_box):
    """Write a two-layer li1-over-licon1 layout and return its path."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    li1 = layout.layer(67, 20)
    layout.set_info(li1, kdb.LayerInfo(67, 20, "li1.drawing"))
    licon1 = layout.layer(66, 44)
    layout.set_info(licon1, kdb.LayerInfo(66, 44, "licon1.drawing"))
    top.shapes(li1).insert(li_box)
    top.shapes(licon1).insert(licon_box)
    path = tmp_path / f"{name}.gds"
    layout.write(str(path))
    return path


def test_run_drc_sky130_li1_enclosing_licon1_violation(tmp_path):
    """sky130's half of #551: `diff`/`poly` (the layers *below* licon1) were
    already checked by `licon.5`/`licon.8`, but nothing checked li1 -- the
    conductor immediately *above* it. A licon1 cut whose li1 strap misses its
    left edge entirely now trips `li1.enclosing.licon1.1`."""
    path = _sky130_li_licon_stack(
        tmp_path,
        "li1_licon1_escape",
        kdb.Box(1100, 800, 1600, 1600),  # misses the cut's left 100 dbu
        kdb.Box(1000, 1000, 1170, 1170),  # 170 dbu, the licon.1 size
    )

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"li1.enclosing.licon1.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "li1.enclosing.licon1.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "li1.drawing"


def test_run_drc_sky130_li1_enclosing_licon1_clean(tmp_path):
    """A licon1 cut fully covered by its li1 strap reports clean."""
    path = _sky130_li_licon_stack(
        tmp_path,
        "li1_licon1_clean",
        kdb.Box(800, 800, 1600, 1600),
        kdb.Box(1000, 1000, 1170, 1170),
    )

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sky130_li1_enclosing_licon1_flush_edges_clean(tmp_path):
    """The reason `li1.enclosing.licon1.1` is transcribed at li.5's
    zero-margin floor rather than its published 0.08 um: li.5 only requires
    that margin on *two adjacent* edges, so real layout routinely lands a
    minimum-width li1 strap exactly flush with the cut on the other two. That
    geometry is legal and must stay clean -- an unconditional 0.08 um check
    would flag it (and, measured against this repo's own corpus, 6-56 sites
    per standard cell)."""
    path = _sky130_li_licon_stack(
        tmp_path,
        "li1_licon1_flush",
        kdb.Box(1000, 1000, 1600, 1600),  # flush on the left and bottom edges
        kdb.Box(1000, 1000, 1170, 1170),
    )

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_sky130_licon1_has_conductor_above_enclosure_coverage():
    """Structural half of the sky130 fix: licon1 (66/44) is now the
    `other_layer` of an enclosure rule on the conductor above it (li1), not
    only of the two below it (`diff`, `poly`)."""
    deck = get_deck("sky130")
    licon_enclosers = {
        r.layer for r in deck if r.check == "enclosing" and r.other_layer == (66, 44)
    }
    assert (65, 20) in licon_enclosers  # diff.drawing, licon.5 (pre-existing)
    assert (66, 20) in licon_enclosers  # poly.drawing, licon.8 (pre-existing)
    assert (67, 20) in licon_enclosers  # li1.drawing, li.5 floor (#551)


@pytest.mark.parametrize(
    "layout_path", SKY130_CORPUS_FILES, ids=[p.name for p in SKY130_CORPUS_FILES]
)
def test_sky130_corpus_no_false_li1_over_licon1_violations(layout_path: Path):
    """Regression guard (#551's own test plan): `li1.enclosing.licon1.1` must
    not fire against real sky130 standard cells, whose li1 straps sit flush
    with licon1 on two edges by design (see the rule's own docstring)."""
    report = run_drc(str(layout_path), "sky130")

    assert report["rule_counts"].get("li1.enclosing.licon1.1", 0) == 0


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


# --- #995: touching-but-unmerged same-layer shapes must not false-positive --


def _sky130_diff_licon_stack(tmp_path, name, diff_boxes, licon_box):
    """Write a two-layer diff-under-licon1 layout and return its path."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    diff = layout.layer(65, 20)
    layout.set_info(diff, kdb.LayerInfo(65, 20, "diff.drawing"))
    licon1 = layout.layer(66, 44)
    layout.set_info(licon1, kdb.LayerInfo(66, 44, "licon1.drawing"))
    for box in diff_boxes:
        top.shapes(diff).insert(box)
    top.shapes(licon1).insert(licon_box)
    path = tmp_path / f"{name}.gds"
    layout.write(str(path))
    return path


def test_run_drc_sky130_diff_enclosing_licon_touching_shapes_clean(tmp_path):
    """Issue #995's exact reproducer geometry: the enclosing layer is drawn
    as two abutting (touching, non-overlapping) rectangles that share a
    vertical edge at x=1035 rather than one merged polygon -- an ordinary
    GDS authoring/tiling choice, not a drawn defect. The licon1 cut sits
    close to that internal seam.

    Measured against the *merged* diff region the cut is enclosed by 925 dbu
    on its left side, far beyond `diff.enclosing.licon.1`'s 40 dbu margin.
    Measured against the raw, unmerged polygons, the right-hand rectangle's
    own left edge is only 25 dbu away, so `Region.enclosing_check` reported a
    violation that the physical geometry does not have.
    """
    path = _sky130_diff_licon_stack(
        tmp_path,
        "diff_licon_touching_shapes",
        [
            kdb.Box(135, 1500, 1035, 1920),  # rect A
            kdb.Box(1035, 1500, 1505, 1920),  # rect B, touches A at x=1035
        ],
        kdb.Box(1060, 1615, 1230, 1785),  # cut near the seam, 25 dbu right of it
    )

    report = run_drc(str(path), "sky130")

    # The rule really ran (both layers are in the stream) -- a "clean" verdict
    # here must not be the vacuous kind that a skipped rule would produce.
    assert "diff.enclosing.licon.1" not in report["coverage"]["rules_skipped"]
    assert report["rule_counts"].get("diff.enclosing.licon.1", 0) == 0
    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sky130_diff_enclosing_licon_touching_shapes_real_shortfall(tmp_path):
    """Control for the regression above: merging must only remove *false*
    positives, never mask a genuine enclosure shortfall. The same two
    touching rectangles, with the cut moved to within 25 dbu of the merged
    region's own outer (right) edge, still trip `diff.enclosing.licon.1`."""
    path = _sky130_diff_licon_stack(
        tmp_path,
        "diff_licon_touching_shapes_shortfall",
        [
            kdb.Box(135, 1500, 1035, 1920),  # rect A
            kdb.Box(1035, 1500, 1505, 1920),  # rect B, touches A at x=1035
        ],
        kdb.Box(1310, 1615, 1480, 1785),  # 25 dbu inside the merged right edge
    )

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"]["diff.enclosing.licon.1"] == 1
    (violation,) = [
        v for v in report["violations"] if v["rule"] == "diff.enclosing.licon.1"
    ]
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "diff.drawing"


def test_run_drc_synthetic_enclosed_check_touching_shapes_clean(tmp_path, monkeypatch):
    """Symmetric coverage for `check="enclosed"` (#995): neither shipped deck
    has an `"enclosed"` rule, so this exercises the dispatch against the same
    minimal synthetic deck style used for #318 -- with the *enclosing* side
    (`other_layer` here) drawn as two touching-but-unmerged rectangles."""
    from klayout_tools.decks import DrcRule

    synthetic_deck = [
        DrcRule(
            id="inner.enclosed.outer.1",
            description="synthetic: inner must be enclosed by outer by >= 40 dbu",
            layer=(10, 0),  # inner (the enclosed layer)
            other_layer=(20, 0),  # outer (the enclosing layer)
            check="enclosed",
            threshold_dbu=40,
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
    top.shapes(outer).insert(kdb.Box(135, 1500, 1035, 1920))
    top.shapes(outer).insert(kdb.Box(1035, 1500, 1505, 1920))
    top.shapes(inner).insert(kdb.Box(1060, 1615, 1230, 1785))
    path = tmp_path / "synthetic_enclosed_touching_shapes.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

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
    67/44, has a `mcon.space.1` rule but no `mcon.width.1` rule) or to every
    registered deck (gf180mcu's `Metal4`/`Via1`-`Via3` have an analogous
    pre-existing gap) -- both predate this issue and are out of scope for
    it; asserting the invariant that broadly would fail on those unrelated
    gaps rather than verify this issue's fix. See #513 for a candidate
    follow-on generalising this check across every layer and every
    registered deck.
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


# ---------------------------------------------------------------------------
# sky130 met3/met4/met5 + via2/via3/via4 + capm/capm2 rule coverage (#776)
#
# #619 extended `EXTRACTION_DECK.metals`/`.vias` to met3/met4/met5 and
# via2/via3/via4, and #225 gave `EXTRACTION_DECK.capacitors` genuine
# `capm`/`capm2` MiM-cap device recognition, but `DECK` had no rule of any
# kind above met2/via -- `klt drc --deck sky130` reported a bare `clean`
# verdict on all of it. These tests exercise the 28 rules added to close
# that gap.
# ---------------------------------------------------------------------------


def test_run_drc_sky130_met3_width_violation(tmp_path):
    """A met3 bar narrower than the 300 dbu (0.3 um) `met3.width.1`
    threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met3 = layout.layer(70, 20)
    layout.set_info(met3, kdb.LayerInfo(70, 20, "met3.drawing"))
    top.shapes(met3).insert(kdb.Box(0, 0, 200, 6000))  # 200 dbu < 300
    path = tmp_path / "met3_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met3.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met3.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "met3.drawing"


def test_run_drc_sky130_met3_space_violation(tmp_path):
    """Two met3 bars closer than the 300 dbu `met3.space.1` threshold trip
    exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met3 = layout.layer(70, 20)
    layout.set_info(met3, kdb.LayerInfo(70, 20, "met3.drawing"))
    top.shapes(met3).insert(kdb.Box(0, 0, 4000, 6000))
    top.shapes(met3).insert(kdb.Box(4200, 0, 8000, 6000))  # 200 dbu gap < 300
    path = tmp_path / "met3_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met3.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met3.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "met3.drawing"


def test_run_drc_sky130_via2_width_violation(tmp_path):
    """A via2 (`via2.drawing`, 69/44) shape narrower than the 200 dbu
    (0.2 um) `via2.width.1` threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via2 = layout.layer(69, 44)
    layout.set_info(via2, kdb.LayerInfo(69, 44, "via2.drawing"))
    top.shapes(via2).insert(kdb.Box(0, 0, 100, 4000))  # 100 dbu < 200
    path = tmp_path / "via2_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"via2.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "via2.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "via2.drawing"


def test_run_drc_sky130_via2_space_violation(tmp_path):
    """Two via2 shapes closer than the 200 dbu `via2.space.1` threshold
    trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via2 = layout.layer(69, 44)
    layout.set_info(via2, kdb.LayerInfo(69, 44, "via2.drawing"))
    top.shapes(via2).insert(kdb.Box(0, 0, 300, 300))
    top.shapes(via2).insert(kdb.Box(400, 0, 700, 300))  # 100 dbu gap < 200
    path = tmp_path / "via2_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"via2.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "via2.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "via2.drawing"


def test_run_drc_sky130_met2_enclosing_via2_violation(tmp_path):
    """A via2 shape hanging off the edge of its met2 landing pad by less
    than the 40 dbu (0.04 um) `met2.enclosing.via2.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met2 = layout.layer(69, 20)
    layout.set_info(met2, kdb.LayerInfo(69, 20, "met2.drawing"))
    via2 = layout.layer(69, 44)
    layout.set_info(via2, kdb.LayerInfo(69, 44, "via2.drawing"))
    top.shapes(met2).insert(kdb.Box(0, 0, 1000, 1000))
    # 400 dbu margin on 3 sides, only 10 dbu (< 40) margin on the right.
    top.shapes(via2).insert(kdb.Box(400, 400, 990, 600))
    path = tmp_path / "met2_enclosing_via2_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met2.enclosing.via2.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met2.enclosing.via2.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "met2.drawing"


def test_run_drc_sky130_met3_enclosing_via2_violation(tmp_path):
    """A via2 shape hanging off the edge of its met3 landing pad by less
    than the 65 dbu (0.065 um) `met3.enclosing.via2.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met3 = layout.layer(70, 20)
    layout.set_info(met3, kdb.LayerInfo(70, 20, "met3.drawing"))
    via2 = layout.layer(69, 44)
    layout.set_info(via2, kdb.LayerInfo(69, 44, "via2.drawing"))
    top.shapes(met3).insert(kdb.Box(0, 0, 1000, 1000))
    # 400 dbu margin on 3 sides, only 10 dbu (< 65) margin on the right.
    top.shapes(via2).insert(kdb.Box(400, 400, 990, 600))
    path = tmp_path / "met3_enclosing_via2_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met3.enclosing.via2.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met3.enclosing.via2.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "met3.drawing"


def test_run_drc_sky130_met4_width_violation(tmp_path):
    """A met4 bar narrower than the 300 dbu (0.3 um) `met4.width.1`
    threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met4 = layout.layer(71, 20)
    layout.set_info(met4, kdb.LayerInfo(71, 20, "met4.drawing"))
    top.shapes(met4).insert(kdb.Box(0, 0, 200, 6000))  # 200 dbu < 300
    path = tmp_path / "met4_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met4.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met4.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "met4.drawing"


def test_run_drc_sky130_met4_space_violation(tmp_path):
    """Two met4 bars closer than the 300 dbu `met4.space.1` threshold trip
    exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met4 = layout.layer(71, 20)
    layout.set_info(met4, kdb.LayerInfo(71, 20, "met4.drawing"))
    top.shapes(met4).insert(kdb.Box(0, 0, 4000, 6000))
    top.shapes(met4).insert(kdb.Box(4200, 0, 8000, 6000))  # 200 dbu gap < 300
    path = tmp_path / "met4_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met4.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met4.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "met4.drawing"


def test_run_drc_sky130_via3_width_violation(tmp_path):
    """A via3 (`via3.drawing`, 70/44) shape narrower than the 200 dbu
    (0.2 um) `via3.width.1` threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via3 = layout.layer(70, 44)
    layout.set_info(via3, kdb.LayerInfo(70, 44, "via3.drawing"))
    top.shapes(via3).insert(kdb.Box(0, 0, 100, 4000))  # 100 dbu < 200
    path = tmp_path / "via3_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"via3.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "via3.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "via3.drawing"


def test_run_drc_sky130_via3_space_violation(tmp_path):
    """Two via3 shapes closer than the 200 dbu `via3.space.1` threshold
    trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via3 = layout.layer(70, 44)
    layout.set_info(via3, kdb.LayerInfo(70, 44, "via3.drawing"))
    top.shapes(via3).insert(kdb.Box(0, 0, 300, 300))
    top.shapes(via3).insert(kdb.Box(400, 0, 700, 300))  # 100 dbu gap < 200
    path = tmp_path / "via3_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"via3.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "via3.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "via3.drawing"


def test_run_drc_sky130_met3_enclosing_via3_violation(tmp_path):
    """A via3 shape hanging off the edge of its met3 landing pad by less
    than the 60 dbu (0.06 um) `met3.enclosing.via3.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met3 = layout.layer(70, 20)
    layout.set_info(met3, kdb.LayerInfo(70, 20, "met3.drawing"))
    via3 = layout.layer(70, 44)
    layout.set_info(via3, kdb.LayerInfo(70, 44, "via3.drawing"))
    top.shapes(met3).insert(kdb.Box(0, 0, 1000, 1000))
    # 400 dbu margin on 3 sides, only 10 dbu (< 60) margin on the right.
    top.shapes(via3).insert(kdb.Box(400, 400, 990, 600))
    path = tmp_path / "met3_enclosing_via3_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met3.enclosing.via3.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met3.enclosing.via3.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "met3.drawing"


def test_run_drc_sky130_met4_enclosing_via3_violation(tmp_path):
    """A via3 shape hanging off the edge of its met4 landing pad by less
    than the 65 dbu (0.065 um) `met4.enclosing.via3.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met4 = layout.layer(71, 20)
    layout.set_info(met4, kdb.LayerInfo(71, 20, "met4.drawing"))
    via3 = layout.layer(70, 44)
    layout.set_info(via3, kdb.LayerInfo(70, 44, "via3.drawing"))
    top.shapes(met4).insert(kdb.Box(0, 0, 1000, 1000))
    # 400 dbu margin on 3 sides, only 10 dbu (< 65) margin on the right.
    top.shapes(via3).insert(kdb.Box(400, 400, 990, 600))
    path = tmp_path / "met4_enclosing_via3_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met4.enclosing.via3.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met4.enclosing.via3.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "met4.drawing"


def test_run_drc_sky130_met5_width_violation(tmp_path):
    """A met5 bar narrower than the 1600 dbu (1.6 um) `met5.width.1`
    threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met5 = layout.layer(72, 20)
    layout.set_info(met5, kdb.LayerInfo(72, 20, "met5.drawing"))
    top.shapes(met5).insert(kdb.Box(0, 0, 1000, 20000))  # 1000 dbu < 1600
    path = tmp_path / "met5_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met5.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met5.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "met5.drawing"


def test_run_drc_sky130_met5_space_violation(tmp_path):
    """Two met5 bars closer than the 1600 dbu `met5.space.1` threshold trip
    exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met5 = layout.layer(72, 20)
    layout.set_info(met5, kdb.LayerInfo(72, 20, "met5.drawing"))
    top.shapes(met5).insert(kdb.Box(0, 0, 4000, 20000))
    top.shapes(met5).insert(kdb.Box(5000, 0, 9000, 20000))  # 1000 dbu gap < 1600
    path = tmp_path / "met5_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met5.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met5.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "met5.drawing"


def test_run_drc_sky130_via4_width_violation(tmp_path):
    """A via4 (`via4.drawing`, 71/44) shape narrower than the 800 dbu
    (0.8 um) `via4.width.1` threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via4 = layout.layer(71, 44)
    layout.set_info(via4, kdb.LayerInfo(71, 44, "via4.drawing"))
    top.shapes(via4).insert(kdb.Box(0, 0, 400, 8000))  # 400 dbu < 800
    path = tmp_path / "via4_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"via4.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "via4.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "via4.drawing"


def test_run_drc_sky130_via4_space_violation(tmp_path):
    """Two via4 shapes closer than the 800 dbu `via4.space.1` threshold
    trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    via4 = layout.layer(71, 44)
    layout.set_info(via4, kdb.LayerInfo(71, 44, "via4.drawing"))
    top.shapes(via4).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(via4).insert(kdb.Box(1400, 0, 2400, 1000))  # 400 dbu gap < 800
    path = tmp_path / "via4_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"via4.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "via4.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "via4.drawing"


def test_run_drc_sky130_met4_enclosing_via4_violation(tmp_path):
    """A via4 shape hanging off the edge of its met4 landing pad by less
    than the 190 dbu (0.19 um) `met4.enclosing.via4.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met4 = layout.layer(71, 20)
    layout.set_info(met4, kdb.LayerInfo(71, 20, "met4.drawing"))
    via4 = layout.layer(71, 44)
    layout.set_info(via4, kdb.LayerInfo(71, 44, "via4.drawing"))
    top.shapes(met4).insert(kdb.Box(0, 0, 3000, 3000))
    # via4 is wide enough (900 >= 800) to avoid via4.width.1; 1200 dbu
    # margin on 3 sides, only 50 dbu (< 190) margin on the right.
    top.shapes(via4).insert(kdb.Box(1200, 1200, 2950, 2100))
    path = tmp_path / "met4_enclosing_via4_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met4.enclosing.via4.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met4.enclosing.via4.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "met4.drawing"


def test_run_drc_sky130_met5_enclosing_via4_violation(tmp_path):
    """A via4 shape hanging off the edge of its met5 landing pad by less
    than the 310 dbu (0.31 um) `met5.enclosing.via4.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met5 = layout.layer(72, 20)
    layout.set_info(met5, kdb.LayerInfo(72, 20, "met5.drawing"))
    via4 = layout.layer(71, 44)
    layout.set_info(via4, kdb.LayerInfo(71, 44, "via4.drawing"))
    top.shapes(met5).insert(kdb.Box(0, 0, 3000, 3000))
    # via4 is wide enough (900 >= 800) to avoid via4.width.1; 1200 dbu
    # margin on 3 sides, only 50 dbu (< 310) margin on the right.
    top.shapes(via4).insert(kdb.Box(1200, 1200, 2950, 2100))
    path = tmp_path / "met5_enclosing_via4_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met5.enclosing.via4.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met5.enclosing.via4.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "met5.drawing"


def test_run_drc_sky130_capm_width_violation(tmp_path):
    """A capm (met3 MiM-cap top plate, 89/44) bar narrower than the 1000 dbu
    (1.0 um) `capm.width.1` threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    capm = layout.layer(89, 44)
    layout.set_info(capm, kdb.LayerInfo(89, 44, "capm.drawing"))
    top.shapes(capm).insert(kdb.Box(0, 0, 500, 6000))  # 500 dbu < 1000
    path = tmp_path / "capm_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"capm.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "capm.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "capm.drawing"


def test_run_drc_sky130_capm_space_violation(tmp_path):
    """Two capm shapes closer than the 840 dbu (0.84 um) `capm.space.1`
    threshold trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    capm = layout.layer(89, 44)
    layout.set_info(capm, kdb.LayerInfo(89, 44, "capm.drawing"))
    top.shapes(capm).insert(kdb.Box(0, 0, 1200, 1200))
    top.shapes(capm).insert(kdb.Box(1600, 0, 2800, 1200))  # 400 dbu gap < 840
    path = tmp_path / "capm_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"capm.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "capm.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "capm.drawing"


def test_run_drc_sky130_met3_enclosing_capm_violation(tmp_path):
    """A capm shape hanging off the edge of its met3 bottom-plate by less
    than the 140 dbu (0.14 um) `met3.enclosing.capm.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met3 = layout.layer(70, 20)
    layout.set_info(met3, kdb.LayerInfo(70, 20, "met3.drawing"))
    capm = layout.layer(89, 44)
    layout.set_info(capm, kdb.LayerInfo(89, 44, "capm.drawing"))
    top.shapes(met3).insert(kdb.Box(0, 0, 3000, 3000))
    # capm is wide enough (1100 >= 1000) to avoid capm.width.1; 800 dbu
    # margin on 3 sides, only 50 dbu (< 140) margin on the right.
    top.shapes(capm).insert(kdb.Box(800, 800, 2950, 1900))
    path = tmp_path / "met3_enclosing_capm_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met3.enclosing.capm.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met3.enclosing.capm.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "met3.drawing"


def test_run_drc_sky130_capm_enclosing_via3_violation(tmp_path):
    """A via3 shape hanging off the edge of its capm top-plate by less than
    the 140 dbu (0.14 um) `capm.enclosing.via3.1` margin trips exactly one
    violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    capm = layout.layer(89, 44)
    layout.set_info(capm, kdb.LayerInfo(89, 44, "capm.drawing"))
    via3 = layout.layer(70, 44)
    layout.set_info(via3, kdb.LayerInfo(70, 44, "via3.drawing"))
    top.shapes(capm).insert(kdb.Box(0, 0, 2000, 2000))
    # 800 dbu margin on 3 sides, only 50 dbu (< 140) margin on the right.
    top.shapes(via3).insert(kdb.Box(800, 800, 1950, 1200))
    path = tmp_path / "capm_enclosing_via3_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"capm.enclosing.via3.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "capm.enclosing.via3.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "capm.drawing"


def test_run_drc_sky130_capm_separation_via3_violation(tmp_path):
    """A via3 shape that does not land on capm at all, but sits closer than
    the 140 dbu (0.14 um) `capm.separation.via3.1` threshold to its edge,
    trips exactly one violation. Unlike this deck's enclosing rules, the
    source rule is a plain, uncompounded two-layer check -- see
    `capm.separation.via3.1`'s own comment in sky130.py."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    capm = layout.layer(89, 44)
    layout.set_info(capm, kdb.LayerInfo(89, 44, "capm.drawing"))
    via3 = layout.layer(70, 44)
    layout.set_info(via3, kdb.LayerInfo(70, 44, "via3.drawing"))
    top.shapes(capm).insert(kdb.Box(0, 0, 1000, 1000))
    # via3 is wide enough (250 >= 200) to avoid via3.width.1, and sits
    # entirely outside capm with only a 50 dbu (< 140) gap.
    top.shapes(via3).insert(kdb.Box(1050, 0, 1300, 1000))
    path = tmp_path / "capm_separation_via3_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"capm.separation.via3.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "capm.separation.via3.1"
    assert violation["check"] == "separation"
    assert violation["layer"] == "capm.drawing"


def test_run_drc_sky130_capm_separation_via3_clean(tmp_path):
    """The same layout as the violation test above, but with the via3 gap
    widened to >= 140 dbu, passes `capm.separation.via3.1`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    capm = layout.layer(89, 44)
    layout.set_info(capm, kdb.LayerInfo(89, 44, "capm.drawing"))
    via3 = layout.layer(70, 44)
    layout.set_info(via3, kdb.LayerInfo(70, 44, "via3.drawing"))
    top.shapes(capm).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(via3).insert(kdb.Box(1200, 0, 1450, 1000))  # 200 dbu gap >= 140
    path = tmp_path / "capm_separation_via3_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sky130_capm2_width_violation(tmp_path):
    """A capm2 (met4 MiM-cap top plate, 97/44) bar narrower than the 1000
    dbu (1.0 um) `capm2.width.1` threshold trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    capm2 = layout.layer(97, 44)
    layout.set_info(capm2, kdb.LayerInfo(97, 44, "capm2.drawing"))
    top.shapes(capm2).insert(kdb.Box(0, 0, 500, 6000))  # 500 dbu < 1000
    path = tmp_path / "capm2_width_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"capm2.width.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "capm2.width.1"
    assert violation["check"] == "width"
    assert violation["layer"] == "capm2.drawing"


def test_run_drc_sky130_capm2_space_violation(tmp_path):
    """Two capm2 shapes closer than the 840 dbu (0.84 um) `capm2.space.1`
    threshold trip exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    capm2 = layout.layer(97, 44)
    layout.set_info(capm2, kdb.LayerInfo(97, 44, "capm2.drawing"))
    top.shapes(capm2).insert(kdb.Box(0, 0, 1200, 1200))
    top.shapes(capm2).insert(kdb.Box(1600, 0, 2800, 1200))  # 400 dbu gap < 840
    path = tmp_path / "capm2_space_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"capm2.space.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "capm2.space.1"
    assert violation["check"] == "space"
    assert violation["layer"] == "capm2.drawing"


def test_run_drc_sky130_met4_enclosing_capm2_violation(tmp_path):
    """A capm2 shape hanging off the edge of its met4 bottom-plate by less
    than the 140 dbu (0.14 um) `met4.enclosing.capm2.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    met4 = layout.layer(71, 20)
    layout.set_info(met4, kdb.LayerInfo(71, 20, "met4.drawing"))
    capm2 = layout.layer(97, 44)
    layout.set_info(capm2, kdb.LayerInfo(97, 44, "capm2.drawing"))
    top.shapes(met4).insert(kdb.Box(0, 0, 3000, 3000))
    # capm2 is wide enough (1100 >= 1000) to avoid capm2.width.1; 800 dbu
    # margin on 3 sides, only 50 dbu (< 140) margin on the right.
    top.shapes(capm2).insert(kdb.Box(800, 800, 2950, 1900))
    path = tmp_path / "met4_enclosing_capm2_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met4.enclosing.capm2.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "met4.enclosing.capm2.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "met4.drawing"


def test_run_drc_sky130_capm2_enclosing_via4_violation(tmp_path):
    """A via4 shape hanging off the edge of its capm2 top-plate by less
    than the 200 dbu (0.2 um) `capm2.enclosing.via4.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    capm2 = layout.layer(97, 44)
    layout.set_info(capm2, kdb.LayerInfo(97, 44, "capm2.drawing"))
    via4 = layout.layer(71, 44)
    layout.set_info(via4, kdb.LayerInfo(71, 44, "via4.drawing"))
    top.shapes(capm2).insert(kdb.Box(0, 0, 3000, 3000))
    # 1000 dbu margin on 3 sides, only 50 dbu (< 200) margin on the right.
    top.shapes(via4).insert(kdb.Box(1000, 1000, 2950, 1900))
    path = tmp_path / "capm2_enclosing_via4_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"capm2.enclosing.via4.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "capm2.enclosing.via4.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "capm2.drawing"


def test_run_drc_sky130_capm2_separation_via4_violation(tmp_path):
    """A via4 shape that does not land on capm2 at all, but sits closer
    than the 200 dbu (0.2 um) `capm2.separation.via4.1` threshold to its
    edge, trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    capm2 = layout.layer(97, 44)
    layout.set_info(capm2, kdb.LayerInfo(97, 44, "capm2.drawing"))
    via4 = layout.layer(71, 44)
    layout.set_info(via4, kdb.LayerInfo(71, 44, "via4.drawing"))
    top.shapes(capm2).insert(kdb.Box(0, 0, 1000, 1000))
    # via4 is wide enough (900 >= 800) to avoid via4.width.1, and sits
    # entirely outside capm2 with only a 100 dbu (< 200) gap.
    top.shapes(via4).insert(kdb.Box(1100, 0, 2000, 900))
    path = tmp_path / "capm2_separation_via4_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"capm2.separation.via4.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "capm2.separation.via4.1"
    assert violation["check"] == "separation"
    assert violation["layer"] == "capm2.drawing"


def test_run_drc_sky130_upper_stack_layers_now_covered(tmp_path):
    """The issue's own reproducer (#776): a layout drawing well-formed
    met3-met5/via2-via4/capm/capm2 geometry -- previously invisible to
    `DECK` -- now shows up in `coverage.layers_checked` and no longer
    appears in `coverage.layers_in_stream_without_rules`, with a genuine
    `status: clean` verdict (not a false "clean" from an empty rule set)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def _layer(num, datatype, name):
        idx = layout.layer(num, datatype)
        layout.set_info(idx, kdb.LayerInfo(num, datatype, name))
        return idx

    met3 = _layer(70, 20, "met3.drawing")
    met4 = _layer(71, 20, "met4.drawing")
    met5 = _layer(72, 20, "met5.drawing")
    via2 = _layer(69, 44, "via2.drawing")
    via3 = _layer(70, 44, "via3.drawing")
    via4 = _layer(71, 44, "via4.drawing")
    capm = _layer(89, 44, "capm.drawing")
    capm2 = _layer(97, 44, "capm2.drawing")

    # Generous shared metal footprint -- easily satisfies every width rule.
    top.shapes(met3).insert(kdb.Box(0, 0, 4000, 4000))
    top.shapes(met4).insert(kdb.Box(0, 0, 4000, 4000))
    top.shapes(met5).insert(kdb.Box(0, 0, 4000, 4000))

    # Upper-right quadrant: via2/via3 landing under a capm top plate, all
    # margins comfortably >= every applicable enclosure/separation
    # threshold (max 140 dbu).
    top.shapes(via2).insert(kdb.Box(2700, 2700, 3100, 3100))
    top.shapes(via3).insert(kdb.Box(2700, 2700, 3100, 3100))
    top.shapes(capm).insert(kdb.Box(2300, 2300, 3800, 3800))

    # Lower-left quadrant: via4 landing under a capm2 top plate, all
    # margins comfortably >= every applicable enclosure/separation
    # threshold (max 310 dbu).
    top.shapes(via4).insert(kdb.Box(700, 700, 1600, 1600))
    top.shapes(capm2).insert(kdb.Box(200, 200, 1900, 1900))

    path = tmp_path / "upper_stack_covered.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    checked_layers = [
        "70/20",  # met3.drawing
        "71/20",  # met4.drawing
        "72/20",  # met5.drawing
        "69/44",  # via2.drawing
        "70/44",  # via3.drawing
        "71/44",  # via4.drawing
        "89/44",  # capm.drawing
        "97/44",  # capm2.drawing
    ]
    for layer in checked_layers:
        assert layer in report["coverage"]["layers_checked"], layer
        assert layer not in report["coverage"]["layers_in_stream_without_rules"], layer


def test_sky130_met3_met4_met5_via2_via3_via4_have_drc_width_and_space_coverage():
    """Narrow instance of the issue's own "more generally" invariant --
    mirroring the #513 structural test above, `test_sky130_met2_via_
    extraction_levels_have_drc_width_and_space_coverage` -- for the
    connectivity levels this issue adds DRC coverage for: met3/met4/met5
    and via2/via3/via4, all already in `EXTRACTION_DECK.metals`/`.vias` as
    of #619, each have at least one `"width"` and one `"space"` rule in
    `DECK`."""
    deck = get_deck("sky130")
    extraction = get_extraction_deck("sky130")

    new_metal_and_via_layers = (
        (70, 20),  # met3.drawing
        (71, 20),  # met4.drawing
        (72, 20),  # met5.drawing
        (69, 44),  # via2.drawing
        (70, 44),  # via3.drawing
        (71, 44),  # via4.drawing
    )
    for layer in new_metal_and_via_layers:
        assert layer in extraction.metals or layer in extraction.vias

    width_layers = {rule.layer for rule in deck if rule.check == "width"}
    space_layers = {rule.layer for rule in deck if rule.check == "space"}

    for layer in new_metal_and_via_layers:
        assert layer in width_layers, f"{layer} has no width rule in DECK"
        assert layer in space_layers, f"{layer} has no space rule in DECK"


def test_sky130_capm_capm2_have_at_least_one_drc_rule():
    """`EXTRACTION_DECK.capacitors` recognises `capm`/`capm2` (89/44,
    97/44) as genuine MiM-cap device marks (#225) -- this asserts `DECK`
    now has at least one rule referencing each, closing the gap #776
    reports (previously zero)."""
    deck = get_deck("sky130")
    extraction = get_extraction_deck("sky130")

    capacitor_top_plates = {cap.top_plate for cap in extraction.capacitors}
    assert (89, 44) in capacitor_top_plates  # capm.drawing
    assert (97, 44) in capacitor_top_plates  # capm2.drawing

    rule_layers = {rule.layer for rule in deck} | {
        rule.other_layer for rule in deck if rule.other_layer is not None
    }
    for layer in capacitor_top_plates:
        assert layer in rule_layers, f"{layer} has no rule of any kind in DECK"


# --------------------------------------------------------------------------- #
# "area" / "density" / "antenna" check kinds (issue #812)
#
# Neither shipped deck (`sky130`/`gf180mcu`) authors a rule of any of these
# three kinds yet -- that's explicitly out of scope for this issue (a
# separate follow-on). Each is exercised here against a minimal synthetic
# one-rule deck via monkeypatch, mirroring
# `test_run_drc_synthetic_enclosed_check_flags_zero_overlap` above.
# --------------------------------------------------------------------------- #


def _patch_synthetic_deck(monkeypatch, rules):
    """Wire `run_drc("synthetic", ...)` to a synthetic one-off `rules` list,
    the same monkeypatch triple `test_run_drc_synthetic_enclosed_check_
    flags_zero_overlap` above uses -- `get_deck`/`get_nominal_dbu`/
    `get_layer_names`, all keyed off the arbitrary deck name `"synthetic"`."""
    monkeypatch.setattr("klayout_tools.drc.get_deck", lambda name: rules)
    monkeypatch.setattr("klayout_tools.drc.get_nominal_dbu", lambda name: 0.001)
    monkeypatch.setattr("klayout_tools.drc.get_layer_names", lambda name: {})


def test_run_drc_synthetic_area_check_violation(tmp_path, monkeypatch):
    """`check="area"`: a polygon smaller than `area_min_dbu2` is reported --
    driven by `Region.with_area(..., inverse=True)`, which returns the
    violating polygon directly (a `Region`, not `EdgePairs`)."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="metal.area.1",
                description="synthetic: minimum metal polygon area",
                layer=(30, 0),
                check="area",
                threshold_dbu=0,  # unused by "area"
                area_min_dbu2=10_000,  # (100 dbu)^2
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal = layout.layer(30, 0)
    # 50 x 50 dbu = 2500 dbu^2 < 10_000 dbu^2 threshold.
    top.shapes(metal).insert(kdb.Box(0, 0, 50, 50))
    path = tmp_path / "area_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert report["status"] == "violations"
    assert report["rule_counts"]["metal.area.1"] == 1
    (violation,) = report["violations"]
    assert violation["check"] == "area"
    assert violation["bbox"] == {"left": 0, "bottom": 0, "right": 50, "top": 50}
    assert violation["polygon"] is not None


def test_run_drc_synthetic_area_check_clean(tmp_path, monkeypatch):
    """`check="area"`: a polygon at/above `area_min_dbu2` is not reported."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="metal.area.1",
                description="synthetic: minimum metal polygon area",
                layer=(30, 0),
                check="area",
                threshold_dbu=0,
                area_min_dbu2=10_000,
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal = layout.layer(30, 0)
    # 200 x 200 dbu = 40_000 dbu^2 >= 10_000 dbu^2 threshold.
    top.shapes(metal).insert(kdb.Box(0, 0, 200, 200))
    path = tmp_path / "area_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_area_check_requires_min_or_max(tmp_path, monkeypatch):
    """A `check="area"` rule with neither `area_min_dbu2` nor
    `area_max_dbu2` set can never detect anything -- `run_drc` raises
    `DrcError` rather than silently reporting `status: "clean"`."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="metal.area.1",
                description="synthetic: misconfigured area rule",
                layer=(30, 0),
                check="area",
                threshold_dbu=0,
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal = layout.layer(30, 0)
    top.shapes(metal).insert(kdb.Box(0, 0, 50, 50))
    path = tmp_path / "area_misconfigured.gds"
    layout.write(str(path))

    with pytest.raises(DrcError, match="area_min_dbu2"):
        run_drc(str(path), "synthetic")


def test_run_drc_synthetic_density_check_violation(tmp_path, monkeypatch):
    """`check="density"`: two adjacent 1 um x 1 um windows, each covered
    only 30% (below `density_min=0.5`), are both reported -- one violation
    per under-dense window, tiled from the checked layer's own drawn extent
    (`region.bbox()`)."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="metal.density.1",
                description="synthetic: minimum metal fill density",
                layer=(30, 0),
                check="density",
                threshold_dbu=0,
                density_window_um=1.0,
                density_min=0.5,
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal = layout.layer(30, 0)
    # Window 1 is [0, 1000) x [0, 1000); window 2 is [1000, 2000) x [0, 1000)
    # (1 um = 1000 dbu at this layout's dbu). Each shape covers exactly
    # 300 x 1000 = 300_000 dbu^2 of its own window's 1_000_000 dbu^2 -- 30%,
    # below the 50% floor -- while together their union bbox spans exactly
    # the two windows (0..2000 x 0..1000), so both windows get tiled.
    top.shapes(metal).insert(kdb.Box(0, 0, 300, 1000))
    top.shapes(metal).insert(kdb.Box(1700, 0, 2000, 1000))
    path = tmp_path / "density_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert report["status"] == "violations"
    assert report["rule_counts"]["metal.density.1"] == 2
    bboxes = {
        (v["bbox"]["left"], v["bbox"]["bottom"], v["bbox"]["right"], v["bbox"]["top"])
        for v in report["violations"]
    }
    assert bboxes == {(0, 0, 1000, 1000), (1000, 0, 2000, 1000)}
    for v in report["violations"]:
        assert v["check"] == "density"
        assert v["polygon"] == [
            [v["bbox"]["left"], v["bbox"]["bottom"]],
            [v["bbox"]["right"], v["bbox"]["bottom"]],
            [v["bbox"]["right"], v["bbox"]["top"]],
            [v["bbox"]["left"], v["bbox"]["top"]],
        ]


def test_run_drc_synthetic_density_check_clean(tmp_path, monkeypatch):
    """`check="density"`: the same two-window layout, each now covered 60%
    (at/above `density_min=0.5`), reports no violations."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="metal.density.1",
                description="synthetic: minimum metal fill density",
                layer=(30, 0),
                check="density",
                threshold_dbu=0,
                density_window_um=1.0,
                density_min=0.5,
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal = layout.layer(30, 0)
    # 600 x 1000 = 600_000 dbu^2 in each window's 1_000_000 dbu^2 -- 60%.
    top.shapes(metal).insert(kdb.Box(0, 0, 600, 1000))
    top.shapes(metal).insert(kdb.Box(1400, 0, 2000, 1000))
    path = tmp_path / "density_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_density_check_requires_window_size(tmp_path, monkeypatch):
    """A `check="density"` rule missing `density_window_um` raises
    `DrcError` rather than silently checking nothing."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="metal.density.1",
                description="synthetic: misconfigured density rule",
                layer=(30, 0),
                check="density",
                threshold_dbu=0,
                density_min=0.5,
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    metal = layout.layer(30, 0)
    top.shapes(metal).insert(kdb.Box(0, 0, 600, 1000))
    path = tmp_path / "density_misconfigured.gds"
    layout.write(str(path))

    with pytest.raises(DrcError, match="density_window_um"):
        run_drc(str(path), "synthetic")


def test_run_drc_synthetic_antenna_check_violation(tmp_path, monkeypatch):
    """`check="antenna"`: a flat, connectivity-free area-ratio approximation
    -- `layer`'s total merged area over `other_layer`'s exceeds
    `antenna_ratio_max` -- reports one violation for the whole cell."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="poly.antenna.1",
                description="synthetic: gate-to-metal antenna ratio",
                layer=(50, 0),  # the accumulating "antenna" layer
                other_layer=(60, 0),  # the protecting/reference layer
                check="antenna",
                threshold_dbu=0,
                antenna_ratio_max=2.0,
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    antenna = layout.layer(50, 0)
    reference = layout.layer(60, 0)
    # 100 x 100 = 10_000 dbu^2 antenna area vs. 10 x 100 = 1_000 dbu^2
    # reference area -- ratio 10.0 > 2.0.
    top.shapes(antenna).insert(kdb.Box(0, 0, 100, 100))
    top.shapes(reference).insert(kdb.Box(200, 0, 210, 100))
    path = tmp_path / "antenna_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert report["status"] == "violations"
    assert report["rule_counts"]["poly.antenna.1"] == 1
    (violation,) = report["violations"]
    assert violation["check"] == "antenna"
    assert violation["bbox"] == {"left": 0, "bottom": 0, "right": 100, "top": 100}
    assert violation["polygon"] is None


def test_run_drc_synthetic_antenna_check_clean(tmp_path, monkeypatch):
    """`check="antenna"`: an area ratio at/below `antenna_ratio_max`
    reports no violation."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="poly.antenna.1",
                description="synthetic: gate-to-metal antenna ratio",
                layer=(50, 0),
                other_layer=(60, 0),
                check="antenna",
                threshold_dbu=0,
                antenna_ratio_max=2.0,
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    antenna = layout.layer(50, 0)
    reference = layout.layer(60, 0)
    # 100 x 100 = 10_000 dbu^2 antenna area vs. 100 x 100 = 10_000 dbu^2
    # reference area -- ratio 1.0 <= 2.0.
    top.shapes(antenna).insert(kdb.Box(0, 0, 100, 100))
    top.shapes(reference).insert(kdb.Box(200, 0, 300, 100))
    path = tmp_path / "antenna_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_synthetic_antenna_check_zero_reference_area_is_violation(
    tmp_path, monkeypatch
):
    """`check="antenna"`: antenna-layer area with *zero* reference-layer
    area anywhere in the cell is an undefined/infinite ratio, always worse
    than any finite `antenna_ratio_max` -- reported as a violation rather
    than skipped or silently treated as a clean run."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="poly.antenna.1",
                description="synthetic: gate-to-metal antenna ratio",
                layer=(50, 0),
                other_layer=(60, 0),
                check="antenna",
                threshold_dbu=0,
                antenna_ratio_max=2.0,
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    antenna = layout.layer(50, 0)
    reference = layout.layer(60, 0)
    top.shapes(antenna).insert(kdb.Box(0, 0, 100, 100))
    # A second, unrelated top cell carries the only reference-layer shape in
    # the stream -- keeps layer 60/0 registered in the written GDS (an empty
    # layer with zero shapes anywhere is dropped on write/read, verified
    # empirically), while `TOP`'s own `other_region` (scoped to `TOP`'s own
    # hierarchy) is still genuinely empty.
    other_top = layout.create_cell("OTHER")
    other_top.shapes(reference).insert(kdb.Box(0, 0, 100, 100))
    path = tmp_path / "antenna_zero_reference.gds"
    layout.write(str(path))

    report = run_drc(str(path), "synthetic")

    assert report["status"] == "violations"
    assert report["rule_counts"]["poly.antenna.1"] == 1
    (violation,) = report["violations"]
    assert violation["cell"] == "TOP"


def test_run_drc_antenna_check_requires_other_layer(tmp_path, monkeypatch):
    """A `check="antenna"` rule with no `other_layer` raises `DrcError`
    rather than crashing or silently reporting `status: "clean"` -- the
    same requirement `_run_check` enforces for every other two-layer check
    kind."""
    from klayout_tools.decks import DrcRule

    _patch_synthetic_deck(
        monkeypatch,
        [
            DrcRule(
                id="poly.antenna.1",
                description="synthetic: misconfigured antenna rule",
                layer=(50, 0),
                check="antenna",
                threshold_dbu=0,
                antenna_ratio_max=2.0,
            )
        ],
    )
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    antenna = layout.layer(50, 0)
    top.shapes(antenna).insert(kdb.Box(0, 0, 100, 100))
    path = tmp_path / "antenna_misconfigured.gds"
    layout.write(str(path))

    with pytest.raises(DrcError, match="other_layer"):
        run_drc(str(path), "synthetic")


# --------------------------------------------------------------------------- #
# sg13g2 (Epic #711 Phase 3b, issue #905)
# --------------------------------------------------------------------------- #
#
# `sg13g2.py`'s 14 width/space rules already have full golden violate/clean
# coverage via `tests/golden_deck/sg13g2/manifest.json` (parametrized by
# `tests/test_golden_deck.py`, which also asserts their `provenance` is
# populated). The 5 rules below (`gatpoly.separation.activ.1`,
# `activ.enclosing.cont.1`, `gatpoly.enclosing.cont.1`,
# `metal1.enclosing.via1.1`, `metal2.enclosing.via2.1`) are `enclosing`/
# `separation` checks, out of that manifest's width/space-only scope (see its
# own README.md) -- mirroring how sky130/gf180mcu's own enclosing/separation
# rules are hand-tested directly in this module (e.g.
# `test_run_drc_sky130_met1_enclosing_via_violation` above).


def test_run_drc_sg13g2_every_rule_has_provenance():
    """Every one of sg13g2's 19 curated `DrcRule` entries carries a populated
    `provenance` citation (issue #905's own acceptance criterion: "every
    compiled rule cites its SG13G2 PDK source line") -- not just the 14
    width/space rules `tests/test_golden_deck.py`'s generic check already
    covers."""
    deck = get_deck("sg13g2")
    assert len(deck) == 19
    for rule in deck:
        assert rule.provenance is not None, f"sg13g2/{rule.id}: no provenance"
        assert rule.provenance.source_repo == "IHP-GmbH/IHP-Open-PDK"
        assert rule.provenance.source_path, f"sg13g2/{rule.id}: empty source_path"
        assert rule.provenance.rule_id, f"sg13g2/{rule.id}: empty rule_id"
        assert rule.provenance.commit == "5cccb161f7492697cfa52eb14dc03beb00bdca9e"


def test_run_drc_sg13g2_gatpoly_separation_activ_violation(tmp_path):
    """A GatPoly shape closer than the 70 dbu (0.07 um)
    `gatpoly.separation.activ.1` threshold to an unrelated (non-overlapping)
    Activ shape trips exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    gatpoly = layout.layer(5, 0)
    layout.set_info(gatpoly, kdb.LayerInfo(5, 0, "GatPoly.drawing"))
    activ = layout.layer(1, 0)
    layout.set_info(activ, kdb.LayerInfo(1, 0, "Activ.drawing"))
    top.shapes(gatpoly).insert(kdb.Box(0, 0, 500, 500))
    top.shapes(activ).insert(kdb.Box(550, 0, 1000, 500))  # 50 dbu gap < 70
    path = tmp_path / "sg13g2_gat_d_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"gatpoly.separation.activ.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "gatpoly.separation.activ.1"
    assert violation["check"] == "separation"
    assert violation["layer"] == "GatPoly.drawing"


def test_run_drc_sg13g2_gatpoly_separation_activ_clean(tmp_path):
    """A GatPoly shape spaced exactly at the 70 dbu threshold from an
    unrelated Activ shape passes."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    gatpoly = layout.layer(5, 0)
    layout.set_info(gatpoly, kdb.LayerInfo(5, 0, "GatPoly.drawing"))
    activ = layout.layer(1, 0)
    layout.set_info(activ, kdb.LayerInfo(1, 0, "Activ.drawing"))
    top.shapes(gatpoly).insert(kdb.Box(0, 0, 500, 500))
    top.shapes(activ).insert(kdb.Box(570, 0, 1000, 500))  # 70 dbu gap == threshold
    path = tmp_path / "sg13g2_gat_d_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sg13g2_gatpoly_over_activ_not_flagged_as_separation(tmp_path):
    """A GatPoly gate drawn directly over its own Activ (the ordinary
    "transistor gate" case) does not trip `gatpoly.separation.activ.1` --
    `separation_check` only measures the gap between *non-interacting*
    shapes, matching the real `Gat.d` rule's own `.sep()` semantics (see
    `sg13g2.py`'s own docstring note on this rule)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    gatpoly = layout.layer(5, 0)
    layout.set_info(gatpoly, kdb.LayerInfo(5, 0, "GatPoly.drawing"))
    activ = layout.layer(1, 0)
    layout.set_info(activ, kdb.LayerInfo(1, 0, "Activ.drawing"))
    top.shapes(activ).insert(kdb.Box(0, 0, 2000, 1000))  # W=1um active strip
    top.shapes(gatpoly).insert(kdb.Box(800, -200, 1200, 1200))  # crosses it, L=0.4um
    path = tmp_path / "sg13g2_gate_over_active.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert "gatpoly.separation.activ.1" not in report["rule_counts"]


def test_run_drc_sg13g2_activ_enclosing_cont_violation(tmp_path):
    """A Cont shape hanging off the edge of its Activ landing region by less
    than the 70 dbu (0.07 um) `activ.enclosing.cont.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    activ = layout.layer(1, 0)
    layout.set_info(activ, kdb.LayerInfo(1, 0, "Activ.drawing"))
    cont = layout.layer(6, 0)
    layout.set_info(cont, kdb.LayerInfo(6, 0, "Cont.drawing"))
    top.shapes(activ).insert(kdb.Box(0, 0, 1000, 1000))
    # 400 dbu margin on 3 sides, only 10 dbu (< 70) margin on the right.
    top.shapes(cont).insert(kdb.Box(400, 400, 990, 600))
    path = tmp_path / "sg13g2_activ_enclosing_cont_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"activ.enclosing.cont.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "activ.enclosing.cont.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "Activ.drawing"


def test_run_drc_sg13g2_activ_enclosing_cont_clean(tmp_path):
    """A Cont shape enclosed by its Activ landing region with >= 70 dbu
    margin on every side passes `activ.enclosing.cont.1`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    activ = layout.layer(1, 0)
    layout.set_info(activ, kdb.LayerInfo(1, 0, "Activ.drawing"))
    cont = layout.layer(6, 0)
    layout.set_info(cont, kdb.LayerInfo(6, 0, "Cont.drawing"))
    top.shapes(activ).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(cont).insert(kdb.Box(400, 400, 600, 600))  # 400 margin >= 70
    path = tmp_path / "sg13g2_activ_enclosing_cont_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sg13g2_gatpoly_enclosing_cont_violation(tmp_path):
    """A Cont shape hanging off the edge of its GatPoly landing region by
    less than the 70 dbu (0.07 um) `gatpoly.enclosing.cont.1` margin trips
    exactly one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    gatpoly = layout.layer(5, 0)
    layout.set_info(gatpoly, kdb.LayerInfo(5, 0, "GatPoly.drawing"))
    cont = layout.layer(6, 0)
    layout.set_info(cont, kdb.LayerInfo(6, 0, "Cont.drawing"))
    top.shapes(gatpoly).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(cont).insert(kdb.Box(400, 400, 990, 600))  # 10 dbu margin < 70
    path = tmp_path / "sg13g2_gatpoly_enclosing_cont_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"gatpoly.enclosing.cont.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "gatpoly.enclosing.cont.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "GatPoly.drawing"


def test_run_drc_sg13g2_gatpoly_enclosing_cont_clean(tmp_path):
    """A Cont shape enclosed by its GatPoly landing region with >= 70 dbu
    margin on every side passes `gatpoly.enclosing.cont.1`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    gatpoly = layout.layer(5, 0)
    layout.set_info(gatpoly, kdb.LayerInfo(5, 0, "GatPoly.drawing"))
    cont = layout.layer(6, 0)
    layout.set_info(cont, kdb.LayerInfo(6, 0, "Cont.drawing"))
    top.shapes(gatpoly).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(cont).insert(kdb.Box(400, 400, 600, 600))  # 400 margin >= 70
    path = tmp_path / "sg13g2_gatpoly_enclosing_cont_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sg13g2_metal1_enclosing_via1_violation(tmp_path):
    """A Via1 shape hanging off the edge of its Metal1 landing pad by less
    than the 10 dbu (0.01 um) `metal1.enclosing.via1.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal1 = layout.layer(8, 0)
    layout.set_info(metal1, kdb.LayerInfo(8, 0, "Metal1.drawing"))
    via1 = layout.layer(19, 0)
    layout.set_info(via1, kdb.LayerInfo(19, 0, "Via1.drawing"))
    top.shapes(metal1).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(via1).insert(kdb.Box(400, 400, 995, 600))  # 5 dbu margin < 10
    path = tmp_path / "sg13g2_metal1_enclosing_via1_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"metal1.enclosing.via1.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "metal1.enclosing.via1.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "Metal1.drawing"


def test_run_drc_sg13g2_metal1_enclosing_via1_clean(tmp_path):
    """A Via1 shape enclosed by its Metal1 landing pad with >= 10 dbu margin
    on every side passes `metal1.enclosing.via1.1`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal1 = layout.layer(8, 0)
    layout.set_info(metal1, kdb.LayerInfo(8, 0, "Metal1.drawing"))
    via1 = layout.layer(19, 0)
    layout.set_info(via1, kdb.LayerInfo(19, 0, "Via1.drawing"))
    top.shapes(metal1).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(via1).insert(kdb.Box(400, 400, 600, 600))  # 400 margin >= 10
    path = tmp_path / "sg13g2_metal1_enclosing_via1_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sg13g2_metal2_enclosing_via2_violation(tmp_path):
    """A Via2 shape hanging off the edge of its Metal2 landing pad by less
    than the 5 dbu (0.005 um) `metal2.enclosing.via2.1` margin trips exactly
    one violation."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal2 = layout.layer(10, 0)
    layout.set_info(metal2, kdb.LayerInfo(10, 0, "Metal2.drawing"))
    via2 = layout.layer(29, 0)
    layout.set_info(via2, kdb.LayerInfo(29, 0, "Via2.drawing"))
    top.shapes(metal2).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(via2).insert(kdb.Box(400, 400, 998, 600))  # 2 dbu margin < 5
    path = tmp_path / "sg13g2_metal2_enclosing_via2_violation.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"metal2.enclosing.via2.1": 1}
    (violation,) = report["violations"]
    assert violation["rule"] == "metal2.enclosing.via2.1"
    assert violation["check"] == "enclosing"
    assert violation["layer"] == "Metal2.drawing"


def test_run_drc_sg13g2_metal2_enclosing_via2_clean(tmp_path):
    """A Via2 shape enclosed by its Metal2 landing pad with >= 5 dbu margin
    on every side passes `metal2.enclosing.via2.1`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    metal2 = layout.layer(10, 0)
    layout.set_info(metal2, kdb.LayerInfo(10, 0, "Metal2.drawing"))
    via2 = layout.layer(29, 0)
    layout.set_info(via2, kdb.LayerInfo(29, 0, "Via2.drawing"))
    top.shapes(metal2).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(via2).insert(kdb.Box(400, 400, 600, 600))  # 400 margin >= 5
    path = tmp_path / "sg13g2_metal2_enclosing_via2_clean.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0


def test_run_drc_sg13g2_coverage_deck_scope_matches_rule_scopes(tmp_path):
    """`coverage.deck_scope` (#566) is every distinct non-empty `DrcRule.scope`
    across the sg13g2 deck's rules, deduplicated and sorted -- mirrors
    `test_run_drc_coverage_deck_scope_matches_sky130_rule_scopes` above."""
    layout = kdb.Layout()
    layout.create_cell("TOP")
    path = tmp_path / "sg13g2_empty.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")

    expected_scope = sorted({rule.scope for rule in get_deck("sg13g2") if rule.scope})
    assert expected_scope
    assert report["coverage"]["deck_scope"] == expected_scope


def test_run_drc_sg13g2_unmodeled_voltage_marker_registered():
    """sg13g2 registers `ThickGateOx` (44/0) as an unmodeled voltage-domain
    marker (issue #552's mechanism, mirroring gf180mcu's `Dualgate`) -- see
    `sg13g2.py`'s own `UNMODELED_VOLTAGE_MARKERS` docstring note."""
    markers = get_unmodeled_voltage_markers("sg13g2")
    assert (44, 0) in markers
    assert "ThickGateOx" in markers[(44, 0)]


def test_sg13g2_extraction_deck_declares_mos_recognition():
    """sg13g2's `EXTRACTION_DECK` declares the curated `active`/`poly`/
    `nwell`/`contact` MOS-recognition layers and a two-level Metal1/Metal2
    connectivity stack joined by Via1 -- the same fields
    `test_golden_pair_sg13g2_*` in `tests/test_sg13g2_deck.py` exercise
    end-to-end."""
    deck = get_extraction_deck("sg13g2")
    assert deck.active == (1, 0)
    assert deck.poly == (5, 0)
    assert deck.nwell == (31, 0)
    assert deck.contact == (6, 0)
    assert deck.metals == ((8, 0), (10, 0))
    assert deck.vias == ((19, 0),)
    assert deck.tap is None
    assert deck.device_classes == ("nfet", "pfet")
