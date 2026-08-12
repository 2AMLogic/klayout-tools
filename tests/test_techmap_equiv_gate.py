"""Real, unmocked tests for the technology-mapping acceptance gate
(issue #875, Phase 2c of Epic #704): a mapped gate-level netlist is
accepted only when `klt equiv` proves it logically equivalent to the
pre-mapping generic netlist it was produced from.

This mirrors `tests/test_synthesize_equiv_gate.py` (issue #808), which
wired the same gate one stage earlier (RTL vs. the synthesized netlist),
including its two-tier structure and its "never hand-edit gate-level
Verilog" discipline for producing the seeded mismatch:

- **Happy path**: the checked-in `adder4_generic.json` corpus fixture
  (`tests/corpus/techmap/`, a real Yosys-derived generic netlist for a
  4-bit ripple-carry adder -- combinational, so in scope for `klt equiv`'s
  Phase 0 MVP) is gated against `adder4_mapped.v`, the real
  `native/techmap` mapper's own output for that exact fixture. The gate
  runs a real Yosys SAT proof and reports `"equivalent"`.
- **Seeded mismatch**: `adder4_broken_mapped.v` is the *same real mapper*
  run over the generic netlist of a deliberately broken adder (the carry-in
  dropped from the sum -- a real "forgot the carry-in" bug class). It is a
  structurally valid, genuinely-wrong mapper output, substituted in place
  of the good design's own mapped netlist to simulate a mapping stage whose
  output diverges from its declared generic-netlist source. The gate must
  catch it: a hard `TechmapError`, not a silent warning or a pass.

The gate tiers below are not skipped in this repo's own CI
(`.github/workflows/ci.yml` provisions a pinned real Yosys 0.67, a pinned
real Icarus Verilog 13.0, and a real sky130_fd_sc_hd liberty, with
`PDK_ROOT`/`PDK` pointed at it, on every run) -- the `skipif` guards exist
only for a developer machine lacking one of those, matching
`tests/test_synthesize_equiv_gate.py`'s own posture. The renderer tests in
the first section need no external tool at all and always run.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from klayout_tools import synthesize, techmap
from klayout_tools.equiv import EquivError
from klayout_tools.techmap import (
    TechmapError,
    generic_netlist_to_verilog,
    run_techmap,
    verify_mapping_equivalence,
)

CORPUS_DIR = os.path.join(os.path.dirname(__file__), "corpus", "techmap")

HAVE_YOSYS = shutil.which("yosys") is not None
HAVE_IVERILOG = shutil.which("iverilog") is not None


def _resolve_sky130_liberty() -> str | None:
    """The exact liberty the checked-in mapped fixtures were mapped
    against, or ``None`` if this machine has no sky130_fd_sc_hd install.

    Deliberately not the "any cell library that resolves" search
    `tests/test_synthesize_equiv_gate.py` uses: the mapped fixtures below
    instantiate real `sky130_fd_sc_hd__*` cells, so only that library's own
    liberty can turn them into logic for the proof.
    """
    try:
        liberty_path, _corner, _info = synthesize._resolve_liberty(
            "sky130_fd_sc_hd", "tt_025C_1v80"
        )
    except Exception:
        return None
    return liberty_path


_SKY130_LIBERTY = _resolve_sky130_liberty()

requires_yosys = pytest.mark.skipif(
    not HAVE_YOSYS, reason="yosys is not installed on this machine"
)
requires_iverilog = pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
requires_liberty = pytest.mark.skipif(
    _SKY130_LIBERTY is None,
    reason="no sky130_fd_sc_hd tt_025C_1v80 liberty resolves on this machine",
)


# ---------------------------------------------------------------------------
# The generic-netlist -> Verilog rendering the gate's `gold` side depends on.
# No external tool needed; these always run.
# ---------------------------------------------------------------------------


def _netlist(cells: list[dict], ports: list[dict] | None = None) -> dict:
    return {
        "schema": "klt.synth.generic-netlist/1",
        "top": "unit",
        "ports": ports
        or [
            {"name": "a", "direction": "input", "bits": ["a"]},
            {"name": "b", "direction": "input", "bits": ["b"]},
            {"name": "y", "direction": "output", "bits": ["y"]},
        ],
        "cells": cells,
    }


@pytest.mark.parametrize(
    ("cell_type", "expected"),
    [
        ("$not", "assign y = ~a;"),
        ("$buf", "assign y = a;"),
        ("$and2", "assign y = a & b;"),
        ("$nand2", "assign y = ~(a & b);"),
        ("$or2", "assign y = a | b;"),
        ("$nor2", "assign y = ~(a | b);"),
        ("$xor2", "assign y = a ^ b;"),
        ("$xnor2", "assign y = ~(a ^ b);"),
    ],
)
def test_each_generic_primitive_renders_its_contract_function(cell_type, expected):
    """Every combinational primitive renders the exact Boolean function
    `docs/design/synth-techmap-stage-contract.md` section 2.2 defines for
    it -- the gate's `gold` side is only meaningful if this rendering is
    faithful."""
    connections = {"A": "a", "Y": "y"}
    if cell_type not in ("$not", "$buf"):
        connections["B"] = "b"
    text = generic_netlist_to_verilog(
        _netlist([{"name": "_g0_", "type": cell_type, "connections": connections}])
    )
    assert expected in text


def test_mux2_renders_contract_polarity():
    """The contract defines `$mux2` as `Y = S ? B : A` -- the opposite
    polarity would still simulate, and would still be "a mux", while
    silently making every gated proof meaningless."""
    text = generic_netlist_to_verilog(
        _netlist(
            [
                {
                    "name": "_g0_",
                    "type": "$mux2",
                    "connections": {"A": "a", "B": "b", "S": "s", "Y": "y"},
                }
            ],
            ports=[
                {"name": "a", "direction": "input", "bits": ["a"]},
                {"name": "b", "direction": "input", "bits": ["b"]},
                {"name": "s", "direction": "input", "bits": ["s"]},
                {"name": "y", "direction": "output", "bits": ["y"]},
            ],
        )
    )
    assert "assign y = s ? b : a;" in text


def test_port_and_wire_declarations_match_the_mapped_netlist_dialect():
    """Port declarations follow `native/techmap`'s own `verilog.rs`
    `port_decl` rule exactly (scalar when the single bit net *is* the port
    name, `[len-1:0]` otherwise), so both sides of the miter present the
    same interface and no `port_map` is needed. Internal nets are declared
    as wires."""
    text = generic_netlist_to_verilog(
        {
            "schema": "klt.synth.generic-netlist/1",
            "top": "adder1",
            "ports": [
                {"name": "a", "direction": "input", "bits": ["a[1]", "a[0]"]},
                {"name": "cin", "direction": "input", "bits": ["cin"]},
                {"name": "y", "direction": "output", "bits": ["y"]},
            ],
            "cells": [
                {
                    "name": "_g0_",
                    "type": "$xor2",
                    "connections": {"A": "a[0]", "B": "a[1]", "Y": "n1"},
                },
                {
                    "name": "_g1_",
                    "type": "$xor2",
                    "connections": {"A": "n1", "B": "cin", "Y": "y"},
                },
            ],
        }
    )
    assert "module adder1 (a, cin, y);" in text
    assert "  input [1:0] a;" in text
    assert "  input cin;" in text
    assert "  output y;" in text
    assert "  wire n1;" in text
    # A port bit is referenced verbatim (it is already a legal bit-select
    # into the declared bus) -- never re-declared as its own wire.
    assert "  wire a[0];" not in text
    assert "assign n1 = a[0] ^ a[1];" in text


def test_port_bits_outside_the_dense_msb_first_convention_are_rejected():
    """A compact `[msb:0]` declaration is only faithful if the port's bit
    nets really are that bus's own bits in MSB-first order. A port that
    breaks the convention must fail loudly, not silently render Verilog
    whose bit references mean something else than the producer intended."""
    with pytest.raises(TechmapError, match="dense MSB-first convention"):
        generic_netlist_to_verilog(
            {
                "schema": "klt.synth.generic-netlist/1",
                "top": "odd",
                "ports": [
                    {"name": "a", "direction": "input", "bits": ["hi", "lo"]},
                    {"name": "y", "direction": "output", "bits": ["y"]},
                ],
                "cells": [
                    {
                        "name": "_g0_",
                        "type": "$and2",
                        "connections": {"A": "hi", "B": "lo", "Y": "y"},
                    }
                ],
            }
        )


def test_reserved_constant_nets_render_as_verilog_literals():
    """`"$const0"`/`"$const1"` are the contract's reserved tie-net names
    (section 8) -- the mapped side resolves them to a real tie cell, so the
    gold side must resolve them to the matching literal or every design
    using a tie net would fail the gate spuriously."""
    text = generic_netlist_to_verilog(
        _netlist(
            [
                {
                    "name": "_g0_",
                    "type": "$and2",
                    "connections": {"A": "$const1", "B": "$const0", "Y": "y"},
                }
            ]
        )
    )
    assert "assign y = 1'b1 & 1'b0;" in text


def test_non_identifier_net_names_are_escaped():
    """An internal net whose name is not a plain identifier (a bus-bit name
    that is not a declared port bit, a `$`-prefixed Yosys-derived name)
    becomes a Verilog escaped identifier rather than illegal Verilog."""
    text = generic_netlist_to_verilog(
        _netlist(
            [
                {
                    "name": "_g0_",
                    "type": "$buf",
                    "connections": {"A": "a", "Y": "$auto$n[3]"},
                },
                {
                    "name": "_g1_",
                    "type": "$buf",
                    "connections": {"A": "$auto$n[3]", "Y": "y"},
                },
            ]
        )
    )
    assert "  wire \\$auto$n[3] ;" in text
    assert "assign \\$auto$n[3]  = a;" in text


def test_dff_is_rejected_with_an_actionable_scope_error():
    """`klt equiv`'s Phase 0 MVP is combinational-only, so a sequential
    generic netlist cannot be gated today. It must fail with a clear scope
    error naming that limitation -- not emit sequential Verilog for Yosys
    to reject later with a less actionable message."""
    with pytest.raises(TechmapError, match="combinational-only") as exc_info:
        generic_netlist_to_verilog(
            _netlist(
                [
                    {
                        "name": "_g0_",
                        "type": "$dff",
                        "connections": {"D": "a", "CLK": "b", "Q": "y"},
                    }
                ]
            )
        )
    assert "#707" in str(exc_info.value)


def test_cell_type_outside_the_vocabulary_is_rejected():
    with pytest.raises(TechmapError, match="outside the generic cell vocabulary"):
        generic_netlist_to_verilog(
            _netlist(
                [
                    {
                        "name": "_g0_",
                        "type": "$aoi21",
                        "connections": {"A": "a", "Y": "y"},
                    }
                ]
            )
        )


def test_cell_missing_a_pin_is_rejected():
    with pytest.raises(TechmapError, match="missing its 'B' pin"):
        generic_netlist_to_verilog(
            _netlist(
                [{"name": "_g0_", "type": "$and2", "connections": {"A": "a", "Y": "y"}}]
            )
        )


def test_foreign_schema_is_rejected(tmp_path):
    path = tmp_path / "netlist.json"
    path.write_text(json.dumps({"schema": "something/else"}), encoding="utf-8")
    with pytest.raises(TechmapError, match="declares schema"):
        techmap.load_generic_netlist(str(path))


# ---------------------------------------------------------------------------
# The gate itself, against real Yosys, a real liberty, and the real mapper's
# own checked-in output. AC #1 and AC #2.
# ---------------------------------------------------------------------------


def _stage(tmp_path, generic_fixture: str, mapped_fixture: str) -> tuple[str, str]:
    """Copy a (generic netlist, mapped netlist) fixture pair into
    ``tmp_path`` so the gate's own `.klt/` artifacts never land in the
    checked-in corpus directory."""
    generic_path = tmp_path / "adder4_generic.json"
    mapped_path = tmp_path / "adder4_mapped.v"
    shutil.copy(os.path.join(CORPUS_DIR, generic_fixture), generic_path)
    shutil.copy(os.path.join(CORPUS_DIR, mapped_fixture), mapped_path)
    return str(generic_path), str(mapped_path)


def test_corpus_fixtures_are_the_real_mapper_output():
    """Guards the premise of both gate tests below: the checked-in mapped
    netlists really are `native/techmap`'s own output (its generated-by
    banner), not hand-written Verilog -- the discipline issue #808
    established for the RTL-stage gate's fixtures."""
    for name in ("adder4_mapped.v", "adder4_broken_mapped.v"):
        with open(os.path.join(CORPUS_DIR, name), encoding="utf-8") as handle:
            assert "Generated by klt-techmap" in handle.readline()


