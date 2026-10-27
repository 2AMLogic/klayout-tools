"""`klt synthesize`'s memory-mapping evidence (issue #2745).

Two halves:

- **Offline unit tests** of `klayout_tools.synthesize_memory`'s pure rules
  (script text, per-module classification, overall folding, blackbox
  reporting, incomplete-evidence handling). No Yosys, no PDK.
- **Optional real-Yosys tests** over small self-contained ROM/RAM/
  unused-memory/mixed/unrelated-blackbox fixtures (inline RTL below),
  comparing the reported stages against the evidence artifacts the side
  invocation actually wrote. Skipped explicitly when `yosys` or a real
  `sky130_fd_sc_hd` liberty is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import pdk as pdk_module
from klayout_tools import synthesize_memory as sm
from klayout_tools.synthesize import SynthesizeError, run_synthesize

# ---------------------------------------------------------------------------
# Offline unit tests
# ---------------------------------------------------------------------------


def test_evidence_script_mirrors_production_prefix_and_stops_before_fine():
    text = sm.memory_evidence_script_text(
        sources=["/a/x.v", "/a/y.v"],
        hdl_toplevel="top",
        blackbox_list_path="/o/bb.txt",
        elaborated_stats_path="/o/e.json",
        pre_lowering_stats_path="/o/p.json",
    )
    assert text.splitlines() == [
        "read_verilog /a/x.v",
        "read_verilog /a/y.v",
        "hierarchy -check -top top",
        "tee -q -o /o/bb.txt select -list =A:blackbox =A:whitebox",
        "proc",
        "tee -q -o /o/e.json stat -json",
        "synth -top top -run :fine",
        "tee -q -o /o/p.json stat -json",
    ]


def test_evidence_script_loads_liberty_first_and_lists_library_cells():
    lines = sm.memory_evidence_script_text(
        sources=["/a/x.v"],
        hdl_toplevel="top",
        blackbox_list_path="/o/bb.txt",
        elaborated_stats_path="/o/e.json",
        pre_lowering_stats_path="/o/p.json",
        liberty_path="/l/x.lib",
        library_cells_path="/o/lib.txt",
    ).splitlines()
    assert lines[:3] == [
        "read_liberty -lib /l/x.lib",
        "tee -q -o /o/lib.txt select -list =A:blackbox",
        "read_verilog /a/x.v",
    ]


def test_evidence_script_carries_arithmetic_techmap_before_synth():
    lines = sm.memory_evidence_script_text(
        sources=["/a/x.v"],
        hdl_toplevel="top",
        blackbox_list_path="/o/bb.txt",
        elaborated_stats_path="/o/e.json",
        pre_lowering_stats_path="/o/p.json",
        adder_sources=("/o/add8.v",),
        adder_techmap_path="/o/map.v",
    ).splitlines()
    techmap = lines.index("techmap -map /o/map.v")
    assert lines.index("read_verilog /o/add8.v") < techmap
    assert techmap < lines.index("synth -top top -run :fine")


@pytest.mark.parametrize(
    ("counts", "status"),
    [
        ((0, 0, 0), "none_detected"),
        ((1, 1, 0), "lowered_to_logic"),
        ((2, 2, 0), "lowered_to_logic"),
        ((1, 0, 0), "optimized_away"),
        ((1, 1, 1), "surviving"),
        ((2, 1, 0), "mixed"),  # one removed, one lowered
        ((2, 2, 1), "mixed"),  # one lowered, one surviving
        ((2, 1, 1), "mixed"),  # one removed, one surviving
        ((0, 1, 0), "indeterminate"),  # appeared after elaboration
        ((1, 0, 1), "indeterminate"),  # final exceeds pre-lowering
    ],
)
def test_classify_module(counts, status):
    got, reason = sm.classify_module(*counts)
    assert got == status
    assert (reason is not None) == (status == "indeterminate")


@pytest.mark.parametrize(
    ("statuses", "overall"),
    [
        ([], "none_detected"),
        (["lowered_to_logic"], "lowered_to_logic"),
        (["lowered_to_logic", "lowered_to_logic"], "lowered_to_logic"),
        (["lowered_to_logic", "optimized_away"], "mixed"),
        (["mixed", "indeterminate"], "indeterminate"),
    ],
)
def test_overall_status(statuses, overall):
    assert sm.overall_status(statuses) == overall


def _modules(**per_module):
    return {
        f"\\{name}": {
            "num_memories": mem,
            "num_memory_bits": bits,
            "num_cells_by_type": {"$mem_v2": cells} if cells else {},
        }
        for name, (mem, bits, cells) in per_module.items()
    }


def test_build_memory_mapping_per_module_rows_and_mixed_overall():
    mapping = sm.build_memory_mapping(
        elaborated_modules=_modules(rom=(1, 128, 0), ram=(1, 64, 0), alu=(0, 0, 0)),
        pre_lowering_modules=_modules(rom=(0, 0, 1), ram=(0, 0, 0), alu=(0, 0, 0)),
        final_modules=_modules(rom=(0, 0, 0), ram=(0, 0, 0), alu=(0, 0, 0)),
        script_path={"path": "x.ys", "scope": "repo"},
    )
    assert mapping["status"] == "mixed"
    assert mapping["reason"] is None
    # Memory-free modules are omitted; rows sorted by module name.
    assert [(r["module"], r["status"]) for r in mapping["modules"]] == [
        ("ram", "optimized_away"),
        ("rom", "lowered_to_logic"),
    ]
    assert mapping["modules"][1]["elaborated_memory_bits"] == 128


@pytest.mark.parametrize("missing", ["elaborated", "pre_lowering", "final"])
def test_build_memory_mapping_incomplete_evidence_is_indeterminate(missing):
    stages = {
        "elaborated_modules": _modules(ram=(1, 128, 0)),
        "pre_lowering_modules": _modules(ram=(0, 0, 1)),
        "final_modules": _modules(ram=(0, 0, 0)),
    }
    stages[f"{missing}_modules"] = None
    mapping = sm.build_memory_mapping(**stages, script_path=None)
    assert mapping["status"] == "indeterminate"
    assert missing in mapping["reason"]
    # Never a partial, fabricated classification.
    assert mapping["modules"] == []


def test_build_memory_mapping_indeterminate_module_carries_reason():
    mapping = sm.build_memory_mapping(
        elaborated_modules=_modules(top=(0, 0, 0)),
        pre_lowering_modules=_modules(top=(0, 0, 1)),
        final_modules=_modules(top=(0, 0, 0)),
        script_path=None,
    )
    assert mapping["status"] == "indeterminate"
    assert mapping["reason"].startswith("top: ")


def test_parse_declared_blackboxes_keeps_module_lines_only():
    listing = "hm\nhm/y\nhm/a\n\\esc\nnoports\n"
    assert sm.parse_declared_blackboxes(listing) == {"hm", "esc", "noports"}
    assert sm.parse_declared_blackboxes("") == set()


def test_blackbox_modules_reports_only_surviving_declared_modules():
    final = {
        "\\top": {
            "num_cells_by_type": {
                "sky130_fd_sc_hd__buf_1": 3,
                "hm": 2,
                "$_DLATCH_P_": 1,
                "sub": 1,
            }
        },
        "\\sub": {"num_cells_by_type": {"wb": 1}},
    }
    assert sm.blackbox_modules({"hm", "wb", "gone"}, final) == ["hm", "wb"]
    assert sm.blackbox_modules(set(), final) == []
    assert sm.blackbox_modules(None, final) is None
    assert sm.blackbox_modules({"hm"}, None) is None


def test_collect_memory_evidence_never_raises_on_yosys_failure(tmp_path, monkeypatch):
    from klayout_tools import synthesize

    def boom(script_path, **kwargs):
        raise SynthesizeError("yosys synthesis failed: ERROR: nope")

    monkeypatch.setattr(synthesize, "_run_yosys", boom)
    mapping, blackboxes = sm.collect_memory_evidence(
        output_dir=str(tmp_path),
        resolved_sources=[str(tmp_path / "x.v")],
        hdl_toplevel="x",
        final_stats_path=str(tmp_path / "missing.json"),
    )
    assert mapping["status"] == "indeterminate"
    assert "ERROR: nope" in mapping["reason"]
    assert blackboxes is None


@pytest.mark.parametrize("repo_root", [None, "REPO"])
def test_collect_memory_evidence_runs_yosys_in_repo_root(
    tmp_path, monkeypatch, repo_root
):
    from klayout_tools import synthesize

    seen = {}

    def spy(script_path, **kwargs):
        seen.update(kwargs)
        raise SynthesizeError("stop")

    monkeypatch.setattr(synthesize, "_run_yosys", spy)
    root = str(tmp_path) if repo_root else None
    sm.collect_memory_evidence(
        output_dir=str(tmp_path),
        resolved_sources=[str(tmp_path / "x.v")],
        hdl_toplevel="x",
        final_stats_path=str(tmp_path / "missing.json"),
        repo_root=root,
    )
    assert seen["cwd"] == root


# ---------------------------------------------------------------------------
# Optional real-Yosys tests
# ---------------------------------------------------------------------------

HAVE_YOSYS = shutil.which("yosys") is not None


def _find_real_sky130_variant() -> tuple[str, str] | None:
    """A host-resolved PDK variant shipping `sky130_fd_sc_hd`'s liberty
    view, or ``None`` -- the same lookup `tests/test_synthesize.py`'s real
    GCD integration test uses (test modules here do not share fixtures)."""
    try:
        result = pdk_module.list_pdks()
    except Exception:
        return None
    for install in result["installs"]:
        for variant in install["variants"]:
            candidate = os.path.join(
                install["root"], variant["name"], "libs.ref", "sky130_fd_sc_hd", "lib"
            )
            if os.path.isdir(candidate):
                return install["root"], variant["name"]
    return None


_REAL_SKY130_VARIANT = _find_real_sky130_variant()

_needs_real_toolchain = [
    pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine"),
    pytest.mark.skipif(
        _REAL_SKY130_VARIANT is None,
        reason="no real sky130_fd_sc_hd liberty resolves via list_pdks() here",
    ),
]

_MEMORY_FIXTURES = {
    # Initialized, read-only array with a registered read: an inferred ROM.
    "rom16x8": """module rom16x8(input clk, input [3:0] a, output reg [7:0] d);
  reg [7:0] mem [0:15];
  integer i;
  initial for (i = 0; i < 16; i = i + 1) mem[i] = i * 37 + 5;
  always @(posedge clk) d <= mem[a];
