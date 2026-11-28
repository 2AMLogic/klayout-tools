"""SDF ``TIMESCALE`` versus the annotated module's time unit (issue #7983).

Icarus ignores an SDF's ``TIMESCALE``, so ``klt`` checks the HDL unit for
each retained delay and refuses a differing unit before the
build. A dispatch spy on the public Verilog-bench request verifies a
refusal precedes compilation. A real Icarus run checks that a matching,
preprocessed model still annotates successfully.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers.tool_probes import SKIP_NO_ICARUS as needs_icarus
from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_sdf as sdf
from klayout_tools import functional_verification_sdf_unit_syntax as syntax
from test_functional_verification_verilog_top import EXAMPLE_DIR, _stage_sdf_units


class _DispatchSpy:
    """Stands in for ``_run_verilog_top``: records what the pipeline would
    have built and returns a bare report."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, verilog_tb, **kwargs):
        self.calls.append(kwargs)
        return fv._Report({"environment": {}})


def _refused(tmp_path, monkeypatch, request: str) -> str:
    """Run ``request`` and return the refusal's message, having checked
    that nothing was dispatched or built."""
    spy = _DispatchSpy()
    monkeypatch.setattr(fv, "_run_verilog_top", spy)
    with pytest.raises(fv.FunctionalVerificationError) as excinfo:
        fv.run_functional_verification(request)
    assert spy.calls == []
    artifacts = tmp_path / ".klt" / "functional-verification"
    assert not (artifacts / "sim_build_icarus").exists()
    assert not (artifacts / "build_icarus.log").exists()
    return str(excinfo.value)


def _dispatched_annotation(monkeypatch, request: str) -> dict:
    """The ``sdf`` dict the Verilog-bench dispatch receives for ``request``."""
    spy = _DispatchSpy()
    monkeypatch.setattr(fv, "_run_verilog_top", spy)
    fv.run_functional_verification(request)
    assert len(spy.calls) == 1
    return spy.calls[0]["sdf"]


def _sdf(timescale: str, *cells: str) -> str:
    return (
        "(DELAYFILE\n"
        '  (SDFVERSION "3.0")\n'
        '  (DESIGN "and2")\n'
        "  (DIVIDER .)\n"
        f"  (TIMESCALE {timescale})\n" + "".join(cells) + ")\n"
    )


_ROOT_CELL = """\
  (CELL (CELLTYPE "and2") (INSTANCE)
    (DELAY (ABSOLUTE (INTERCONNECT a u1.A (0.5:0.5:0.5)))))
"""
_LEAF_CELL = """\
  (CELL (CELLTYPE "and_cell") (INSTANCE u1)
    (DELAY (ABSOLUTE (IOPATH A Y (0.5:0.5:0.5)))))
"""


# -- mismatch ---------------------------------------------------------------


def test_ps_sdf_on_ns_models_is_refused_before_the_build(tmp_path, monkeypatch):
    request = _stage_sdf_units(tmp_path, sdf_timescale="1ps", delay="500")
    message = _refused(tmp_path, monkeypatch, request)
    assert "the SDF file is in 1ps" in message
    assert "'u1' (CELLTYPE \"and_cell\", `timescale 1ns)" in message
    assert "1000x too large" in message


def test_10ps_sdf_on_a_1ns_10ps_leaf_is_refused(tmp_path, monkeypatch):
    """A leaf's ``1ns/10ps`` declaration has a ``1ns`` unit: the precision is
    not the unit, and the SDF's ``10`` multiplier is part of its own."""
    request = _stage_sdf_units(
        tmp_path, sdf_timescale="10ps", delay="5", leaf_timescale="1ns/10ps"
    )
    message = _refused(tmp_path, monkeypatch, request)
    assert "the SDF file is in 10ps" in message
    assert "'u1' (CELLTYPE \"and_cell\", `timescale 1ns)" in message
    assert "100x too large" in message


def test_sdf_without_a_timescale_is_read_as_1ns(tmp_path, monkeypatch):
    """The SDF standard's default unit, so a ``1ps`` model is refused with the
    delays read as too small rather than too large."""
    request = _stage_sdf_units(tmp_path, sdf_timescale=None, leaf_timescale="1ps/1ps")
    message = _refused(tmp_path, monkeypatch, request)
    assert "the SDF file is in 1ns" in message
    assert "'u1' (CELLTYPE \"and_cell\", `timescale 1ps)" in message
    assert "1000x too small" in message


def test_unparseable_timescale_is_refused(tmp_path, monkeypatch):
    request = _stage_sdf_units(tmp_path, sdf_timescale="2.5ns")
    message = _refused(tmp_path, monkeypatch, request)
    assert "TIMESCALE '2.5ns'" in message


