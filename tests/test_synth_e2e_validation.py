"""CI-safe slice of Epic #704 Phase 4's end-to-end validation (issue #990):
`klt synthesize` run for real against a real Tiny Tapeout `tt06` corpus
design (`tests/corpus/synth_e2e_validation/sources/tt_um_ALU/`, see that
directory's own `README.md` for full provenance/licensing/results), checked
equivalent to its source RTL via `klt equiv` (issue #808's acceptance gate)
-- the combinational half of that validation, which needs only a real
`yosys` and the CI-fetched `sky130_fd_sc_hd` liberty (`docs/cli/equiv.md`'s
Phase 0 scope), mirroring `tests/test_synthesize_equiv_gate.py`'s own real
(non-mocked) CI-integration posture.

The other 4 designs in that corpus are all sequential -- out of `klt
equiv`'s combinational-only scope -- and are validated instead via a
standalone Yosys `equiv_make`/`equiv_induct` proof plus gate-level
simulation against the real sky130 functional cell-model library
(`libs.ref/sky130_fd_sc_hd/verilog/`), which this repo's CI does not
provision (only the `.lib` liberty view is fetched, per
`scripts/fetch-sky130-liberty.sh`'s own docstring). That fuller validation
is `tests/corpus/synth_e2e_validation/run_validation.py` -- deliberate,
reviewed, run locally against a real PDK install, never a CI step, same
convention as `tests/corpus/statime/regenerate.sh`/
`tests/corpus/techmap/regenerate.sh`. See
`tests/corpus/synth_e2e_validation/README.md` for the full corpus, method,
and last-measured results table.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from klayout_tools import pdk as pdk_module
from klayout_tools.synthesize import run_synthesize

_CORPUS_DIR = Path(__file__).parent / "corpus" / "synth_e2e_validation"
_ALU_DIR = _CORPUS_DIR / "sources" / "tt_um_ALU"

HAVE_YOSYS = shutil.which("yosys") is not None


def _find_any_cell_library() -> tuple[str, str, str, str] | None:
    """Duplicated from `tests/test_synthesize_equiv_gate.py`'s own helper of
    the same name/behaviour -- test modules in this repo do not import
    fixtures across each other."""
    try:
        result = pdk_module.list_pdks()
    except Exception:
        return None
    for install in result["installs"]:
        for variant in install["variants"]:
            try:
                libraries = pdk_module.list_cell_libraries(
                    variant=variant["name"], root=install["root"]
                )
            except Exception:
                continue
            for library in libraries["libraries"]:
                name = library["name"]
                nominal_corner = library["nominal_corner"]
                prefix = f"{name}__"
                corner = (
                    nominal_corner[len(prefix) :]
                    if nominal_corner.startswith(prefix)
                    else nominal_corner
                )
                return install["root"], variant["name"], name, corner
    return None


_REAL_CELL_LIBRARY = _find_any_cell_library()

pytestmark = [
    pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine"),
    pytest.mark.skipif(
        _REAL_CELL_LIBRARY is None,
        reason="no real standard-cell liberty resolves via list_pdks() on this machine",
    ),
]


def test_tt_um_alu_verify_equivalence_real_yosys(tmp_path, monkeypatch):
    """AC #2 (combinational slice): `tt_um_ALU` -- a real Tiny Tapeout `tt06`
    corpus design, not a hand-written fixture -- run through the real `klt
    synthesize` pipeline with `verify_equivalence=True`. `klt equiv`'s real
    Yosys SAT proof reports the mapped netlist equivalent to the checked-in
    source RTL."""
    root, variant, cell_library, corner = _REAL_CELL_LIBRARY
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    for name in ("project.v", "ALU_3bits_Top.v", "ALU_ArithmeticOperators.v"):
        shutil.copy(_ALU_DIR / name, tmp_path / name)

    request_path = tmp_path / "synth.json"
    request_path.write_text(
        json.dumps(
            {
                "sources": [
                    "project.v",
                    "ALU_3bits_Top.v",
                    "ALU_ArithmeticOperators.v",
                ],
                "hdl_toplevel": "tt_um_ALU",
                "pdk": {"cell_library": cell_library, "corner": corner},
            }
        ),
        encoding="utf-8",
    )

    report = run_synthesize(str(request_path), verify_equivalence=True)

    assert report["status"] == "ok"
    assert report["instance_count"] > 0
    assert report["equivalence"]["status"] == "equivalent"
    assert report["equivalence"]["engine"] == "yosys"


def test_results_snapshot_is_well_formed():
    """No toolchain required: the checked-in `results.json` snapshot (last
    produced by `run_validation.py`) has the shape this corpus's own
    README documents, and records a real verdict (never a fabricated
    'equivalent'/'proven' with no evidence behind it) for every design."""
    snapshot = json.loads((_CORPUS_DIR / "results.json").read_text(encoding="utf-8"))

    designs = {entry["design"] for entry in snapshot}
    assert designs == {
        "modexp",
        "tt_um_ALU",
        "tt_um_8bitALU",
        "tt_um_LFSR_shivam",
        "tt_um_CKPope_top",
    }

    for entry in snapshot:
        assert entry["synthesize"]["status"] == "ok"
        assert entry["synthesize"]["instance_count"] > 0
        equivalence = entry["equivalence"]
        if "klt_equiv" in equivalence:
            assert equivalence["klt_equiv"]["status"] == "equivalent"
        else:
            # Sequential design: at least one of the two independent checks
            # must have produced a real, non-fabricated positive result --
            # a bounded formal proof, or an executable gate-level
            # simulation actually passing (never both absent/False).
            induct_proven = equivalence["yosys_equiv_induct"]["proven"]
            gls_passed = equivalence["gate_level_simulation"]["passed"]
            assert induct_proven or gls_passed