endmodule
""",
    # Synchronous write, registered read: an inferred RAM.
    "ram16x8": """module ram16x8(input clk, input we, input [3:0] wa, input [3:0] ra,
               input [7:0] wd, output reg [7:0] rd);
  reg [7:0] mem [0:15];
  always @(posedge clk) begin
    if (we) mem[wa] <= wd;
    rd <= mem[ra];
  end
endmodule
""",
    # Written but never read: removed before lowering.
    "unused_mem": """module unused_mem(input clk, input we, input [3:0] wa,
                  input [7:0] wd, input [7:0] x, output [7:0] y);
  reg [7:0] mem [0:15];
  always @(posedge clk) if (we) mem[wa] <= wd;
  assign y = ~x;
endmodule
""",
    # One live RAM and one write-only memory in the same module.
    "mixed_mem": """module mixed_mem(input clk, input we, input [3:0] wa,
                 input [3:0] ra, input [7:0] wd, output reg [7:0] rd);
  reg [7:0] mem [0:15];
  reg [7:0] dead [0:15];
  always @(posedge clk) begin
    if (we) begin mem[wa] <= wd; dead[wa] <= wd; end
    rd <= mem[ra];
  end
endmodule
""",
    # An unrelated (non-memory) blackbox macro instantiated by the top.
    "bb_top": """(* blackbox *)
