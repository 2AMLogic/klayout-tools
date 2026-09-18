"""Cross-validation of `klt drc` against **magic**, an independently
implemented geometry engine -- issue #2014, pairing #1 of tracking issue
#2007 ("independent cross-validation oracles for klt verdicts").

`klt drc` is backed by KLayout alone. A curated-deck bug shows up in
`tests/test_drc.py`'s golden expectations, but a bug in the *geometry
engine underneath* -- or in how this repo's deck asks it a question --
looks identical to a clean layout there, because there is nothing else to
ask. magic is a different implementation of the same job (different
codebase, different geometry model, no shared code with KLayout) reading
its own independently-transcribed sky130/gf180mcu rule decks from
open_pdks, so a disagreement between the two means something.

`tests/helpers/magic_oracle.py` drives magic (headless batch Tcl, never a
GUI); `docs/design/magic-oracle.md` is the methodology, including the
declared shared surface and the constructs neither deck covers. Read that
doc before adding or relaxing an assertion here.

## How this tier is gated

Real-binary-gated, exactly like `tests/test_lvs.py`'s netgen tier: every
test skips with a specific reason when `magic` is not on `$PATH` or no
magic technology file resolves for the deck. Absence must never fail CI.
`.github/workflows/magic-oracle.yml` is the opt-in job that provisions
both and asserts the tests were *not* skipped there.

## What is compared, and why not more

Per #2007's criterion 1 (matched geometry/units/model/scope) both engines
read the **same GDS file** -- not two exports of one design -- and each
test asserts that magic's own loaded-cell bounding box equals KLayout's
for that file before comparing any verdict, plus (in the provenance test)
that `klt`'s recorded `provenance.input.content_hash` is the hash of the
bytes the oracle read.

Violations are compared as **zero vs. non-zero plus locations**, never as
equal integers. The two engines report a spacing failure with different
granularity: KLayout returns one edge-pair polygon *spanning* the illegal
gap, magic paints error tiles on the geometry on *either side* of it
(measured on the seeded fixture below: 1 KLayout violation, 2 magic error
areas covering 4 rectangles). Demanding `1 == 2` would encode a reporting
convention, not a verdict; demanding "both found something, in the same
place, for the same rule" is the verdict-level agreement that is actually
meaningful. `docs/design/magic-oracle.md` records this in full.
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from helpers import magic_oracle
from helpers.magic_oracle import run_magic_drc
from klayout_tools.drc import run_drc

CORPUS_DIR = Path(__file__).parent / "corpus"
SKY130_INV = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
GF180_CLKINV = CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds"

#: Tolerance for "magic flagged this in the same place `klt` did", in µm.
#: The two error shapes touch rather than overlap (see the module
#: docstring), so any non-negative slack passes on the fixture below; this
#: is set to one met1 spacing threshold (0.14 µm, rounded up) so a magic
#: error a *different* rule fired on, elsewhere in the cell, could not be
#: mistaken for agreement.
LOCATION_TOLERANCE_UM = 0.15


def _require_oracle(deck: str) -> None:
    reason = magic_oracle.oracle_skip_reason(deck)
    if reason:
        pytest.skip(reason)


@pytest.fixture
def sky130_oracle() -> str:
    _require_oracle("sky130")
    return "sky130"


@pytest.fixture
def gf180mcu_oracle() -> str:
    _require_oracle("gf180mcu")
    return "gf180mcu"


def _top_cell(path: Path) -> str:
    layout = kdb.Layout()
    layout.read(str(path))
    return layout.top_cell().name


def _bbox_um(path: Path) -> tuple[float, float, float, float]:
    """KLayout's own bounding box for the top cell of ``path``, in µm --
    the reference the oracle's `cell_bbox_um` is checked against so a
    comparison can never be made between two different pieces of
    geometry."""
    layout = kdb.Layout()
    layout.read(str(path))
    box = layout.top_cell().bbox()
    dbu = layout.dbu
    return (
        round(box.left * dbu, 4),
        round(box.bottom * dbu, 4),
        round(box.right * dbu, 4),
        round(box.top * dbu, 4),
    )


def _seed_met1_spacing_violation(source: Path, dest: Path) -> dict[str, float]:
    """Copy ``source``, adding one met1 rectangle 0.09 µm above the cell's
    met1 ground rail -- below both decks' 0.14 µm met1 spacing minimum
    (sky130 DRM `m1.2`, this repo's `met1.space.1`).

    Deliberately a *seeded defect in real geometry* rather than a
    synthetic two-rectangle layout: the same cell is the clean case, so
    the only difference between the two runs is the injected error. The
    rectangle is 0.6 × 1.0 µm, comfortably above met1's minimum width and
    minimum area, so it introduces exactly one *kind* of failure.

    Returns the seeded gap region in µm, for the location comparison.
    """
    layout = kdb.Layout()
    layout.read(str(source))
    top = layout.top_cell()
    met1 = layout.layer(68, 20)
    top.shapes(met1).insert(kdb.Box(300, 330, 900, 1330))
    layout.write(str(dest))
    return {"left": 0.3, "bottom": 0.24, "right": 0.9, "top": 0.33}


def _distance_um(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    """Separation between two rectangles in µm; ``0.0`` when they overlap
    or touch."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return max(dx, dy)


# --------------------------------------------------------------------------- #
# Known-good case: a real corpus cell both engines must call clean
# --------------------------------------------------------------------------- #


def test_sky130_clean_corpus_cell_agrees_with_magic(sky130_oracle, tmp_path):
    """#2007 criterion 3's known-good half: `klt drc` and magic both find
    zero violations in a real sky130 standard cell, having provably read
    the same geometry (bbox agreement) with the intended deck loaded
    (`tech name`)."""
    cell = _top_cell(SKY130_INV)

    report = run_drc(str(SKY130_INV), sky130_oracle)
    oracle = run_magic_drc(
        str(SKY130_INV), deck=sky130_oracle, cell=cell, work_dir=tmp_path
    )

    # Matched geometry: magic loaded the same cell extent KLayout sees.
    assert oracle.cell_bbox_um == _bbox_um(SKY130_INV)
    # Matched deck: the sky130A technology, not magic's built-in scmos.
    assert oracle.tech_name == "sky130A"
    assert oracle.tech_version
    # Evidence `klt` actually checked something (not a deck that skipped
    # every rule for want of layers), alongside the seeded-defect test
    # below, which is the evidence magic's own deck fires.
    assert report["coverage"]["layers_checked"]

    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    assert oracle.violation_count == 0
    assert oracle.errors == ()


def test_gf180mcu_clean_corpus_cell_agrees_with_magic(gf180mcu_oracle, tmp_path):
    """The same known-good agreement one PDK over, against magic's
    independently-transcribed `gf180mcuC` deck."""
    cell = _top_cell(GF180_CLKINV)

    report = run_drc(str(GF180_CLKINV), gf180mcu_oracle)
    oracle = run_magic_drc(
        str(GF180_CLKINV), deck=gf180mcu_oracle, cell=cell, work_dir=tmp_path
    )

    assert oracle.cell_bbox_um == _bbox_um(GF180_CLKINV)
    assert oracle.tech_name == "gf180mcuC"
    assert report["coverage"]["layers_checked"]

    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    assert oracle.violation_count == 0


# --------------------------------------------------------------------------- #
# Seeded defect: both engines must catch an injected met1 spacing error
# --------------------------------------------------------------------------- #


def test_seeded_met1_spacing_violation_caught_by_both(sky130_oracle, tmp_path):
    """#2007 criterion 3's load-bearing half: agreement on a *caught*
    defect. Both engines must flag the injected 0.09 µm met1 gap, name a
    met1-spacing rule for it, and put it in the same place.

    This test is also what makes the clean case above meaningful: it is
    direct evidence, on the same host and the same decks, that both rule
    sets are live -- neither "clean" verdict came from a deck that checked
    nothing.
    """
    defect = tmp_path / "inv_met1_spacing_defect.gds"
    seeded = _seed_met1_spacing_violation(SKY130_INV, defect)
    cell = _top_cell(defect)

    report = run_drc(str(defect), sky130_oracle)
    oracle = run_magic_drc(
        str(defect), deck=sky130_oracle, cell=cell, work_dir=tmp_path
    )

    assert oracle.cell_bbox_um == _bbox_um(defect)

    # Both caught it.
    assert report["status"] == "violations"
    assert report["violation_count"] >= 1
    assert oracle.violation_count >= 1

    # Both blamed met1 spacing -- `klt`'s own rule id, and magic's own rule
    # text (which carries the sky130 DRM id `met1.2` this deck's
    # `met1.space.1` transcribes).
    assert set(report["rule_counts"]) == {"met1.space.1"}
    assert oracle.rule_counts, "magic reported errors but no rule text"
    for rule in oracle.rule_counts:
        assert "met1.2" in rule, f"unexpected magic rule fired: {rule!r}"

    # Both put it in the same place: every magic error rectangle is within
    # a rule-threshold of the seeded gap, and every `klt` violation has a
    # magic error next to it.
    gap = (seeded["left"], seeded["bottom"], seeded["right"], seeded["top"])
    for error in oracle.errors:
        assert _distance_um(error.bbox_um, gap) <= LOCATION_TOLERANCE_UM, error

    dbu = report["dbu_um"]
    for violation in report["violations"]:
        box = violation["bbox"]
        klt_rect = (
            round(box["left"] * dbu, 4),
            round(box["bottom"] * dbu, 4),
            round(box["right"] * dbu, 4),
            round(box["top"] * dbu, 4),
        )
        assert any(
            _distance_um(klt_rect, error.bbox_um) <= LOCATION_TOLERANCE_UM
            for error in oracle.errors
        ), f"no magic error near klt violation {klt_rect}"


def test_seeded_defect_is_the_only_difference_from_the_clean_cell(
    sky130_oracle, tmp_path
):
    """Negative control for the fixture itself: re-copying the corpus cell
    through the same KLayout write path *without* the injected rectangle
    still reads clean under magic, so the seeding -- not the round-trip --
    is what both engines reacted to."""
    layout = kdb.Layout()
    layout.read(str(SKY130_INV))
    rewritten = tmp_path / "inv_rewritten.gds"
    layout.write(str(rewritten))

    oracle = run_magic_drc(
        str(rewritten), deck=sky130_oracle, cell=_top_cell(rewritten), work_dir=tmp_path
    )

    assert oracle.violation_count == 0
    assert run_drc(str(rewritten), sky130_oracle)["violation_count"] == 0


# --------------------------------------------------------------------------- #
# Provenance (#2007 criterion 4) and the "did it really run" guards
# --------------------------------------------------------------------------- #


def test_oracle_provenance_records_versions_and_the_same_input_bytes(
    sky130_oracle, tmp_path
):
    """Both stacks' tool and deck versions, and a hash of the bytes each
    one read, are recorded together -- and the two hashes of the input
    agree, which is what makes any future disagreement attributable."""
    cell = _top_cell(SKY130_INV)
    report = run_drc(str(SKY130_INV), sky130_oracle)
    oracle = run_magic_drc(
        str(SKY130_INV), deck=sky130_oracle, cell=cell, work_dir=tmp_path
    )

    provenance = magic_oracle.oracle_provenance(
        deck=sky130_oracle,
        gds_path=str(SKY130_INV),
        klt_report=report,
        tech_version=oracle.tech_version,
    )

    assert provenance["oracle"]["tool"] == "magic"
    assert provenance["oracle"]["version"]
    assert provenance["oracle"]["deck"]["name"] == "sky130A"
    assert provenance["oracle"]["deck"]["content_hash"].startswith("sha256:")
    assert provenance["oracle"]["deck"]["version"] == oracle.tech_version
    assert provenance["input"]["content_hash"].startswith("sha256:")

    klt_provenance = provenance["klt"]
    assert klt_provenance["klayout_version"]
    assert klt_provenance["deck"]["name"] == "sky130"
    assert klt_provenance["deck"]["content_hash"].startswith("sha256:")
    # The load-bearing line: `klt` hashed the same file the oracle did.
    input_hash = provenance["input"]["content_hash"]
    assert klt_provenance["input"]["content_hash"] == input_hash


def test_oracle_refuses_a_stream_it_cannot_read(sky130_oracle, tmp_path):
    """Guard for the failure this module hit in development: magic exits 0
    after failing to read a stream and then reports a clean, empty cell.
    A missing input must raise, never return `violation_count == 0`."""
    with pytest.raises(magic_oracle.MagicOracleError):
        run_magic_drc(
            str(tmp_path / "does_not_exist.gds"),
            deck=sky130_oracle,
            cell="nothing",
            work_dir=tmp_path,
        )
