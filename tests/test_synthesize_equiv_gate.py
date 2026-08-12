"""Real, unmocked end-to-end tests for `klt synthesize`'s `verify_equivalence`
acceptance gate (issue #808, Phase 1 of Epic #704 -- wires `klt equiv` (#726)
in as `klt synthesize`'s correctness gate: a synthesized netlist is not
considered done until `klt equiv` reports it `"equivalent"` to its source
RTL).

Two tiers, mirroring `tests/test_equiv.py::test_corpus_rtl_vs_real_synthesized_gates`'s
own structure and substitution rationale (issue #520's Tiny Tapeout RTL
corpus has not yet landed checked-in fixtures in this repo -- only GDS
standard-cell layouts exist under `tests/corpus/` as of this issue; a real,
small **combinational** RTL design run through the real pipeline against a
real, host-resolved standard-cell library is the accepted substitute until
#520 ships RTL material):

- **Happy path**: a real 4-bit ripple-carry adder (`adder4.v`, combinational
  -- no clock, satisfying `klt equiv`'s Phase 0 combinational-only scope)
  run through the real `klt synthesize` pipeline with `verify_equivalence=
  True` against a real, host-resolved standard-cell library -- the gate
  runs `klt equiv` for real (real Yosys SAT proof) and reports
  `"equivalent"`, attached to the response's own `equivalence` field.
- **Seeded mismatch**: mirrors `test_corpus_rtl_vs_real_synthesized_gates`'s
  own "genuinely-broken synthesized netlist" technique (never hand-edited
  gate-level Verilog, which risks producing something Yosys simply refuses
  to parse instead of a real logical divergence) -- a second, independently
  broken RTL variant (the same "dropped carry-in" bug class) is really
  synthesized to produce a real, structurally-valid but functionally wrong
  gate netlist; that netlist is substituted in place of the good design's
  own correctly-synthesized netlist (simulating a synthesis pipeline that
  produced output diverging from its own source RTL), and `run_synthesize`'s
  `verify_equivalence=True` gate is proven to catch it -- a hard
  `SynthesizeError`, not a silent warning or a passing "happy path" result.

Neither test is skipped in this repo's own CI (`.github/workflows/ci.yml`
provisions a pinned real Yosys 0.67, a pinned real Icarus Verilog 13.0, and
a real sky130_fd_sc_hd liberty unconditionally on every run) -- both
`@pytest.mark.skipif` guards below exist only for a developer machine
lacking one of those three, matching `tests/test_equiv.py`'s own posture.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from klayout_tools import pdk as pdk_module
from klayout_tools import synthesize
from klayout_tools.synthesize import SynthesizeError, run_synthesize

# 4-bit ripple-carry adder -- purely combinational (no clock), so it is
# in-scope for `klt equiv`'s Phase 0 MVP (a design with flip-flops/latches/
# memories is rejected outright, see `docs/cli/equiv.md`'s "Scope" section).
# Identical to `tests/test_equiv.py`'s own `_ADDER4_RTL` fixture, duplicated
# here rather than imported -- this repo's own convention (`test_synthesize.py`
# duplicates its GCD fixture rather than importing across test modules).
_ADDER4_RTL = """\
module adder4 (
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire       cin,
    output wire [3:0] sum,
    output wire       cout
);
  assign {cout, sum} = a + b + cin;
endmodule
"""

# Seeded-broken: drops `cin` from the sum -- a real "forgot the carry-in"
# bug class, not a synthetic/arbitrary corruption. Identical to
# `tests/test_equiv.py`'s own `_ADDER4_RTL_BROKEN` fixture.
_ADDER4_RTL_BROKEN = """\
module adder4 (
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire       cin,
    output wire [3:0] sum,
    output wire       cout
);
  assign {cout, sum} = a + b;
