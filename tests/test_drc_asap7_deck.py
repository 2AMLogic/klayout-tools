"""Tests for the in-repo ASAP7 DRC deck (issue #2760, phase 1: FEOL/MOL).

The deck is a KLayout DRC-DSL script (``src/klayout_tools/decks/asap7.drc``)
run through ``klt drc --engine klayout --deck-file``. Its rule ids are
declared once in :mod:`klayout_tools.decks.asap7_rules`.

Three tiers:

- static checks that the deck script, the declared id list, the per-rule
  fixtures and ``docs/cli/drc.md`` agree (no binary, no PDK);
- real-binary checks, gated on the ``klayout`` application binary like
  ``tests/test_drc_klayout_engine.py``: every generated fixture trips exactly
  its expected rule ids, the envelope is the standard ``klt drc`` one, and the
  drawing-scale convention holds;
- PDK checks, gated on ``scripts/fetch-pdks.sh`` having populated
  ``pdks/lambdapdk``: the deck's layer numbers match the pinned ``asap7.lyp``
  and the shipped standard cells are clean apart from documented deviations.

Every layout is generated into ``tmp_path``; nothing is written to the repo.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

import klayout.db as kdb
import pytest

from helpers import asap7_drc_fixtures as fx
from klayout_tools.cli import main
from klayout_tools.decks.asap7_rules import (
    DECK_PATH,
    NOT_IMPLEMENTED,
    RULE_IDS,
    RULES,
    SHIPPED_CELL_DEVIATIONS,
)
from klayout_tools.drc import run_drc_klayout_engine

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS = _REPO_ROOT / "docs" / "cli" / "drc.md"
_ASAP7_ROOT = _REPO_ROOT / "pdks" / "lambdapdk" / "lambdapdk" / "asap7"
_ASAP7_LYP = _ASAP7_ROOT / "base" / "setup" / "klayout" / "asap7.lyp"

HAVE_KLAYOUT_BINARY = shutil.which("klayout") is not None
_needs_klayout = pytest.mark.skipif(
    not HAVE_KLAYOUT_BINARY, reason="klayout binary is not installed on this machine"
)
_needs_asap7 = pytest.mark.skipif(
    not _ASAP7_LYP.is_file(),
    reason=(
        "pinned lambdapdk ASAP7 assets not found under pdks/lambdapdk -- "
        "run scripts/fetch-pdks.sh"
    ),
)

_DECK_LAYER_RE = re.compile(r"^(\w+)\s*=\s*input\((\d+),\s*(\d+)\)", re.M)
_LYP_ENTRY_RE = re.compile(r"<name>(\S+) drawing - (\d+)/(\d+)</name>")
_SELECT_FAMILY = ("NSELECT", "PSELECT", "SLVT", "LVT", "SRAMVT")
_CASES = fx.all_cases()


def _deck_text() -> str:
    return DECK_PATH.read_text(encoding="utf-8")


def _docs_asap7_section() -> str:
    text = _DOCS.read_text(encoding="utf-8")
    start = text.index("## ASAP7 deck")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else None]


# --------------------------------------------------------------------------- #
# Static: deck <-> declared ids <-> fixtures <-> docs
# --------------------------------------------------------------------------- #


def test_rule_ids_unique_and_disjoint_from_gaps():
    assert len(RULE_IDS) == len(set(RULE_IDS))
    gaps = [g.id for g in NOT_IMPLEMENTED]
    assert len(gaps) == len(set(gaps))
    assert not set(gaps) & set(RULE_IDS)


def test_every_rule_cites_a_drm_table():
    for rule in RULES:
        assert re.fullmatch(r"Table 3\.\d+\.\d", rule.table), rule
    for gap in NOT_IMPLEMENTED:
        assert re.fullmatch(r"Table 3\.\d+\.\d", gap.table), gap
        assert gap.reason and re.fullmatch(r"#\d+", gap.tracked_by), gap


def test_deck_declares_exactly_the_declared_ids():
    """Every literal ``output("<id>", "Table ...")`` in the script is a
    declared id with a DRM table citation, and every declared id is either
    literal in the script or produced by the section-3.7 layer loop."""
    text = _deck_text()
    literal = re.findall(r'output\("([^"#]+)",\s*"(Table [^"]*)"', text)
    literal_ids = [rule for rule, _ in literal]
    assert len(literal_ids) == len(set(literal_ids))
    assert set(literal_ids) <= set(RULE_IDS)
    templated = set(re.findall(r'output\("#\{name\}\.([^"]+)",\s*"Table', text))
    for name in _SELECT_FAMILY:
        assert f'"{name}" =>' in text
    expanded = {f"{name}.{suffix}" for name in _SELECT_FAMILY for suffix in templated}
    assert set(literal_ids) | expanded == set(RULE_IDS)
    assert not set(literal_ids) & expanded


def test_every_rule_has_a_fail_and_a_boundary_pass_fixture():
    fails = {c.rule for c in _CASES if c.kind == "fail"}
    passes = {c.rule for c in _CASES if c.kind == "pass"}
    assert fails == set(RULE_IDS)
    assert passes == set(RULE_IDS)
    for case in _CASES:
        assert case.expect <= set(RULE_IDS), case.name
        if case.kind == "fail":
            assert case.rule in case.expect, case.name
        else:
            assert case.rule not in case.expect, case.name


def test_docs_list_every_rule_and_gap():
    section = _docs_asap7_section()
    for rule_id in RULE_IDS:
        assert f"`{rule_id}`" in section, rule_id
    for gap in NOT_IMPLEMENTED:
        assert f"`{gap.id}`" in section, gap.id
    for cell in SHIPPED_CELL_DEVIATIONS:
        assert f"`{cell}`" in section, cell
    assert f"--expect-rule-categories {len(RULE_IDS)}" in section
    assert f"--expect-rule-categories {len(RULE_IDS)}" in _deck_text()


def test_fixture_layers_match_deck():
    deck = {
        var.upper(): int(layer)
        for var, layer, _ in _DECK_LAYER_RE.findall(_deck_text())
    }
    assert deck == fx.LAYERS


# --------------------------------------------------------------------------- #
# Real klayout binary
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory):
    if not HAVE_KLAYOUT_BINARY:
        pytest.skip("klayout binary is not installed on this machine")
    path = tmp_path_factory.mktemp("asap7_fixtures") / "cases.gds"
    fx.write_cases(str(path), _CASES)
    report = run_drc_klayout_engine(str(path), str(DECK_PATH))
    tripped: dict[int, set[str]] = defaultdict(set)
    for v in report["violations"]:
        tripped[fx.slot_of(v["bbox"])].add(v["rule"])
    return report, tripped


@_needs_klayout
@pytest.mark.parametrize("index", range(len(_CASES)), ids=[c.name for c in _CASES])
def test_fixture_trips_exactly_its_expected_rules(fixture_run, index):
    _, tripped = fixture_run
    assert tripped.get(index, set()) == set(_CASES[index].expect)


@_needs_klayout
def test_declared_rule_categories_match_manifest(fixture_run):
    report, _ = fixture_run
    assert sorted(set(report["coverage"]["rule_categories"])) == sorted(RULE_IDS)


def _run_cli(capsys, gds: Path, *extra: str) -> tuple[int, dict]:
    code = main(
        [
            "drc",
            str(gds),
            "--engine",
            "klayout",
            "--deck-file",
            str(DECK_PATH),
            *extra,
            "--format",
            "json",
        ]
    )
    return code, json.loads(capsys.readouterr().out)


@_needs_klayout
def test_cli_envelope_clean_device(tmp_path, capsys):
    gds = tmp_path / "device.gds"
    fx.write_shapes(str(gds), fx.device())
    code, payload = _run_cli(
        capsys, gds, "--expect-rule-categories", str(len(RULE_IDS))
    )
    assert code == 0
    assert payload["schema_version"]
    assert payload["engine"] == "klayout"
    assert payload["status"] == "clean"
    assert payload["violation_count"] == 0
    assert payload["coverage_assertion"]["satisfied"] is True


@_needs_klayout
def test_cli_envelope_reports_rule_id(tmp_path, capsys):
    gds = tmp_path / "narrow_gate.gds"
    fx.write_shapes(str(gds), fx.device(gate_y0=27 - 4 + fx.D))
    code, payload = _run_cli(
        capsys, gds, "--expect-rule-categories", str(len(RULE_IDS))
    )
    assert code == 3
    assert payload["status"] == "violations"
    assert {v["rule"] for v in payload["violations"]} == {"GATE.ACTIVE.EX.1"}
    assert set(payload["rule_counts"]) == {"GATE.ACTIVE.EX.1"}


@_needs_klayout
def test_drawn_scale_convention_is_1x(tmp_path):
    """The deck checks 1x layouts. The same clean device drawn at 4x (the
    convention some ASAP7 distributions use) fails the exact-width rules
    wholesale instead of passing silently."""
    one = tmp_path / "device_1x.gds"
    four = tmp_path / "device_4x.gds"
    fx.write_shapes(str(one), fx.device(), scale=1.0)
    fx.write_shapes(str(four), fx.device(), scale=4.0)
    assert run_drc_klayout_engine(str(one), str(DECK_PATH))["violations"] == []
    rules_4x = {
        v["rule"]
        for v in run_drc_klayout_engine(str(four), str(DECK_PATH))["violations"]
    }
    assert {"FIN.W.1", "GATE.W.1"} <= rules_4x


# --------------------------------------------------------------------------- #
# Pinned PDK (scripts/fetch-pdks.sh)
# --------------------------------------------------------------------------- #


@_needs_asap7
def test_deck_layers_match_pinned_lyp():
    """The deck's GDS numbers are those the pinned asap7.lyp names: no
    second, hand-maintained layer map."""
    lyp = {
        name.lower(): (int(layer), int(dt))
        for name, layer, dt in _LYP_ENTRY_RE.findall(_ASAP7_LYP.read_text())
    }
    deck = {
        var: (int(layer), int(dt))
        for var, layer, dt in _DECK_LAYER_RE.findall(_deck_text())
    }
    assert len(deck) == 16
    for var, source in deck.items():
        assert lyp.get(var) == source, var


def _place_between_neighbours(gds: Path, neighbour: str, out: Path) -> list[str]:
    """Place every shipped cell abutted between two copies of ``neighbour``
    (row cells such as FILLERxp5 or TAPCELL_WITH_FILLER carry 54 nm
    select/well slivers that are only legal abutted), one sandwich per 8 um
    slot. Returns the cell names in slot order."""
    src = kdb.Layout()
    src.read(str(gds))
    out_layout = kdb.Layout()
    out_layout.dbu = src.dbu
    top = out_layout.create_cell("TOP")
    names = sorted(c.name for c in src.top_cells())
    copies: dict[str, kdb.Cell] = {}
    boundary = src.layer(100, 0)

    def cell(name: str) -> kdb.Cell:
        if name not in copies:
            copies[name] = out_layout.create_cell(name)
            copies[name].copy_tree(src.cell(name))
        return copies[name]

    def width(name: str) -> int:
        return kdb.Region(src.cell(name).begin_shapes_rec(boundary)).bbox().width()

    pitch = int(round(8.0 / src.dbu))
    for i, name in enumerate(names):
        x, y = (i % 12) * pitch, (i // 12) * pitch
        for placed, dx in (
            (neighbour, 0),
            (name, width(neighbour)),
            (neighbour, width(neighbour) + width(name)),
        ):
            top.insert(
                kdb.CellInstArray(
                    cell(placed).cell_index(), kdb.Trans(kdb.Point(x + dx, y))
                )
            )
    out_layout.write(str(out))
    return names


@_needs_asap7
@_needs_klayout
@pytest.mark.parametrize(
    ("lib", "suffix"),
    [("asap7sc7p5t_rvt", "R"), ("asap7sc7p5t_lvt", "L"), ("asap7sc7p5t_slvt", "SL")],
)
def test_shipped_cells_clean_apart_from_documented_deviations(tmp_path, lib, suffix):
    gds = _ASAP7_ROOT / "libs" / lib / "gds" / f"asap7sc7p5t_28_{suffix}.gds.gz"
    out = tmp_path / f"cells_{suffix}.gds"
    names = _place_between_neighbours(gds, f"INVx1_ASAP7_75t_{suffix}", out)
    report = run_drc_klayout_engine(str(out), str(DECK_PATH))
    pitch_dbu = 8.0 / report["dbu_um"]
    found: dict[str, set[str]] = defaultdict(set)
    for v in report["violations"]:
        slot = int(v["bbox"]["bottom"] // pitch_dbu) * 12 + int(
            v["bbox"]["left"] // pitch_dbu
        )
        found[names[slot].removesuffix(f"_ASAP7_75t_{suffix}")].add(v["rule"])
    expected = {cell: set(rules) for cell, rules in SHIPPED_CELL_DEVIATIONS.items()}
    assert dict(found) == expected
    assert len(set(report["coverage"]["rule_categories"])) == len(RULE_IDS)
