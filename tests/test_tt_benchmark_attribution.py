"""Drift guards for the published Tiny Tapeout benchmark (Epic #520, #8119).

``docs/guides/tinytapeout-benchmark.md`` is the harness README. These checks
fail when its attribution table, file hashes, pins, headline or reproduce
commands drift from the published data, and when any published design lacks
attribution.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "corpus"
GUIDE = ROOT / "docs" / "guides" / "tinytapeout-benchmark.md"
REPORT = ROOT / "docs" / "reports" / "tt-gf180mcu-corpus-run.md"
SAMPLE = "tt06_sky130_sample.json"
BASELINE = "tt_sky130_baseline.json"
RESULTS = "tt_sky130_optimized.json"
ATTRIBUTION = "tt06_sky130_attribution.json"
PUBLISHED = (
    SAMPLE,
    ATTRIBUTION,
    BASELINE,
    RESULTS,
    "tt_gf180_slots.tsv",
    "tt_gf0p2_drc_sample.json",
)
HEX40 = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"https://github\.com/[^/]+/[^/]+|git@github\.com:[^/]+/[^/]+")


def load(name: str) -> dict:
    return json.loads((CORPUS / name).read_text())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cells_of(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def table(text: str, *header: str) -> list[list[str]]:
    """Rows of the first markdown table whose header names every given cell."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("|") and all(name in cells_of(line) for name in header):
            rows = []
            for row in lines[index + 2 :]:
                if not row.startswith("|"):
                    break
                rows.append(cells_of(row))
            return rows
    raise AssertionError(f"no table with header cells {header}")


def leaves(value: object) -> list[str]:
    if isinstance(value, dict):
        return [item for member in value.values() for item in leaves(member)]
    return [value] if isinstance(value, str) else []


def headline(results: dict) -> str:
    """The same arithmetic and rendering as ``tt_corpus_equiv.run``."""
    reductions = [
        100 * (row["baseline_count"] - row["optimized_count"]) / row["baseline_count"]
        for row in results["designs"]
        if "optimized_count" in row
    ]
    average = mean(reductions) if reductions else None
    value = f"{average:g}%" if average is not None else "unavailable"
    excluded = len(results["designs"]) - len(reductions)
    return f"mean {value} across {len(reductions)} designs, {excluded} excluded"


def test_every_published_phase2_design_has_attribution():
    designs = load(ATTRIBUTION)["designs"]
    credit = {row["macro"]: row for row in designs}
    macros = [row["macro"] for row in designs]
    assert len(credit) == len(designs)
    assert all(row["author"].strip() and row["title"].strip() for row in designs)
    for name, key in (
        (SAMPLE, "projects"),
        (BASELINE, "designs"),
        (RESULTS, "designs"),
    ):
        published = load(name)[key]
        assert [row["macro"] for row in published] == macros, name
        for row in published:
            assert (credit[row["macro"]]["repo"], credit[row["macro"]]["commit"]) == (
                row["repo"],
                row["commit"],
            ), (name, row["macro"])
    for name in (BASELINE, RESULTS):
        assert all(row["license"]["spdx"] for row in load(name)["designs"]), name


def test_guide_attribution_table_matches_the_attribution_file():
    rows = table(GUIDE.read_text(), "Design", "Author(s)")
    designs = load(ATTRIBUTION)["designs"]
    licenses = {
        row["macro"]: row["license"]["spdx"] for row in load(BASELINE)["designs"]
    }
    assert len(rows) == len(designs)
    for cells, design in zip(rows, designs, strict=True):
        macro, title, author, repo, commit, spdx = cells
        assert macro == f"`{design['macro']}`"
        assert (title, author, commit.strip("`")) == (
            design["title"],
            design["author"],
            design["commit"],
        )
        link = re.fullmatch(r"\[[^\]]+\]\((https://github\.com/[^)]+)\)", repo)
        assert link and link.group(1) == design["repo"]
        assert spdx == licenses[design["macro"]]
    guide = GUIDE.read_text()
    index = load(ATTRIBUTION)["source_index"]
    assert index["commit"] in guide and index["sha256"] in guide