@requires_yosys
@requires_iverilog
@requires_liberty
def test_gate_accepts_a_real_mapped_netlist(tmp_path):
    """AC #1: a technology-mapped netlist is checked against `klt equiv`
    for equivalence to its pre-mapping generic netlist -- run for real
    (real Yosys `miter -equiv` + SAT proof) against the real mapper's own
    output for that exact generic netlist."""
    generic_path, mapped_path = _stage(
        tmp_path, "adder4_generic.json", "adder4_mapped.v"
    )

    equivalence = verify_mapping_equivalence(
        generic_netlist_path=generic_path,
        mapped_netlist_path=mapped_path,
        liberty_path=_SKY130_LIBERTY,
        output_dir=str(tmp_path / "gate"),
    )

    assert equivalence["status"] == "equivalent"
    assert equivalence["engine"] == "yosys"
    assert equivalence["artifacts"]["script_path"]
    assert os.path.isfile(equivalence["generic_netlist_verilog_path"])


@requires_yosys
@requires_iverilog
@requires_liberty
def test_gate_rejects_a_seeded_broken_mapping(tmp_path):
    """AC #2: a deliberately-broken mapping is caught by the gate, proven
    rather than assumed.

    The "broken mapping" is the real mapper's own output for a *different*
    (carry-in-dropped) generic netlist, substituted in place of the good
    design's mapped netlist -- structurally valid, genuinely wrong, never
    hand-edited gate-level Verilog (which risks producing something Yosys
    refuses to parse instead of a real logical divergence)."""
    generic_path, mapped_path = _stage(
        tmp_path, "adder4_generic.json", "adder4_broken_mapped.v"
    )

    with pytest.raises(TechmapError, match="NOT equivalent") as exc_info:
        verify_mapping_equivalence(
            generic_netlist_path=generic_path,
            mapped_netlist_path=mapped_path,
            liberty_path=_SKY130_LIBERTY,
            output_dir=str(tmp_path / "gate"),
        )

    message = str(exc_info.value)
    assert "counterexample" in message
    assert "sum" in message
    # The counterexample was re-run through both netlists with iverilog/vvp,
    # not merely reported by the SAT solver (`klt equiv`'s own "the
    # counterexample is executable" discipline).
    assert "confirmed_by_simulation=True" in message