module analog_macro(input a, output y);
endmodule
module bb_top(input a, output y);
  analog_macro u0(.a(a), .y(y));
endmodule
""",
}


def _synthesize_fixture(tmp_path: Path, monkeypatch, top: str) -> dict:
    root, variant = _REAL_SKY130_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)
    (tmp_path / ".git").mkdir(exist_ok=True)  # pin the repo root to tmp_path
    (tmp_path / f"{top}.v").write_text(_MEMORY_FIXTURES[top], encoding="utf-8")
    request_path = tmp_path / "synth.json"
    request_path.write_text(
        json.dumps(
            {
                "sources": [f"{top}.v"],
                "hdl_toplevel": top,
                "pdk": {"cell_library": "sky130_fd_sc_hd"},
            }
        ),
        encoding="utf-8",
    )
    return run_synthesize(str(request_path))


def _artifact_memories(tmp_path: Path, report: dict, stage: str, module: str) -> int:
    """Recount one stage's memories straight from the artifact the side
    invocation wrote, independently of `synthesize_memory`'s own counting."""
    script = tmp_path / report["memory_mapping"]["script_path"]["path"]
    stats = script.parent / f"{report['hdl_toplevel']}_memory_{stage}.json"
    block = json.loads(stats.read_text(encoding="utf-8"))["modules"][f"\\{module}"]
    by_type = block.get("num_cells_by_type", {})
    return (
        block.get("num_memories", 0)
        + by_type.get("$mem", 0)
        + by_type.get("$mem_v2", 0)
    )


@pytest.mark.parametrize(
    ("top", "status", "elaborated", "pre_lowering"),
    [
        ("rom16x8", "lowered_to_logic", 1, 1),
        ("ram16x8", "lowered_to_logic", 1, 1),
        ("unused_mem", "optimized_away", 1, 0),
        ("mixed_mem", "mixed", 2, 1),
    ],
)
@_needs_real_toolchain[0]
@_needs_real_toolchain[1]
def test_memory_mapping_real_yosys(
    tmp_path, monkeypatch, top, status, elaborated, pre_lowering
):
    report = _synthesize_fixture(tmp_path, monkeypatch, top)

    mapping = report["memory_mapping"]
    assert mapping["status"] == status
    assert mapping["reason"] is None
    assert mapping["modules"] == [
        {
            "module": top,
            "status": status,
            "elaborated_memories": elaborated,
            "elaborated_memory_bits": 128 * elaborated,
            "pre_lowering_memories": pre_lowering,
            "final_memories": 0,
        }
    ]
    assert _artifact_memories(tmp_path, report, "elaborated", top) == elaborated
    assert _artifact_memories(tmp_path, report, "pre_lowering", top) == pre_lowering
    assert report["blackbox_modules"] == []
    assert report["instance_count"] > 0


@_needs_real_toolchain[0]
@_needs_real_toolchain[1]
def test_unrelated_blackbox_real_yosys(tmp_path, monkeypatch):
    report = _synthesize_fixture(tmp_path, monkeypatch, "bb_top")

    assert report["memory_mapping"]["status"] == "none_detected"
    assert report["memory_mapping"]["modules"] == []
    assert report["blackbox_modules"] == ["analog_macro"]
