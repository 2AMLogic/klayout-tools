"""Corpus-backed tests: run `klt layers` against the checked-in sky130 /
gf180mcu example layouts and assert the output matches the golden JSON
fixture recorded under `tests/corpus/golden/`.

See `tests/corpus/README.md` for provenance (source repo, commit, license)
of each corpus file, and `tests/corpus/generate_golden.py` for how the
golden fixtures are (re)generated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.layers import layers_report

CORPUS_DIR = Path(__file__).parent / "corpus"
GOLDEN_DIR = CORPUS_DIR / "golden"
PDKS = ["sky130", "gf180mcu"]


def _corpus_cases() -> list[tuple[Path, Path]]:
    """Pair every corpus layout file with its golden fixture."""
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
    for pdk in PDKS:
        assert any(layout.parent.name == pdk for layout, _ in CASES), (
            f"no corpus files found for {pdk}"
        )


@pytest.mark.parametrize("layout_path,golden_path", CASES, ids=CASE_IDS)
def test_golden_fixture_exists(layout_path: Path, golden_path: Path):
    assert layout_path.is_file(), f"missing corpus file: {layout_path}"
    assert golden_path.is_file(), (
        f"missing golden fixture for {layout_path.name}: {golden_path}. "
        "Run `python tests/corpus/generate_golden.py` to (re)generate it."
    )


@pytest.mark.parametrize("layout_path,golden_path", CASES, ids=CASE_IDS)
def test_layers_report_matches_golden(layout_path: Path, golden_path: Path):
    """`layers_report()` output matches the golden fixture.

    The golden's `file` field records the repo-relative path for readability
    (see `generate_golden.py`); it is re-keyed here to whatever path this
    test actually passed in, since it is an echo of the caller's argument,
    not layout-derived content.
    """
    golden = json.loads(golden_path.read_text())

    report = layers_report(str(layout_path))

    expected = {**golden, "file": str(layout_path)}
    assert report == expected


@pytest.mark.parametrize("layout_path,golden_path", CASES, ids=CASE_IDS)
def test_klt_layers_json_matches_golden(layout_path, golden_path, capsys):
    """The `klt layers --format json` CLI output matches the golden fixture."""
    golden = json.loads(golden_path.read_text())

    exit_code = main(["layers", str(layout_path), "--format", "json"])
    assert exit_code == 0

    out = json.loads(capsys.readouterr().out)
    expected = {**golden, "file": str(layout_path)}
    assert out == expected


@pytest.mark.parametrize("layout_path,golden_path", CASES, ids=CASE_IDS)
def test_klt_layers_text_runs_cleanly(layout_path, golden_path, capsys):
    """The default (text) `klt layers` output succeeds and is internally
    consistent with the golden layer count for every corpus file."""
    golden = json.loads(golden_path.read_text())

    exit_code = main(["layers", str(layout_path)])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert f"layers: {golden['layer_count']}" in out