def test_gate_rejects_an_inconclusive_verdict(tmp_path, monkeypatch):
    """A solver/process timeout is never a pass: an `"inconclusive"`
    verdict fails the gate as hard as a proven counterexample, so a mapped
    netlist can never be accepted on a proof that did not finish.

    `klt equiv` reaching that verdict for real needs a design hard enough
    to time out (inherently machine-dependent), so the *policy* is tested
    here against a stubbed verdict -- the two tests above already exercise
    the real solver on real artifacts."""
    generic_path, mapped_path = _stage(
        tmp_path, "adder4_generic.json", "adder4_mapped.v"
    )
    liberty_path = tmp_path / "stub.lib"
    liberty_path.write_text(
        "/* not read -- run_equiv is stubbed */\n", encoding="utf-8"
    )

    def _inconclusive(request_path, *, timeout_s=None):
        return {
            "status": "inconclusive",
            "artifacts": {"log_path": None},
            "diagnostics": [
                {
                    "severity": "error",
                    "code": "solver_timeout",
                    "message": (
                        "yosys's internal SAT solver hit its own --timeout "
                        "before reaching a verdict"
                    ),
                }
            ],
        }

    monkeypatch.setattr(techmap, "run_equiv", _inconclusive)

    with pytest.raises(TechmapError, match="did not reach a verdict") as exc_info:
        verify_mapping_equivalence(
            generic_netlist_path=generic_path,
            mapped_netlist_path=mapped_path,
            liberty_path=str(liberty_path),
            output_dir=str(tmp_path / "gate"),
        )
    assert "SAT solver" in str(exc_info.value)


