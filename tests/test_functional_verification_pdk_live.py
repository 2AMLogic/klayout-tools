"""The issue #5782 end-to-end case, live: a gate-level netlist re-simulated
through a ``request.pdk`` block alone against a **real** sky130A install --
no PDK path and no ``FUNCTIONAL`` define in the request, reaching the same
verdict as the hand-written-paths form.

Split from ``tests/test_functional_verification_pdk.py`` for the 500-line
per-file size ratchet (that file carries the offline request-surface
coverage). This test requires a real install, ``iverilog``, and cocotb.
It skips in CI, where the sky130 fixture supplies only Liberty data.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from helpers.tool_probes import SKIP_NO_COCOTB, SKIP_NO_ICARUS
from klayout_tools import functional_verification as fv


def _request(tmp_path, doc):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(doc))
    return str(path)


# --------------------------------------------------------------------------- #
# end to end, gated on a real sky130A install + iverilog + cocotb
# --------------------------------------------------------------------------- #


def _real_sky130_install():
    """The resolved install a careful caller would pin, or ``None``."""
    from klayout_tools.pdk import PdkNotFoundError, find_pdk

    try:
        info = find_pdk(variant="sky130A")
    except PdkNotFoundError:
        return None
    view = os.path.join(info["assets"]["libs_ref"] or "", "sky130_fd_sc_hd", "verilog")
    if not os.path.isfile(os.path.join(view, "sky130_fd_sc_hd.v")):
        return None
    return info


# Resolved at import time, before this module's autouse search-space isolation
# fixture hides the host's installs from the per-test environment -- the
# end-to-end case below runs against the *real* install this captured.
_REAL_SKY130 = _real_sky130_install()
_REAL_SKY130_VIEW = (
    None
    if _REAL_SKY130 is None
    else os.path.join(_REAL_SKY130["assets"]["libs_ref"], "sky130_fd_sc_hd", "verilog")
)


GATE_NETLIST = """module gcd_top(input a, input b, output y);
  sky130_fd_sc_hd__and2_1 _u0 (.A(a), .B(b), .X(y));
endmodule
"""

GATE_TESTBENCH = """import cocotb
from cocotb.clock import Timer


@cocotb.test()
async def and2_matches_promote(dut):
    for a, b in ((0, 0), (0, 1), (1, 0), (1, 1)):
        dut.a.value = a
        dut.b.value = b
        await Timer(10, unit="ns")
        assert dut.y.value == (a & b), f"a={a} b={b} y={dut.y.value}"
"""


@SKIP_NO_COCOTB
@SKIP_NO_ICARUS
@pytest.mark.skipif(
    _REAL_SKY130 is None,
    reason="no sky130A install with a sky130_fd_sc_hd verilog/ view",
)
def test_end_to_end_gate_level_resim_through_the_pdk_block_alone(tmp_path):
    """AC2, live: a gate-level netlist re-simulated through a ``pdk`` block
    alone -- no PDK path and no ``FUNCTIONAL`` define in the request --
    reaches the same verdict as the hand-written-paths form."""
    info, view = _REAL_SKY130, _REAL_SKY130_VIEW
    (tmp_path / "gate.v").write_text(GATE_NETLIST)
    (tmp_path / "test_gate.py").write_text(GATE_TESTBENCH)
    shared = {
        "hdl_toplevel": "gcd_top",
        "testbench": {"module": "test_gate"},
        "sources": ["gate.v"],
    }

    via_pdk = fv.run_functional_verification(
        _request(
            tmp_path,
            {
                **shared,
                "pdk": {
                    "variant": "sky130A",
                    "root": info["root"],
                    "cell_library": "sky130_fd_sc_hd",
                },
            },
        )
    )
    # The hand-written-paths form: the same model files spelled out by
    # absolute path, plus the FUNCTIONAL define the pdk form derives.
    primitives = os.path.join(view, "primitives.v")
    by_hand = fv.run_functional_verification(
        _request(
            tmp_path,
            {
                **shared,
                "sources": ([primitives] if os.path.isfile(primitives) else [])
                + [os.path.join(view, "sky130_fd_sc_hd.v"), "gate.v"],
                "options": {"defines": {"FUNCTIONAL": "1"}},
            },
        ),
        output_dir=str(tmp_path / ".klt" / "byhand"),
    )

    assert via_pdk["status"] == "pass"
    assert [t["status"] for t in via_pdk["tests"]] == ["passed"]
    assert by_hand["status"] == "pass"
    assert [(t["name"], t["status"]) for t in via_pdk["tests"]] == [
        (t["name"], t["status"]) for t in by_hand["tests"]
    ]
    # The pdk-block run's own echo names the resolved model files, in
    # dependency order, before the caller's netlist. The models live outside
    # any repo, so their envelope carries no host path (#2224) -- identity
    # is the digest.
    echoed = via_pdk["environment"]["sources"]
    primitives_rel = os.path.join(view, "primitives.v")
    expected_files = ([primitives_rel] if os.path.isfile(primitives_rel) else []) + [
        os.path.join(view, "sky130_fd_sc_hd.v"),
        str(tmp_path / "gate.v"),
    ]
    assert [entry["sha256"] for entry in echoed] == [
        hashlib.sha256(open(f, "rb").read()).hexdigest() for f in expected_files
    ]
    assert all(entry["scope"] == "external" for entry in echoed)
