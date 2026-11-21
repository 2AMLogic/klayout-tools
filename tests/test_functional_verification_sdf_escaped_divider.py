"""Issue #2897: back-annotating a flat post-route SDF whose instance names are
escaped identifiers containing a hierarchy-divider character.

A `generate` array of sub-modules, flattened by synthesis/P&R, becomes
`\\g[0].sub.u` in the netlist and `g\\[0\\]\\.sub\\.u` in the SDF. Icarus 13.0
aborts `vvp` on that shape in an `INTERCONNECT` endpoint (`NULL handle passed
to vpi_scan`) and cannot find it in a `CELL`'s `INSTANCE`, under either
`DIVIDER` -- so `klt` renames such instances to plain identifiers in
build-dir copies of the SDF and netlist, and reports each rename as
`environment.sdf.renamed`.
"""

from __future__ import annotations

import json
import shutil
import types
from pathlib import Path

import pytest

from helpers.cocotb_fakes import FakeCocotbRunner as _FakeRunner
from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_sdf_rename as fvr
from klayout_tools.functional_verification import run_functional_verification


def _have_cocotb() -> bool:
    try:
        import cocotb_tools.runner  # noqa: F401
    except Exception:
        return False
    return True


HAVE_COCOTB = _have_cocotb()
HAVE_ICARUS = shutil.which("iverilog") is not None

_RESULTS_XML = """\
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1785780800" />
    <testcase name="test_one" classname="test_gen_top" time="0.01" \
sim_time_ns="10.0" />
  </testsuite>
</testsuites>
"""

_CELLS_V = """\
module klt_buf (A, Z);
  input A;
  output Z;
  assign Z = A;
  specify
    (A => Z) = (0.0, 0.0);
  endspecify
endmodule
"""


def _netlist_v(sep: str = ".") -> str:
    """A flattened two-element `generate` array of buffers in series, with
    the escaped instance (and net) names a post-route `write_verilog`
    emits; ``sep`` is the character the flattener joined the hierarchy
    with."""
    return f"""\
module gen_top (a, y);
  input a;
  output y;
  wire \\g[0]{sep}sub{sep}n ;
  klt_buf \\g[0]{sep}sub{sep}u (.A(a), .Z(\\g[0]{sep}sub{sep}n ));
  klt_buf \\g[1]{sep}sub{sep}u (.A(\\g[0]{sep}sub{sep}n ), .Z(y));
endmodule
"""


def _sdf_text(divider: str = ".", sep: str = ".") -> str:
    """What `write_sdf` emits for :func:`_netlist_v`: an INTERCONNECT between
    the two escaped instances (3 ns) plus a 1 ns IOPATH on each -- 5 ns from
    `a` to `y` in total."""
    u0 = f"g\\[0\\]\\{sep}sub\\{sep}u"
    u1 = f"g\\[1\\]\\{sep}sub\\{sep}u"
    return f"""\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "gen_top")
  (DIVIDER {divider})
  (TIMESCALE 1ns)
  (CELL
    (CELLTYPE "gen_top")
    (INSTANCE)
    (DELAY (ABSOLUTE
      (INTERCONNECT {u0}{divider}Z {u1}{divider}A (3.000:3.000:3.000))
    ))
  )
  (CELL
    (CELLTYPE "klt_buf")
    (INSTANCE {u0})
    (DELAY (ABSOLUTE
      (IOPATH A Z (1.000:1.000:1.000) (1.000:1.000:1.000))
    ))
  )
  (CELL
    (CELLTYPE "klt_buf")
    (INSTANCE {u1})
    (DELAY (ABSOLUTE
      (IOPATH A Z (1.000:1.000:1.000) (1.000:1.000:1.000))
    ))
  )
)
"""


def _expected_renamed(sep: str = ".") -> list[dict[str, str]]:
    return [
        {"from": f"g[0]{sep}sub{sep}u", "to": "g_0__sub_u"},
        {"from": f"g[1]{sep}sub{sep}u", "to": "g_1__sub_u"},
    ]


_TESTBENCH_PY = '''\
import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def test_y_follows_a_within_two_ns(dut):
    """Passes at zero delay; fails once the 5 ns a->y path is annotated."""
    dut.a.value = 0
    await Timer(10, unit="ns")
    dut.a.value = 1
    await Timer(2, unit="ns")
    assert dut.y.value == 1, f"y={dut.y.value} 2 ns after a rose"


@cocotb.test()
async def test_y_follows_a_within_six_ns(dut):
    """Passes either way: the annotated path is 5 ns."""
    dut.a.value = 0
    await Timer(10, unit="ns")
    dut.a.value = 1
    await Timer(6, unit="ns")
    assert dut.y.value == 1, f"y={dut.y.value} 6 ns after a rose"
'''


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# SDF path parsing
# --------------------------------------------------------------------------- #