endmodule
"""

_WRITE_VERILOG_RE = re.compile(r"^write_verilog -noattr (\S+)$")


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def _netlist_path_from_script(script_path: str) -> str:
    """Parse the generated `.ys` script's own `write_verilog` line for the
    netlist path it declares -- never hardcodes `synthesize.py`'s private
    path-naming scheme, mirroring `tests/test_synthesize.py`'s own
    `_script_output_paths` helper."""
    with open(script_path, encoding="utf-8") as handle:
        for line in handle:
            match = _WRITE_VERILOG_RE.match(line.rstrip("\n"))
            if match:
                return match.group(1)
    raise AssertionError(f"'{script_path}' has no write_verilog line")


HAVE_YOSYS = shutil.which("yosys") is not None
HAVE_IVERILOG = shutil.which("iverilog") is not None


def _find_any_cell_library() -> tuple[str, str, str, str] | None:
    """Search every install/variant `list_pdks()` discovers for one shipping
    at least one standard-cell digital library -- returns
    ``(root, variant, cell_library, corner)`` or ``None``. Duplicated from
    `tests/test_equiv.py`'s own helper of the same name/behaviour (test
    modules in this repo do not import fixtures across each other)."""
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
        not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
    ),
    pytest.mark.skipif(
        _REAL_CELL_LIBRARY is None,
        reason="no real standard-cell liberty resolves via list_pdks() on this machine",
    ),
]


def test_verify_equivalence_gate_passes_on_real_synthesis(tmp_path, monkeypatch):
    """AC #2: demonstrated end-to-end on a real synthesizable design (the
    ripple-carry adder, `tests/test_equiv.py`'s own documented substitute
    for #520's not-yet-landed RTL corpus) run through the actual
    `klt synthesize` pipeline -- `klt equiv` reports `"equivalent"`."""
    root, variant, cell_library, corner = _REAL_CELL_LIBRARY
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    _write(tmp_path / "adder4.v", _ADDER4_RTL)
    request_path = _write_request(
        tmp_path / "synth.json",
        {
            "sources": ["adder4.v"],
            "hdl_toplevel": "adder4",
            "pdk": {"cell_library": cell_library, "corner": corner},
        },
    )

    report = run_synthesize(request_path, verify_equivalence=True)

    assert report["status"] == "ok"
    assert report["equivalence"]["status"] == "equivalent"
    assert report["equivalence"]["engine"] == "yosys"
    assert report["equivalence"]["artifacts"]["script_path"]


def test_verify_equivalence_gate_fails_hard_on_seeded_mismatch(tmp_path, monkeypatch):
    """AC #3: a deliberately broken synthesis output (seeded mismatch) makes
    the gate fail, proving it actually catches divergence rather than just
    passing on the happy path -- mirrors `test_equiv.py`'s own
    "dropped carry-in" mutation technique for producing a genuinely-broken
    synthesized netlist, applied here to `run_synthesize`'s own wired gate:
    the correctly-synthesized good netlist is substituted with the real
    (but functionally wrong) netlist the broken RTL variant synthesizes to,
    simulating a synthesis pipeline that produced output diverging from its
    declared source RTL."""
    root, variant, cell_library, corner = _REAL_CELL_LIBRARY
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    # Real synthesis of the broken ("dropped cin") RTL variant, independent
    # of the good design below -- a real, structurally-valid, functionally
    # wrong netlist (never hand-edited gate-level Verilog).
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    _write(bad_dir / "adder4.v", _ADDER4_RTL_BROKEN)
    bad_request_path = _write_request(
        bad_dir / "synth.json",
        {
            "sources": ["adder4.v"],
            "hdl_toplevel": "adder4",
            "pdk": {"cell_library": cell_library, "corner": corner},
        },
    )
    bad_report = run_synthesize(bad_request_path)
    assert bad_report["status"] == "ok"
    with open(bad_report["netlist_path"], encoding="utf-8") as handle:
        broken_netlist_text = handle.read()

    # Seed the mismatch: after the *good* design's own real Yosys run
    # succeeds (produces the correct netlist for `_ADDER4_RTL`), overwrite
    # the netlist file the gate is about to check with the broken variant's
    # real synthesized gates -- `run_synthesize`'s `verify_equivalence`
    # gate must catch that the file on disk no longer matches the RTL it
    # declares as its source.
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    _write(good_dir / "adder4.v", _ADDER4_RTL)
    good_request_path = _write_request(
        good_dir / "synth.json",
        {
            "sources": ["adder4.v"],
            "hdl_toplevel": "adder4",
            "pdk": {"cell_library": cell_library, "corner": corner},
        },
    )

    original_run_yosys = synthesize._run_yosys

    def _seed_mismatch_after_synthesis(script_path: str) -> None:
        original_run_yosys(script_path)
        netlist_path = _netlist_path_from_script(script_path)
        with open(netlist_path, "w", encoding="utf-8") as handle:
            handle.write(broken_netlist_text)

    monkeypatch.setattr(synthesize, "_run_yosys", _seed_mismatch_after_synthesis)

    with pytest.raises(SynthesizeError, match="NOT equivalent") as exc_info:
        run_synthesize(good_request_path, verify_equivalence=True)
    assert "sum" in str(exc_info.value)