# -- unresolved -------------------------------------------------------------


def test_leaf_without_a_timescale_is_unresolved_on_the_verilog_bench(
    tmp_path, monkeypatch
):
    """Nothing supplies a default unit on this path: ``testbench.verilog_top``
    rejects ``options.timescale``, and the leaf is the first source, so no
    earlier directive reaches it."""
    request = _stage_sdf_units(tmp_path, leaf_timescale=None)
    message = _refused(tmp_path, monkeypatch, request)
    assert "'u1' (CELLTYPE \"and_cell\")" in message
    assert "effective time unit of module 'and_cell' could not be resolved" in message
    assert "testbench.verilog_top rejects options.timescale" in message


def test_undeclared_celltype_is_unresolved(tmp_path, monkeypatch):
    """A CELLTYPE absent from the build sources cannot be verified."""
    stock = (EXAMPLE_DIR / "and2.sdf").read_text()
    request = _stage_sdf_units(
        tmp_path,
        sdf_text=stock.replace('CELLTYPE "and_cell"', 'CELLTYPE "ghost_cell"'),
    )
    message = _refused(tmp_path, monkeypatch, request)
    assert "'u1' (CELLTYPE \"ghost_cell\")" in message
    assert "no module 'ghost_cell' is declared in request.sources" in message


def test_delay_outside_any_cell_is_compared_with_the_annotation_root(
    tmp_path, monkeypatch
):
    request = _stage_sdf_units(
        tmp_path,
        sdf_text=_sdf("1ps", "  (DELAY (ABSOLUTE (IOPATH a y (500:500:500))))\n"),
    )
    message = _refused(tmp_path, monkeypatch, request)
    assert "annotation root 'and2'" in message
    assert "`timescale 1ns)" in message


def test_cell_without_a_celltype_is_unresolved(tmp_path, monkeypatch):
    """A ``CELL`` that names no module is refused, not taken for the
    annotation root."""
    request = _stage_sdf_units(
        tmp_path,
        sdf_text=_sdf(
            "1ns",
            "  (CELL (INSTANCE u1)\n"
            "    (DELAY (ABSOLUTE (IOPATH A Y (0.5:0.5:0.5)))))\n",
        ),
    )
    message = _refused(tmp_path, monkeypatch, request)
    assert "'u1' (no CELLTYPE): the CELL names no module to compare" in message


# -- which targets are compared ---------------------------------------------


def test_mixed_scope_refusal_names_only_the_mismatched_cell(tmp_path, monkeypatch):
    """The root ``CELL`` matches the SDF and is not named; the one retained
    delay onto the ``1ps`` leaf is."""
    request = _stage_sdf_units(
        tmp_path, leaf_timescale="1ps/1ps", sdf_text=_sdf("1ns", _ROOT_CELL, _LEAF_CELL)
    )
    message = _refused(tmp_path, monkeypatch, request)
    assert 'CELLTYPE "and_cell", `timescale 1ps)' in message
    assert 'CELLTYPE "and2"' not in message


def test_matching_mixed_scope_reaches_the_build(tmp_path, monkeypatch):
    """The root's INTERCONNECT matches the shim even when the leaf uses ps."""
    request = _stage_sdf_units(
        tmp_path, leaf_timescale="1ps/1ps", sdf_text=_sdf("1ns", _ROOT_CELL)
    )
    annotation = _dispatched_annotation(monkeypatch, request)
    assert (annotation["timescale"], annotation["time_unit"]) == ("1ns", "1ns")


def test_timingcheck_only_cell_is_not_a_delay_target(tmp_path, monkeypatch):
    """Icarus applies no ``TIMINGCHECK`` value, so a cell carrying only one
    receives no delay and its unit is not compared."""
    only_check = """\
  (CELL (CELLTYPE "and_cell") (INSTANCE u1)
    (TIMINGCHECK (SETUP A (posedge B) (1:1:1))))
"""
    request = _stage_sdf_units(
        tmp_path, leaf_timescale="1ps/1ps", sdf_text=_sdf("1ns", only_check)
    )
    annotation = _dispatched_annotation(monkeypatch, request)
    assert (annotation["timescale"], annotation["time_unit"]) == ("1ns", None)


# -- the unit scan ----------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("1ns", "1ns", True),
        ("100ps", "100ps", True),
        ("1ns", "1000ps", True),
        ("1ns", "1e3ps", True),
        ("1ns", "1ps", False),
        ("10ps", "1ns", False),
        ("10ps", "100ps", False),
    ],
)
def test_unit_comparison_includes_the_multiplier(left, right, same):
    assert syntax._same_time_unit(left, right) is same


