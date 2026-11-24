"""PDK model aliases and caller sources in compilation order."""

import hashlib
from pathlib import Path

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools.functional_verification_run_inputs import resolve_run_inputs
from test_functional_verification_pdk import work as work


@pytest.mark.parametrize("caller_root", ["alias", "install"])
def test_model_alias_keeps_resolver_path_and_distinct_caller_copy(work, caller_root):
    tmp, _ = work
    alias = tmp / "alias"
    alias.symlink_to(tmp / "install", target_is_directory=True)
    view = Path("sky130A/libs.ref/sky130_fd_sc_hd/verilog")
    original = alias / view / "primitives.v"
    copy = tmp / "copy.v"
    copy.write_bytes(original.read_bytes())
    request = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "pdk": {
            "variant": "sky130A",
            "root": str(alias),
            "cell_library": "sky130_fd_sc_hd",
        },
        "sources": ["dut.v", str(Path(caller_root) / view / "primitives.v"), "copy.v"],
    }

    sources, inputs = resolve_run_inputs(request, str(tmp), "gcd", fv._path_envelope)
    expected = [original, alias / view / "sky130_fd_sc_hd.v", tmp / "dut.v", copy]
    assert sources == [str(path) for path in expected]
    assert inputs.sources == [
        {
            **fv._path_envelope(str(path)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in expected
    ]