def test_gate_surfaces_an_equiv_error_as_a_hard_failure(tmp_path, monkeypatch):
    """An `EquivError` (the check could not be completed at all) is also a
    hard failure -- never a silently-accepted mapping."""
    generic_path, mapped_path = _stage(
        tmp_path, "adder4_generic.json", "adder4_mapped.v"
    )
    liberty_path = tmp_path / "stub.lib"
    liberty_path.write_text(
        "/* not read -- run_equiv is stubbed */\n", encoding="utf-8"
    )

    def _raise(request_path, *, timeout_s=None):
        raise EquivError("could not launch yosys")

    monkeypatch.setattr(techmap, "run_equiv", _raise)

    with pytest.raises(TechmapError, match="could not be completed"):
        verify_mapping_equivalence(
            generic_netlist_path=generic_path,
            mapped_netlist_path=mapped_path,
            liberty_path=str(liberty_path),
            output_dir=str(tmp_path / "gate"),
        )


# ---------------------------------------------------------------------------
# End-to-end: the gate wired into the stage's own runner, so a mapped
# netlist is gated *before* the response is returned to a caller.
# ---------------------------------------------------------------------------


def _techmap_binary() -> str | None:
    try:
        return techmap._resolve_binary(None)
    except TechmapError:
        return None