def test_split_sdf_path_honors_escapes():
    assert fvr._split_sdf_path("g\\[0\\]\\.sub\\.u.Z", ".") == [
        "g\\[0\\]\\.sub\\.u",
        "Z",
    ]
    assert fvr._split_sdf_path("top/g\\.u/Z", "/") == ["top", "g\\.u", "Z"]
    assert fvr._split_sdf_path("a", ".") == ["a"]


@pytest.mark.parametrize(
    ("component", "divider", "expected"),
    [
        ("g\\[0\\]\\.sub\\.u", ".", True),
        # Icarus mis-splits an escaped `.` whatever the declared divider.
        ("g\\[0\\]\\.sub\\.u", "/", True),
        ("g\\[0\\]\\/sub\\/u", "/", True),
        # An escaped `/` under `DIVIDER .` annotates normally (verified live).
        ("g\\[0\\]\\/sub\\/u", ".", False),
        # An escaped bracket alone resolves normally (#1890's guard test).
        ("g\\[0\\]", ".", False),
        ("u1", ".", False),
    ],
)
def test_component_unresolvable_shape(component, divider, expected):
    assert fvr._sdf_component_is_unresolvable(component, divider) is expected


def test_pins_are_never_instance_components():
    """Only an endpoint's leading components are instances; its final
    component is the pin (or a bare top-level port)."""
    text = _sdf_text()
    assert fvr._sdf_unresolvable_instance_components(text, ".") == {
        "g\\[0\\]\\.sub\\.u",
        "g\\[1\\]\\.sub\\.u",
    }
    assert fvr._sdf_unresolvable_pin_tokens(text) == []


# --------------------------------------------------------------------------- #
# The rename itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("divider", "sep"), [(".", "."), ("/", "."), ("/", "/")])
def test_rename_rewrites_build_dir_copies_only(tmp_path, divider, sep):
    sdf_path = _write(tmp_path / "route.sdf", _sdf_text(divider, sep))
    netlist = _write(tmp_path / "gen_top.v", _netlist_v(sep))
    cells = _write(tmp_path / "cells.v", _CELLS_V)
    out = tmp_path / "out"
    out.mkdir()

    result = fvr._rename_sdf_escaped_divider_instances(
        sdf_path, [netlist, cells], str(out)
    )

    assert result is not None
    new_sdf, new_sources, renamed = result
    assert renamed == _expected_renamed(sep)
    assert new_sdf == str(out / "klt_sdf_renamed.sdf")
    sdf_out = Path(new_sdf).read_text()
    assert f"(INTERCONNECT g_0__sub_u{divider}Z g_1__sub_u{divider}A" in sdf_out
    assert "(INSTANCE g_0__sub_u)" in sdf_out
    assert "(INSTANCE g_1__sub_u)" in sdf_out
    assert "\\" not in sdf_out
    # Only the netlist declaring the instances is copied; cells.v is not.
    assert new_sources[1] == cells
    assert new_sources[0] != netlist
    assert str(out) in new_sources[0]
    netlist_out = Path(new_sources[0]).read_text()
    assert "klt_buf g_0__sub_u (" in netlist_out
    assert "klt_buf g_1__sub_u (" in netlist_out
    # An escaped *net* the SDF never names is left as it was.
    assert f"\\g[0]{sep}sub{sep}n " in netlist_out
    # The caller's own inputs are untouched.
    assert Path(sdf_path).read_text() == _sdf_text(divider, sep)
    assert Path(netlist).read_text() == _netlist_v(sep)


def test_rename_avoids_an_identifier_already_in_use(tmp_path):
    sdf_path = _write(tmp_path / "route.sdf", _sdf_text())
    netlist = _write(
        tmp_path / "gen_top.v",
        _netlist_v().replace("output y;", "output y;\n  wire g_0__sub_u;"),
    )

    result = fvr._rename_sdf_escaped_divider_instances(
        sdf_path, [netlist], str(tmp_path)
    )

    assert result is not None
    assert result[2][0] == {"from": "g[0].sub.u", "to": "g_0__sub_u_klt1"}
    assert "(INSTANCE g_0__sub_u_klt1)" in Path(result[0]).read_text()