def test_guide_hashes_match_the_published_files():
    rows = table(GUIDE.read_text(), "File", "SHA-256")
    assert {path.strip("`") for path, _, _ in rows} == {
        f"tests/corpus/{name}" for name in PUBLISHED
    }
    for path, _, digest in rows:
        file = ROOT / path.strip("`")
        assert file.is_file(), path
        assert sha256(file) == digest.strip("`"), path


def test_guide_states_the_published_pins():
    guide = GUIDE.read_text()
    baseline = load(BASELINE)
    assert baseline["pins"] == load(RESULTS)["pins"]
    for pin in (*leaves(baseline["pins"]), *leaves(load(RESULTS)["optimization"])):
        assert pin.removeprefix("sha256:") in guide, pin


def test_guide_headline_and_counts_match_the_published_results():
    results = load(RESULTS)
    assert headline(results) in GUIDE.read_text()
    proven = [row for row in results["designs"] if "optimized_count" in row]
    assert (results["reported"], results["excluded"]) == (
        len(proven),
        len(results["designs"]) - len(proven),
    )
    rows = table(GUIDE.read_text(), "Design", "Baseline", "Verdict")
    assert len(rows) == len(results["designs"])
    for cells, row in zip(rows, results["designs"], strict=True):
        macro, baseline, optimized, delta, verdict = cells
        assert macro == f"`{row['macro']}`"
        assert int(baseline) == row["baseline_count"]
        assert verdict == row["verdict"]
        if "optimized_count" in row:
            assert int(optimized) == row["optimized_count"]
            assert int(delta) == row["delta"] and delta.startswith("+")
        else:
            assert (optimized, delta) == ("–", "–")
            assert row["exclusion_reason"]


def test_guide_gives_the_commands_that_regenerate_the_published_files():
    guide = GUIDE.read_text()
    for command in (
        "scripts/tt_corpus_fetch.py --shuttle tt06 --pdk sky130A",
        "scripts/tt_corpus_baseline.py --output",
        "scripts/tt_corpus_optimize.py prepare --work-dir",
        "scripts/tt_corpus_equiv.py --pairs",
        "scripts/tt_corpus_optimize.py report --pairs",
    ):
        assert command in guide, command
        assert (ROOT / command.split()[0]).is_file(), command
    assert "cmp /tmp/tt-baseline.json tests/corpus/tt_sky130_baseline.json" in guide
    assert "cmp run/results.json tests/corpus/tt_sky130_optimized.json" in guide
    corpus_readme = (CORPUS / "README.md").read_text()
    assert "docs/guides/tinytapeout-benchmark.md" in corpus_readme
    docs_readme = (ROOT / "docs" / "README.md").read_text()
    assert "tinytapeout-benchmark.md" in docs_readme


def test_gf180_catalogue_attributes_every_measured_slot():
    with (CORPUS / "tt_gf180_slots.tsv").open() as stream:
        catalogue = {
            (row["shuttle"], row["macro"]): row
            for row in csv.DictReader(stream, delimiter="\t")
        }
    section = REPORT.read_text().split("## Per-design results", 1)[1]
    skipped, measured = set(), set()
    for cells in table(section, "Shuttle", "Design"):
        key = (cells[0], cells[1].strip("`"))
        (skipped if cells[2] == "skipped" else measured).add(key)
    assert measured | skipped == set(catalogue)
    assert not measured & skipped
    for key in measured:
        row = catalogue[key]
        assert REPO.fullmatch(row["repo"]), key
        assert HEX40.fullmatch(row["commit"]) and HEX40.fullmatch(
            row["license_blob"]
        ), key
        assert row["spdx"] and row["license_file"], key
    unattributed = {
        key
        for key, row in catalogue.items()
        if not (row["spdx"] and row["license_file"])
    }
    assert unattributed <= skipped
    licenses = Counter(row["spdx"] for row in catalogue.values())
    prose = " ".join(GUIDE.read_text().split())
    summary = (
        f"{licenses['Apache-2.0']} slots are Apache-2.0"
        f" and {licenses['AGPL-3.0']} are AGPL-3.0"
    )
    assert summary in prose