_TECHMAP_BINARY = _techmap_binary()

requires_mapper = pytest.mark.skipif(
    _TECHMAP_BINARY is None,
    reason=(
        "klt-techmap is not built on this machine "
        "(cargo build --release in native/techmap/)"
    ),
)


def _write_request(tmp_path, generic_fixture: str) -> str:
    shutil.copy(
        os.path.join(CORPUS_DIR, generic_fixture), tmp_path / "adder4_generic.json"
    )
    request_path = tmp_path / "techmap_request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema": "klt.synth.techmap.request/1",
                "generic_netlist": "adder4_generic.json",
                "liberty": _SKY130_LIBERTY,
                "constraints": {"clock_period_ns": None},
            }
        ),
        encoding="utf-8",
    )
    return str(request_path)


@requires_yosys
@requires_iverilog
@requires_liberty
@requires_mapper
def test_run_techmap_attaches_the_gate_verdict(tmp_path):
    """The runner gates before returning: a real mapping run with
    `verify_equivalence=True` returns its response with the proof attached
    under `equivalence`, and the same run with the gate off returns
    `None` (additive, opt-in -- unchanged behaviour for existing callers of
    the standalone binary)."""
    request_path = _write_request(tmp_path, "adder4_generic.json")

    gated = run_techmap(request_path, verify_equivalence=True)
    assert gated["status"] == "ok"
    assert gated["instance_count"] > 0
    assert gated["equivalence"]["status"] == "equivalent"

    ungated = run_techmap(request_path)
    assert ungated["equivalence"] is None


@requires_yosys
@requires_iverilog
@requires_liberty
@requires_mapper
def test_run_techmap_rejects_a_seeded_mismatch(tmp_path, monkeypatch):
    """The seeded mismatch again, but through the runner: after the real
    mapper produces the correct netlist, the file the gate is about to
    check is overwritten with the broken variant's real mapper output
    (mirroring `tests/test_synthesize_equiv_gate.py`'s own `_run_yosys`
    monkeypatch). `run_techmap` must refuse to return a response at all --
    a mapped netlist the gate cannot prove equivalent is not accepted."""
    request_path = _write_request(tmp_path, "adder4_generic.json")

    with open(
        os.path.join(CORPUS_DIR, "adder4_broken_mapped.v"), encoding="utf-8"
    ) as handle:
        broken_text = handle.read()

    original_run = techmap.subprocess.run

    def _seed_mismatch_after_mapping(args, **kwargs):
        completed = original_run(args, **kwargs)
        if args and str(args[0]).endswith(techmap.TECHMAP_BINARY_NAME):
            response = json.loads(completed.stdout)
            with open(response["mapped_netlist_path"], "w", encoding="utf-8") as handle:
                handle.write(broken_text)
        return completed

    monkeypatch.setattr(techmap.subprocess, "run", _seed_mismatch_after_mapping)

    with pytest.raises(TechmapError, match="NOT equivalent"):
        run_techmap(request_path, verify_equivalence=True)