def test_rename_is_a_noop_on_an_ordinary_sdf(tmp_path):
    """No escaped-divider instance (an escaped `/` under `DIVIDER .`, an
    escaped bracket alone, plain names): nothing is written."""
    sdf_path = _write(tmp_path / "route.sdf", _sdf_text(".", "/"))
    netlist = _write(tmp_path / "gen_top.v", _netlist_v("/"))

    assert (
        fvr._rename_sdf_escaped_divider_instances(sdf_path, [netlist], str(tmp_path))
        is None
    )
    assert fvr._apply_sdf_escaped_divider_rename(
        sdf_path, [netlist], str(tmp_path)
    ) == (sdf_path, [netlist], [])
    assert not (tmp_path / "klt_sdf_renamed.sdf").exists()


def test_rename_tolerates_an_unreadable_sdf(tmp_path):
    assert (
        fvr._rename_sdf_escaped_divider_instances(
            str(tmp_path / "missing.sdf"), [], str(tmp_path)
        )
        is None
    )


# --------------------------------------------------------------------------- #
# JSON contract (stubbed runner)
# --------------------------------------------------------------------------- #


def _stub_runner(monkeypatch, runner: _FakeRunner) -> None:
    monkeypatch.setattr(
        fv,
        "_import_runner",
        lambda: types.SimpleNamespace(get_runner=lambda name: runner),
    )
    monkeypatch.setattr(fv, "_engine_version", lambda engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.0.1")


def _stage(tmp_path: Path, *, divider: str = ".", sep: str = ".") -> None:
    _write(tmp_path / "cells.v", _CELLS_V)
    _write(tmp_path / "gen_top.v", _netlist_v(sep))
    _write(tmp_path / "test_gen_top.py", _TESTBENCH_PY)
    _write(tmp_path / "route.sdf", _sdf_text(divider, sep))


def _request(tmp_path: Path, name: str, sdf: dict | None) -> str:
    request: dict = {
        "engine": "icarus",
        "sources": ["gen_top.v", "cells.v"],
        "hdl_toplevel": "gen_top",
        "testbench": {"module": "test_gen_top", "testcase": None},
    }
    if sdf is not None:
        request["options"] = {"sdf": sdf}
    path = tmp_path / name
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def test_stubbed_report_names_every_renamed_instance(tmp_path, monkeypatch):
    """`environment.sdf.renamed` lists each rename; `environment.sdf.file`
    still names the caller's own SDF; the request is not rejected."""
    _stage(tmp_path)
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML))

    report = run_functional_verification(
        _request(tmp_path, "request.json", {"file": "route.sdf"})
    )

    sdf = report["environment"]["sdf"]
    assert sdf["file"] == str(tmp_path / "route.sdf")
    assert sdf["renamed"] == _expected_renamed()
    assert sdf["partial"] is False


def test_stubbed_report_omits_renamed_when_nothing_was_renamed(tmp_path, monkeypatch):
    """Additive and absent otherwise: an SDF with no escaped-divider instance
    reports exactly the pre-#2897 `environment.sdf` keys."""
    _stage(tmp_path, sep="/")
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML))

    report = run_functional_verification(
        _request(tmp_path, "request.json", {"file": "route.sdf"})
    )

    assert "renamed" not in report["environment"]["sdf"]


# --------------------------------------------------------------------------- #
# Integration: real cocotb + real Icarus (skipped when either is absent)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
@pytest.mark.parametrize(("divider", "sep"), [(".", "."), ("/", "."), ("/", "/")])
def test_integration_real_icarus_escaped_divider_instances_annotate(
    tmp_path, divider, sep
):
    """The load-bearing regression for issue #2897, end to end: before the
    fix this was rejected up front (#1890's guard) -- or, under
    `DIVIDER /` with an escaped `.`, which that guard missed, aborted `vvp`.
    It must now annotate with no diagnostic, and the annotated 5 ns path
    must flip the 2 ns test from pass to fail while the 6 ns test passes."""
    _stage(tmp_path, divider=divider, sep=sep)
    plain = run_functional_verification(_request(tmp_path, "request-plain.json", None))
    annotated = run_functional_verification(
        _request(tmp_path, "request-sdf.json", {"file": "route.sdf"})
    )

    def statuses(report: dict) -> dict[str, str]:
        return {test["name"]: test["status"] for test in report["tests"]}

    assert statuses(plain) == {
        "test_y_follows_a_within_two_ns": "passed",
        "test_y_follows_a_within_six_ns": "passed",
    }
    assert statuses(annotated) == {
        "test_y_follows_a_within_two_ns": "failed",
        "test_y_follows_a_within_six_ns": "passed",
    }
    sdf = annotated["environment"]["sdf"]
    assert sdf["annotated"] is True
    assert sdf["partial"] is False
    assert sdf["renamed"] == _expected_renamed(sep)
