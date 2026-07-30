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
from klayout_tools.drc import DrcError, run_drc

# poly.width.1 (sky130 deck): minimum poly width is 150 dbu (0.15 um).
_POLY_WIDTH_THRESHOLD_DBU = 150

# poly2.width.1 (gf180mcu deck): minimum poly2 interconnect width is 180 dbu
# (0.18 um).
_GF180MCU_POLY2_WIDTH_THRESHOLD_DBU = 180

CORPUS_DIR = Path(__file__).parent / "corpus"
GF180MCU_CORPUS_FILES = sorted((CORPUS_DIR / "gf180mcu").glob("*.gds"))


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


def test_exit_code_clean(tmp_path):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    assert main(["drc", str(path), "--deck", "sky130", "--format", "json"]) == 0


def test_exit_code_violations(tmp_path):
    path = tmp_path / "violation.gds"
    _make_violation_layout().write(str(path))
    assert main(["drc", str(path), "--deck", "sky130", "--format", "json"]) == 3


def test_missing_file(tmp_path, capsys):
    missing = tmp_path / "nope.gds"
    assert main(["drc", str(missing), "--deck", "sky130"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "klt drc" in captured.err
    assert "not found" in captured.err


def test_non_layout_file(tmp_path, capsys):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("this is not a layout stream\n" * 4)

    assert main(["drc", str(bogus), "--deck", "sky130"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "klt drc" in captured.err


def test_unknown_deck(tmp_path, capsys):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))

    assert main(["drc", str(path), "--deck", "not-a-real-deck"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "klt drc" in captured.err
    assert "unknown deck" in captured.err


def test_missing_file_json_format(tmp_path, capsys):
    """`--format json` errors emit the documented JSON error envelope on
    stderr, leave stdout empty, and exit 1."""
    missing = tmp_path / "nope.gds"

    assert main(["drc", str(missing), "--deck", "sky130", "--format", "json"]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "drc"
    assert "not found" in error["error"]["message"]


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
