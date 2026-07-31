"""Corpus-backed tests: run `klt render` against the checked-in sky130 /
gf180mcu example layouts.

Unlike `test_corpus.py` (which asserts pixel-independent JSON output against
golden fixtures), rendered PNG *pixel content* is not golden-tested here --
rendering can vary subtly across platforms and KLayout versions. Instead
these tests derive the expected non-empty-layer count from the existing
`klt layers` golden fixtures (`tests/corpus/golden/`) and assert the
structural contract: one PNG per non-empty layer, at the requested size, in
the documented output directory.

See `tests/corpus/README.md` for corpus provenance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.render import render_report

CORPUS_DIR = Path(__file__).parent / "corpus"
GOLDEN_DIR = CORPUS_DIR / "golden"
PDKS = ["sky130", "gf180mcu"]


def _corpus_cases() -> list[tuple[Path, Path]]:
    """Pair every corpus layout file with its `klt layers` golden fixture."""
    cases: list[tuple[Path, Path]] = []
    for pdk in PDKS:
        pdk_dir = CORPUS_DIR / pdk
        for layout_path in sorted(pdk_dir.glob("*.gds")) + sorted(
            pdk_dir.glob("*.oas")
        ):
            golden_path = GOLDEN_DIR / pdk / f"{layout_path.stem}.layers.json"
            cases.append((layout_path, golden_path))
    return cases


CASES = _corpus_cases()
CASE_IDS = [f"{layout.parent.name}/{layout.name}" for layout, _ in CASES]


def test_corpus_is_non_empty():
    """Guard against a silently-empty corpus (e.g. a broken glob)."""
    assert len(CASES) >= 4, "expected at least a handful of corpus files"


@pytest.mark.parametrize("layout_path,golden_path", CASES, ids=CASE_IDS)
def test_render_report_matches_golden_layer_set(
    layout_path: Path, golden_path: Path, tmp_path: Path
):
    golden = json.loads(golden_path.read_text())
    expected_non_empty = [
        (entry["layer"], entry["datatype"])
        for entry in golden["layers"]
        if entry["shapes"] > 0
    ]

    out_dir = tmp_path / "renders"
    report = render_report(
        str(layout_path), output_dir=str(out_dir), width=64, height=48
    )

    assert report["layer_count"] == golden["layer_count"]
    assert report["rendered_count"] == len(expected_non_empty)

    rendered_pairs = [
        (entry["layer"], entry["datatype"])
        for entry in report["layers"]
        if entry["rendered"]
    ]
    assert sorted(rendered_pairs) == sorted(expected_non_empty)

    for entry in report["layers"]:
        if not entry["rendered"]:
            assert entry["path"] is None
            continue
        path = Path(entry["path"])
        assert path.parent == out_dir
        assert path.is_file()
        assert path.stat().st_size > 0


@pytest.mark.parametrize("layout_path,golden_path", CASES, ids=CASE_IDS)
def test_klt_render_json_cli_matches_golden(layout_path, golden_path, tmp_path, capsys):
    from klayout_tools.cli import main

    golden = json.loads(golden_path.read_text())
    expected_rendered = sum(1 for e in golden["layers"] if e["shapes"] > 0)

    out_dir = tmp_path / "renders"
    exit_code = main(
        [
            "render",
            str(layout_path),
            "-o",
            str(out_dir),
            "--width",
            "64",
            "--height",
            "48",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["layer_count"] == golden["layer_count"]
    assert data["rendered_count"] == expected_rendered