@pytest.mark.parametrize(
    "prefix", ['(* keep_hierarchy = "yes" *) ', "module other; endmodule "]
)
def test_module_declaration_after_an_attribute_or_endmodule(
    tmp_path, monkeypatch, prefix
):
    request = _stage_sdf_units(tmp_path)
    leaf = tmp_path / "and_cell.v"
    leaf.write_text(
        leaf.read_text().replace("module and_cell", prefix + "module and_cell")
    )
    assert _dispatched_annotation(monkeypatch, request)["time_unit"] == "1ns"


def test_module_timeunit_overrides_directive_for_only_that_module(
    tmp_path, monkeypatch
):
    request = _stage_sdf_units(tmp_path)
    leaf = tmp_path / "and_cell.v"
    leaf.write_text(leaf.read_text().replace("  and (", "  timeunit 1ps/1ps;\n  and ("))
    assert "1000x too small" in _refused(tmp_path, monkeypatch, request)
    (tmp_path / "and2.sdf").write_text(_sdf("1ps", _LEAF_CELL))
    assert _dispatched_annotation(monkeypatch, request)["time_unit"] == "1ps"


@needs_icarus
@pytest.mark.parametrize("define_option", ["defines", "source_defines"])
def test_effective_unit_from_included_header_and_active_branch(
    tmp_path, monkeypatch, define_option
):
    request = Path(_stage_sdf_units(tmp_path))
    header_dir = tmp_path / "headers"
    header_dir.mkdir()
    (header_dir / "units.vh").write_text(
        "`ifdef FAST\n`timescale 1ps/1ps\n`endif\n"
        "`ifdef NEVER_SET\n`timescale 1ns/1ps\n`endif\n"
    )
    leaf = tmp_path / "and_cell.v"
    leaf.write_text(
        leaf.read_text().replace(
            "module and_cell", '`include "units.vh"\nmodule and_cell'
        )
    )
    spec = json.loads(request.read_text())
    spec["options"]["includes"] = ["headers"]
    spec["options"][define_option] = (
        {"FAST": None} if define_option == "defines" else {"and_cell.v": {"FAST": None}}
    )
    request.write_text(json.dumps(spec))
    assert "1000x too small" in _refused(tmp_path, monkeypatch, str(request))
    del spec["options"][define_option]
    request.write_text(json.dumps(spec))
    assert _dispatched_annotation(monkeypatch, str(request))["time_unit"] == "1ns"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("(CELL ", "( CELL "),
        ("(DELAY ", "(delay "),
        ("(CELLTYPE ", "( CeLlTyPe "),
        ("(INSTANCE ", "( InStAnCe "),
    ],
)
def test_alternate_sdf_cell_syntax_still_checks_the_leaf(
    tmp_path, monkeypatch, old, new
):
    request = _stage_sdf_units(
        tmp_path,
        leaf_timescale="1ps/1ps",
        sdf_text=_sdf("1ns", _LEAF_CELL.replace(old, new)),
    )
    message = _refused(tmp_path, monkeypatch, request)
    assert "'u1'" in message and "1000x too small" in message


@pytest.mark.parametrize(
    "header",
    [
        "( TiMeScAlE 1ps)",
        "/* (TIMESCALE 1ns) ( */ (TIMESCALE 1ps)",
        "// (TIMESCALE 1ns) )\n (TIMESCALE /* comment */ 1ps)",
        '(PROGRAM "fake (TIMESCALE 1ns) //") (TIMESCALE 1ps)',
    ],
)
def test_actual_sdf_header_sets_the_unit(tmp_path, monkeypatch, header):
    text = _sdf("1ns", _LEAF_CELL).replace("(TIMESCALE 1ns)", header)
    request = _stage_sdf_units(tmp_path, sdf_text=text)
    assert "1000x too large" in _refused(tmp_path, monkeypatch, request)


@pytest.mark.parametrize(
    "invalid",
    [
        "0ps",
        "1.5 ns",
        "1e-1 ns",
        "1.00000000000000001e0 ns",
        ".1e1 ns",
        "1e999999999 ns",
        "1ns/1e-1 ps",
        "1ns extra",
    ],
)
def test_invalid_multiplier_or_trailing_text_is_a_typed_refusal(
    tmp_path, monkeypatch, invalid
):
    request = _stage_sdf_units(tmp_path, sdf_timescale=invalid)
    assert f"TIMESCALE {invalid!r}" in _refused(tmp_path, monkeypatch, request)


