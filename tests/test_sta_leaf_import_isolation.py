"""STA leaf imports and validation errors must leave the STA runner unloaded."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "import-boundaries.json").read_text(encoding="utf-8"))
LEAVES = {
    name.removeprefix("klayout_tools.") for name in POLICY["groups"]["sta"]["modules"]
} - {"post_route_sta", "post_route_sta_verify", "cli.sta_cmd"}

# None marks leaves with no validation call in this guard.
CASES = {
    "_sta_clock_transition": (
        "validate_clock_transition",
        [{"clock_transition_ns": -1}],
    ),
    "_sta_clock_uncertainty": (
        "validate_clock_uncertainty",
        [{"clock_uncertainty_ns": -1}],
    ),
    "_sta_period_maps": ("validate_period", [0, "clock"]),
    "_sta_request_diag": None,
    "_sta_sdc_check": ("resolve_sdc_check", [False, "."]),
    "_sta_timing_derate": ("validate_timing_derate", [{"timing_derate": False}]),
    "_sta_wire_load": None,
    "post_route_sta_electrical": None,
    "post_route_sta_errors": None,
    "post_route_sta_instance_paths": (
        "_validate_report_instances",
        [{"report_instances": False}],
    ),
    "post_route_sta_path_groups": None,
    "post_route_sta_stages": None,
    "post_route_sta_violating_endpoints": (
        "_validate_max_violating_endpoints",
        [{"max_violating_endpoints": 0}],
    ),
}


def test_sta_leaf_inventory():
    source_leaves = {
        path.stem for path in (ROOT / "src/klayout_tools").glob("_sta_*.py")
    }
    assert not (source_leaves - LEAVES), (
        f"Missing from STA group: {sorted(source_leaves - LEAVES)}"
    )
    assert LEAVES == CASES.keys(), (
        f"Missing cases: {sorted(LEAVES - CASES.keys())}; "
        f"obsolete cases: {sorted(CASES.keys() - LEAVES)}"
    )


PROBE = """
import importlib
import json
import sys

leaf, case = sys.argv[1], json.loads(sys.argv[2])
module = importlib.import_module("klayout_tools." + leaf)
runner = "klayout_tools.post_route_sta"
assert runner not in sys.modules, f"{leaf} imported {runner}"
if case is not None:
    from klayout_tools.post_route_sta_errors import PostRouteStaError

    function, args = case
    try:
        getattr(module, function)(*args)
    except PostRouteStaError:
        pass
    else:
        raise AssertionError(f"{leaf}.{function} accepted invalid input")
    assert runner not in sys.modules, f"{leaf}.{function} imported {runner}"
"""


@pytest.mark.parametrize("leaf", sorted(LEAVES))
def test_sta_leaf_import_isolation(leaf):
    result = subprocess.run(
        [sys.executable, "-c", PROBE, leaf, json.dumps(CASES[leaf])],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{leaf}\n{result.stdout}{result.stderr}"
