"""``op`` analyses default ``args`` to ``""`` (issue #5078)."""

import pytest

from klayout_tools import sim, sim_steps


@pytest.mark.parametrize("kind", ["op", "OP"])
def test_scalar_op_analysis_defaults_args_on_a_copy(kind):
    request = {
        "analysis": {"kind": kind},
        "measurements": [{"name": "v", "expr": "v(out)"}],
    }
    analysis, _ = sim._resolve_analysis_and_measurements(request)
    assert analysis["args"] == ""
    assert "args" not in request["analysis"]


def test_step_op_analysis_defaults_args():
    assert sim_steps._resolve_step_analysis({"kind": "op"}, "s")[1] == ""
    with pytest.raises(sim.SimError, match="requires 'kind' and 'args'"):
        sim_steps._resolve_step_analysis({"kind": "dc"}, "s")