def test_each_mismatched_target_has_its_own_factor(tmp_path, monkeypatch):
    request = _stage_sdf_units(
        tmp_path,
        leaf_timescale="1ps/1ps",
        sdf_text=_sdf("10ps", _LEAF_CELL, _ROOT_CELL),
    )
    message = _refused(tmp_path, monkeypatch, request)
    assert 'CELLTYPE "and_cell", `timescale 1ps): 10x too small' in message
    assert (
        "annotation module 'klt_sdf_annotate', `timescale 1ns): 100x too large"
        in message
    )


@needs_icarus
def test_matching_preprocessed_attributed_model_runs_on_icarus(tmp_path):
    request = Path(_stage_sdf_units(tmp_path, sdf_timescale="1ps", delay="500"))
    spec = json.loads(request.read_text())
    spec["options"]["includes"] = ["."]
    request.write_text(json.dumps(spec))
    (tmp_path / "unit.vh").write_text("timeunit 1ps/1ps;\n")
    leaf = tmp_path / "and_cell.v"
    leaf.write_text(
        leaf.read_text()
        .replace("module and_cell", '(* keep = "yes" *) module and_cell')
        .replace("  and (", '  `include "unit.vh"\n  and (')
    )
    report = fv.run_functional_verification(str(request))
    assert report["status"] == "pass"
    annotation = report["environment"]["sdf"]
    assert (annotation["timescale"], annotation["time_unit"]) == ("1ps", "1ps")


@pytest.mark.parametrize(
    "unit,expected", [("1ps", "1ps"), ("1.00 ps", "1ps"), ("1e2 ps", "100ps")]
)
def test_split_sdf_keeps_the_actual_header_unit(tmp_path, unit, expected):
    # The vector endpoint sends this entry to the second annotation file.
    path = tmp_path / "split.sdf"
    path.write_text(
        _sdf("1ps", _ROOT_CELL.replace("a u1.A", "a[0] u1.A")).replace(
            "(TIMESCALE 1ps)", f"/* (TIMESCALE 1ns) */ ( TiMeScAlE {unit})"
        )
    )
    original, deferred = sdf._split_sdf_bus_port_interconnects(str(path), {"a"})
    assert f"( TiMeScAlE {unit})" in original
    assert f"(TIMESCALE {expected})" in deferred


def test_uncertain_compilation_unit_refuses_before_build(tmp_path, monkeypatch):
    request = Path(_stage_sdf_units(tmp_path))
    spec = json.loads(request.read_text())
    spec["options"]["build_args"] = ["-u"]
    request.write_text(json.dumps(spec))
    message = _refused(tmp_path, monkeypatch, str(request))
    assert "'u1'" in message and "options.build_args entry '-u'" in message


@pytest.mark.parametrize("divider", [".", "/"])
def test_same_celltype_in_two_annotated_scopes(tmp_path, monkeypatch, divider):
    # u1 instantiates the global and_cell. In the same DUT, Icarus also
    # instantiates 'branch.and_cell' directly from the nested declarations.
    instance = f"branch{divider}and_cell"
    nested = _LEAF_CELL.replace("INSTANCE u1", f"INSTANCE {instance}")
    text = _sdf("1ns", _LEAF_CELL, nested).replace(
        "(DIVIDER .)", f"( DiViDeR {divider})"
    )
    request = _stage_sdf_units(tmp_path, sdf_text=text)
    dut = tmp_path / "and2.v"
    dut.write_text(
        dut.read_text().replace(
            "  and_cell u1",
            "  module branch; timeunit 1ps/1ps;\n"
            "    module and_cell; endmodule\n"
            "  endmodule\n  and_cell u1",
        )
    )
    message = _refused(tmp_path, monkeypatch, request)
    assert f"'{instance}'" in message and "1000x too small" in message
    assert "'u1'" not in message
    (tmp_path / "and2.sdf").write_text(
        text.replace(_LEAF_CELL, "").replace("1ns", "1ps")
    )
    assert _dispatched_annotation(monkeypatch, request)["time_unit"] == "1ps"


def test_verilog_dut_instance_is_a_nested_annotation_root(tmp_path, monkeypatch):
    root_iopath = _ROOT_CELL.replace("INTERCONNECT a u1.A", "IOPATH a y")
    request = Path(_stage_sdf_units(tmp_path, sdf_text=_sdf("1ns", root_iopath)))
    spec = json.loads(request.read_text())
    spec["testbench"]["dut_instance"] = "tb_and2.and2"
    request.write_text(json.dumps(spec))
    bench = tmp_path / "tb_and2.v"
    bench.write_text(
        bench.read_text().replace(
            "  reg a", "  module and2; timeunit 1ps/1ps; endmodule\n  reg a"
        )
    )
    message = _refused(tmp_path, monkeypatch, str(request))
    assert 'the root CELL (CELLTYPE "and2", `timescale 1ps): 1000x too small' in message
