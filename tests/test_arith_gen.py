"""Tests for `klayout_tools.arith_gen` / `klt arith-gen` (issue #1722).

Three tiers, following this repo's established test-tier convention (see
`tests/README.md` and `tests/test_synthesize.py`'s own docstring):

- **Pure unit tests** (always run, no external tool): cell-map construction
  for all five architectures, the cell-map -> prefix-graph legalisation rule,
  a Python-level *functional* simulation of the resulting graph against
  `a + b + cin` (the structural correctness gate that does not need a Verilog
  simulator), Verilog/techmap text emission, request/CLI validation, and the
  error paths.
- **Real-Yosys integration** (`@pytest.mark.skipif` when `yosys` is not on
  `$PATH`): every generated adder is proven equivalent to its behavioural
  `+` reference by the real `klt equiv` -- issue #1722's acceptance criterion
  1, run for real rather than asserted.
- **Real-Icarus integration** (`skipif` when `iverilog` is missing): the
  generated self-checking testbench actually compiles and reports `PASS`.

Neither integration tier is required for CI: both skip cleanly.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess

import pytest

from klayout_tools import arith_gen
from klayout_tools.arith_gen import (
    ARCHITECTURES,
    ArithGenError,
    build_prefix_graph,
    cell_map_for_architecture,
    default_module_name,
    emit_techmap_verilog,
    generate_adder,
    normalize_architecture,
    run_arith_gen,
    validate_cell_map,
)
from klayout_tools.cli.parser import create_parser

HAVE_YOSYS = shutil.which("yosys") is not None
HAVE_IVERILOG = shutil.which("iverilog") is not None

#: Widths exercised by the structural/functional sweeps: 1 and 2 are the
#: degenerate ends, 3/5/12/17 are the non-power-of-two cases the truncation
#: rules have to get right, 8/16/32 are the interesting real widths.
_SWEEP_WIDTHS = (1, 2, 3, 5, 8, 12, 16, 17, 32)


# --------------------------------------------------------------------------- #
# Functional correctness: simulate the prefix graph directly
# --------------------------------------------------------------------------- #


def _simulate(graph, a: int, b: int, cin: int) -> tuple[int, int]:
    """Evaluate a `PrefixGraph` the way the emitted Verilog does.

    Deliberately re-implements the emitted semantics (bitwise g/p, the prefix
    operator, then `c[i+1] = G | (P & cin)`) rather than importing anything
    from the emitter -- so a bug in either the cell map or the wiring rule
    shows up as a wrong sum, not as two matching implementations of the same
    mistake.
    """
    width = graph.width
    g0 = [((a >> i) & (b >> i)) & 1 for i in range(width)]
    p0 = [((a >> i) ^ (b >> i)) & 1 for i in range(width)]
    values: dict[tuple[int, int], tuple[int, int]] = {}
    for node in graph.nodes:
        if node.is_bitwise:
            values[node.key] = (g0[node.msb], p0[node.msb])
        else:
            upper_g, upper_p = values[node.upper]
            lower_g, lower_p = values[node.lower]
            values[node.key] = (
                upper_g | (upper_p & lower_g),
                upper_p & lower_p,
            )
    carries = [cin]
    for i in range(width):
        group_g, group_p = values[(i, 0)]
        carries.append(group_g | (group_p & cin))
    total = 0
    for i in range(width):
        total |= (p0[i] ^ carries[i]) << i
    return total, carries[width]


@pytest.mark.parametrize("architecture", ARCHITECTURES)
@pytest.mark.parametrize("width", _SWEEP_WIDTHS)
def test_prefix_graph_computes_addition(architecture, width):
    """Every architecture, at every width, computes `a + b + cin` exactly."""
    graph = build_prefix_graph(cell_map_for_architecture(architecture, width))
    mask = (1 << width) - 1
    rng = random.Random(f"{architecture}-{width}")
    vectors = [
        (0, 0, 0),
        (0, 0, 1),
        (mask, mask, 1),
        (mask, 1, 0),
        (mask, 0, 1),
        (mask, mask, 0),
    ] + [
        (rng.randint(0, mask), rng.randint(0, mask), rng.randint(0, 1))
        for _ in range(200)
    ]
    for a, b, cin in vectors:
        total, carry_out = _simulate(graph, a, b, cin)
        expected = a + b + cin
        assert total == expected & mask, (architecture, width, a, b, cin)
        assert carry_out == (expected >> width) & 1, (architecture, width, a, b, cin)


@pytest.mark.parametrize("architecture", ARCHITECTURES)
@pytest.mark.parametrize("width", _SWEEP_WIDTHS)
def test_cell_map_is_structurally_well_formed(architecture, width):
    cell_map = cell_map_for_architecture(architecture, width)
    assert len(cell_map) == width
    assert all(len(row) == width for row in cell_map)
    for i in range(width):
        assert cell_map[i][i] == 1, "the diagonal (g_i, p_i) always exists"
        assert cell_map[i][0] == 1, "every carry out of bit i must be computed"
        assert all(cell_map[i][j] == 0 for j in range(i + 1, width))


@pytest.mark.parametrize(
    ("architecture", "prefix_cells", "logic_levels", "max_fanout"),
    [
        # Textbook figures for a 16-bit adder (N = 16, log2 N = 4).
        ("ripple", 15, 15, 1),
        # Brent-Kung: 2N - log2(N) - 2 cells, 2*log2(N) - 2 levels.
        ("brent-kung", 26, 6, 4),
        # Han-Carlson: half of Kogge-Stone's cells, log2(N) + 1 levels.
        ("han-carlson", 32, 5, 4),
        # Sklansky: (N/2)*log2(N) cells, log2(N) levels, N/2 max fanout.
        ("sklansky", 32, 4, 8),
        # Kogge-Stone: N*log2(N) - N + 1 cells, log2(N) levels, fanout <= 2
        # per *stage* (this metric counts total prefix-cell loads).
        ("kogge-stone", 49, 4, 4),
    ],
)
def test_16_bit_metrics_match_textbook_values(
    architecture, prefix_cells, logic_levels, max_fanout
):
    """The whole point of the lever is that the five structures really do
    trade cells against levels -- if a constructor silently degenerated into
    another architecture these numbers would collide."""
    graph = build_prefix_graph(cell_map_for_architecture(architecture, 16))
    assert graph.prefix_cells == prefix_cells
    assert graph.logic_levels == logic_levels
    assert graph.max_fanout == max_fanout


def test_architectures_are_distinct_at_16_bits():
    signatures = {
        architecture: tuple(
            tuple(row) for row in cell_map_for_architecture(architecture, 16)
        )
        for architecture in ARCHITECTURES
    }
    assert len(set(signatures.values())) == len(ARCHITECTURES)


# --------------------------------------------------------------------------- #
# Cell-map validation and legalisation
# --------------------------------------------------------------------------- #


def test_build_prefix_graph_accepts_a_hand_written_cell_map():
    """A 4-bit ripple map, written out by hand, legalises to the expected
    wiring: (i, 0) = (i, i) o (i - 1, 0)."""
    cell_map = [
        [1, 0, 0, 0],
        [1, 1, 0, 0],
        [1, 0, 1, 0],
        [1, 0, 0, 1],
    ]
    graph = build_prefix_graph(cell_map)
    assert graph.prefix_cells == 3
    assert graph.logic_levels == 3
    for i in range(1, 4):
        node = graph.by_key[(i, 0)]
        assert node.upper == (i, i)
        assert node.lower == (i - 1, 0)


def test_build_prefix_graph_rejects_a_map_with_a_missing_lower_input():
    """(3, 0) here would need (1, 0), which the map does not contain -- an
    illegal map, rejected by name rather than silently repaired."""
    cell_map = [
        [1, 0, 0, 0],
        [0, 1, 0, 0],
        [1, 0, 1, 0],
        [1, 0, 1, 1],
    ]
    with pytest.raises(ArithGenError) as excinfo:
        build_prefix_graph(cell_map)
    assert "(1, 0)" in str(excinfo.value)


@pytest.mark.parametrize(
    ("cell_map", "fragment"),
    [
        ([], "non-empty list"),
        ([[1, 0]], "square"),
        ([[1, 0], [1, 2]], "must be 0 or 1"),
        ([[1, 1], [1, 1]], "above the diagonal"),
        ([[0, 0], [1, 1]], "must be 1"),
        ([[1, 0], [0, 1]], "the adder needs the carry out"),
    ],
)
def test_validate_cell_map_rejects_malformed_input(cell_map, fragment):
    with pytest.raises(ArithGenError) as excinfo:
        validate_cell_map(cell_map)
    assert fragment in str(excinfo.value)


def test_normalize_architecture_accepts_both_spellings():
    assert normalize_architecture("brent_kung") == "brent-kung"
    assert normalize_architecture("Kogge-Stone") == "kogge-stone"
    with pytest.raises(ArithGenError) as excinfo:
        normalize_architecture("ladner-fischer")
    assert "kogge-stone" in str(excinfo.value)


@pytest.mark.parametrize("width", [0, -1, arith_gen.MAX_WIDTH + 1])
def test_out_of_range_width_is_rejected(width):
    with pytest.raises(ArithGenError):
        cell_map_for_architecture("sklansky", width)


# --------------------------------------------------------------------------- #
# Verilog / techmap emission
# --------------------------------------------------------------------------- #


def test_emitted_adder_declares_its_ports_and_every_prefix_cell():
    built = generate_adder(width=8, architecture="sklansky")
    verilog = built["verilog"]
    assert f"module {default_module_name('sklansky', 8)} (a, b, cin, sum, cout);" in (
        verilog
    )
    assert "assign g0 = a & b;" in verilog
    assert "assign p0 = a ^ b;" in verilog
    assert "assign sum = p0 ^ c[7:0];" in verilog
    assert "assign cout = c[8];" in verilog
    # One (G, P) wire pair per prefix cell, plus the carry recombination.
    assert verilog.count("  assign g_") == built["metrics"]["prefix_cells"]
    assert verilog.count("  assign p_") == built["metrics"]["prefix_cells"]
    assert verilog.count("  assign c[") == 8 + 1


def test_emitted_adder_is_deterministic():
    first = generate_adder(width=16, architecture="han-carlson")["verilog"]
    second = generate_adder(width=16, architecture="han-carlson")["verilog"]
    assert first == second


def test_reference_module_has_the_same_ports_as_the_adder():
    built = generate_adder(width=12, architecture="brent-kung")
    for port in ("a", "b", "cin", "sum", "cout"):
        assert port in built["verilog"]
        assert port in built["reference_verilog"]
    assert "assign {cout, sum} = a + b + cin;" in built["reference_verilog"]


def test_techmap_rule_file_fails_over_for_unhandled_widths():
    verilog = emit_techmap_verilog({16: "klt_add_sklansky_16", 18: "wide_adder"})
    assert "module \\$add (A, B, Y);" in verilog
    assert "wire _TECHMAP_FAIL_ = (Y_WIDTH != 16) && (Y_WIDTH != 18);" in verilog
    assert "if (Y_WIDTH == 16)" in verilog
    assert "else if (Y_WIDTH == 18)" in verilog
    # Both signed and unsigned operands are extended, never assumed.
    assert "A_SIGNED" in verilog
    assert "B_SIGNED" in verilog


def test_techmap_rule_file_needs_at_least_one_width():
    with pytest.raises(ArithGenError):
        emit_techmap_verilog({})


def test_custom_cell_map_round_trips_through_generate_adder():
    cell_map = cell_map_for_architecture("kogge-stone", 8)
    built = generate_adder(width=8, cell_map=cell_map)
    assert built["architecture"] == "custom"
    assert built["module_name"] == "klt_add_custom_8"
    assert built["cell_map"] == cell_map


def test_generate_adder_requires_exactly_one_structure_selector():
    with pytest.raises(ArithGenError):
        generate_adder(width=8)
    with pytest.raises(ArithGenError):
        generate_adder(
            width=8,
            architecture="ripple",
            cell_map=cell_map_for_architecture("ripple", 8),
        )


def test_generate_adder_rejects_a_width_that_contradicts_the_cell_map():
    with pytest.raises(ArithGenError) as excinfo:
        generate_adder(width=9, cell_map=cell_map_for_architecture("ripple", 8))
    assert "does not match" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# run_arith_gen / CLI surface
# --------------------------------------------------------------------------- #


def test_run_arith_gen_writes_every_artifact(tmp_path):
    payload = run_arith_gen(
        width=16, architecture="kogge-stone", output_dir=str(tmp_path)
    )
    assert payload["schema_version"] == arith_gen.SCHEMA_VERSION
    assert payload["architecture"] == "kogge-stone"
    assert payload["width"] == 16
    assert payload["module_name"] == "klt_add_kogge_stone_16"
    assert payload["prefix_cells"] == 49
    assert payload["logic_levels"] == 4
    assert payload["cell_map"] is None, "O(N^2) payload is opt-in"

    for key in (
        "verilog_path",
        "reference_path",
        "testbench_path",
        "techmap_path",
        "equiv_request_path",
    ):
        assert (tmp_path / (payload[key].rsplit("/", 1)[-1])).is_file(), key

    equiv_request = json.loads(
        (tmp_path / "klt_add_kogge_stone_16_equiv_request.json").read_text()
    )
    assert equiv_request["gold"]["top"] == "klt_add_kogge_stone_16_ref"
    assert equiv_request["gate"]["top"] == "klt_add_kogge_stone_16"


def test_run_arith_gen_include_cell_map(tmp_path):
    payload = run_arith_gen(
        width=4,
        architecture="ripple",
        output_dir=str(tmp_path),
        include_cell_map=True,
    )
    assert payload["cell_map"] == cell_map_for_architecture("ripple", 4)


def test_run_arith_gen_reads_a_cell_map_file(tmp_path):
    cell_map = cell_map_for_architecture("brent-kung", 8)
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"cell_map": cell_map}))
    payload = run_arith_gen(
        cell_map_path=str(path),
        output_dir=str(tmp_path),
        module_name="my_adder",
    )
    assert payload["architecture"] == "custom"
    assert payload["module_name"] == "my_adder"
    assert payload["width"] == 8
    assert (tmp_path / "my_adder.v").is_file()


def test_run_arith_gen_rejects_arch_and_cell_map_together(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(json.dumps(cell_map_for_architecture("ripple", 4)))
    with pytest.raises(ArithGenError) as excinfo:
        run_arith_gen(
            width=4,
            architecture="ripple",
            cell_map_path=str(path),
            output_dir=str(tmp_path),
        )
    assert "mutually exclusive" in str(excinfo.value)


def test_run_arith_gen_requires_a_structure_selector():
    with pytest.raises(ArithGenError):
        run_arith_gen(width=8)


def test_run_arith_gen_reports_a_missing_cell_map_file(tmp_path):
    with pytest.raises(ArithGenError) as excinfo:
        run_arith_gen(cell_map_path=str(tmp_path / "nope.json"))
    assert "not found" in str(excinfo.value)


def test_cli_parser_registers_arith_gen(tmp_path):
    args = create_parser().parse_args(
        [
            "arith-gen",
            "--width",
            "8",
            "--arch",
            "sklansky",
            "--output-dir",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    assert args.width == 8
    assert args.arch == "sklansky"
    assert args.format == "json"
    assert args.func.__module__.endswith("arith_gen_cmd")


def test_cli_run_emits_json(tmp_path, capsys):
    from klayout_tools.cli import arith_gen_cmd

    args = create_parser().parse_args(
        [
            "arith-gen",
            "--width",
            "8",
            "--arch",
            "brent-kung",
            "--output-dir",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    assert arith_gen_cmd.run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["architecture"] == "brent-kung"
    assert payload["module_name"] == "klt_add_brent_kung_8"


def test_cli_run_reports_errors_through_the_envelope(tmp_path, capsys):
    from klayout_tools.cli import arith_gen_cmd

    args = create_parser().parse_args(
        ["arith-gen", "--width", "0", "--arch", "ripple", "--format", "json"]
    )
    assert arith_gen_cmd.run(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["command"] == "arith-gen"


def test_cli_text_rendering_includes_the_cell_map(tmp_path, capsys):
    from klayout_tools.cli import arith_gen_cmd

    args = create_parser().parse_args(
        [
            "arith-gen",
            "--width",
            "4",
            "--arch",
            "ripple",
            "--output-dir",
            str(tmp_path),
            "--include-cell-map",
        ]
    )
    assert arith_gen_cmd.run(args) == 0
    out = capsys.readouterr().out
    assert "architecture: ripple" in out
    assert "cell_map" in out


# --------------------------------------------------------------------------- #
# Integration: real Yosys (`klt equiv`) and real Icarus
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.parametrize("architecture", ARCHITECTURES)
@pytest.mark.parametrize("width", [8, 16])
def test_integration_klt_equiv_proves_the_generated_adder(
    architecture, width, tmp_path
):
    """Issue #1722 acceptance criterion 1, run for real: the emitted Verilog
    is proven equivalent to `a + b` by the existing `klt equiv` gate."""
    from klayout_tools.equiv import run_equiv

    payload = run_arith_gen(
        width=width, architecture=architecture, output_dir=str(tmp_path)
    )
    report = run_equiv(payload["equiv_request_path"], timeout_s=120)
    assert report["status"] == "equivalent", report


@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
@pytest.mark.parametrize("architecture", ["ripple", "sklansky", "kogge-stone"])
def test_integration_generated_testbench_passes(architecture, tmp_path):
    payload = run_arith_gen(
        width=6, architecture=architecture, output_dir=str(tmp_path)
    )
    binary = tmp_path / "tb.out"
    compile_result = subprocess.run(
        [
            "iverilog",
            "-o",
            str(binary),
            payload["verilog_path"],
            payload["reference_path"],
            payload["testbench_path"],
        ],
        capture_output=True,
        text=True,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    run_result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run_result.returncode == 0, run_result.stderr
    assert "PASS:" in run_result.stdout, run_result.stdout
    assert "MISMATCH" not in run_result.stdout
