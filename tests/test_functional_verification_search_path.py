"""List-form ``testbench.search_path`` and ``env.PYTHONPATH`` tests for
`klt functional-verification` (issue #5797).

Split out of ``tests/test_functional_verification.py`` for headroom under
the per-file size ratchet -- moved, not rewritten. The three pre-existing
single-directory ``search_path`` tests stay in the parent module (their
assertions widened to the list return value there); everything here is
new with the list form itself.
"""

from __future__ import annotations

import sys

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)
from test_functional_verification import (
    _TESTBENCH,
    HAVE_COCOTB,
    HAVE_SIMULATOR,
    _write,
    _write_request,
)


def test_testbench_search_path_list_first_hit_wins_and_returns_all(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write(b / "test_gcd.py", _TESTBENCH)
    module, module_dirs, _tc = fv._resolve_testbench(
        {"module": "test_gcd", "search_path": ["a", str(b), "a", "./a"]},
        str(tmp_path),
    )
    assert module == "test_gcd"
    # duplicates collapse to first occurrence; order preserved
    assert module_dirs == [str(a.resolve()), str(b.resolve())]


def test_testbench_search_path_single_entry_list_equals_string(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    _write(shared / "test_gcd.py", _TESTBENCH)
    as_str = fv._resolve_testbench(
        {"module": "test_gcd", "search_path": "shared"}, str(tmp_path)
    )
    as_list = fv._resolve_testbench(
        {"module": "test_gcd", "search_path": ["shared"]}, str(tmp_path)
    )
    assert as_str == as_list


def test_testbench_search_path_list_missing_module_names_every_dir(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(FunctionalVerificationError) as exc:
        fv._resolve_testbench(
            {"module": "test_gcd", "search_path": ["a", "b"]}, str(tmp_path)
        )
    msg = str(exc.value)
    assert msg.index(str(tmp_path / "a")) < msg.index(str(tmp_path / "b"))
    assert "request.testbench.search_path" in msg


@pytest.mark.parametrize("bad", [[], [""], ["ok", 7], 7, "", {"a": 1}])
def test_testbench_search_path_shape_rejected(tmp_path, bad):
    with pytest.raises(
        FunctionalVerificationError,
        match="search_path must be a non-empty string or a non-empty array",
    ):
        fv._resolve_testbench({"module": "test_gcd", "search_path": bad}, str(tmp_path))


def test_env_pythonpath_rejected_naming_search_path():
    with pytest.raises(FunctionalVerificationError, match="testbench.search_path"):
        fv._resolve_env({"PYTHONPATH": "/x"})


def test_run_test_inserts_all_dirs_in_order_and_restores_sys_path(tmp_path):
    seen = {}

    class _Runner:
        def test(self, **_kw):
            seen["path"] = list(sys.path[:3])

    present = str(tmp_path / "present")
    sys.path.append(present)
    before = list(sys.path)
    try:
        fv._run_test(
            _Runner(),
            engine="icarus",
            module="m",
            module_dirs=["/d0", present, "/d1"],
            hdl_toplevel="t",
            testcase=None,
            random_seed=None,
            build_dir="b",
            test_dir="t",
            results_xml="r",
            timescale=("1ns", "1ps"),
            parameters={},
            log_path=str(tmp_path / "l"),
        )
        assert seen["path"][:2] == ["/d0", "/d1"]
        assert sys.path == before
    finally:
        sys.path.remove(present)


_SPLIT_BENCH_RTL = """module counter(input clk, input rst, output reg [3:0] q);
  always @(posedge clk) if (rst) q <= 0; else q <= q + 1;
endmodule
"""
_SPLIT_BENCH_HELPER = "def expected_after(n):\n    return n % 16\n"
_SPLIT_BENCH_TB = """import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from bench_helpers import expected_after


@cocotb.test()
async def test_counts(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    assert int(dut.q.value) == expected_after(3)
"""


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_SIMULATOR["icarus"], reason="iverilog is not installed on this machine"
)
def test_integration_search_path_list_two_directory_bench(tmp_path, monkeypatch):
    """Issue #5797: a bench split across two directories runs from the request
    alone (no ambient PYTHONPATH); listing only the first entry fails."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    (tmp_path / "bench").mkdir()
    (tmp_path / "lib").mkdir()
    _write(tmp_path / "counter.v", _SPLIT_BENCH_RTL)
    _write(tmp_path / "bench" / "test_counter.py", _SPLIT_BENCH_TB)
    _write(tmp_path / "lib" / "bench_helpers.py", _SPLIT_BENCH_HELPER)

    def _request(name, search_path):
        return _write_request(
            tmp_path / name,
            {
                "sources": ["counter.v"],
                "hdl_toplevel": "counter",
                "engine": "icarus",
                "testbench": {"module": "test_counter", "search_path": search_path},
            },
        )

    report = run_functional_verification(_request("ok.json", ["bench", "lib"]))
    assert report["status"] == "pass"
    assert report["test_count"] == 1

    with pytest.raises(FunctionalVerificationError):
        run_functional_verification(_request("bad.json", ["bench"]))
