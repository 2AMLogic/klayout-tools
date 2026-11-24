"""A malformed optional corner-request object yields a field-specific sim error."""

import json

import pytest

from klayout_tools import sim
from klayout_tools.cli import main
from klayout_tools.sim_batch_plan import preflight_batch_request


def _request(tmp_path, **fields):
    (tmp_path / "body.sp").write_text("V1 out 0 1\nR1 out 0 1k\n")
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.sp",
                "analysis": {"kind": "tran", "args": "1n 1u"},
                **fields,
            }
        )
    )
    return str(path)


@pytest.mark.parametrize("field", ["options", "monte_carlo"])
@pytest.mark.parametrize("value", ["typo", [1], [], True, False, 0, ""])
def test_non_object_corner_field_fails_before_dispatch(tmp_path, capsys, field, value):
    request = _request(tmp_path, **{field: value})
    message = f"request.{field} must be an object"
    with pytest.raises(sim.SimError) as error:
        sim.run_sim(request, backend="local", dry_run=True)
    assert message in str(error.value)
    with pytest.raises(sim.SimError) as error:
        preflight_batch_request(request, timeout_s=5)
    assert message in str(error.value)
    assert main(["sim", request, "--backend", "local", "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    envelope = json.loads(captured.err)
    assert envelope["error"]["command"] == "sim"
    assert message in envelope["error"]["message"]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"options": None, "monte_carlo": None},
        {"options": {}},
        {"monte_carlo": {"n": 2, "seed": 7, "vary": "both"}},
    ],
)
def test_valid_object_or_omission_accepts_dry_run(tmp_path, fields):
    report = sim.run_sim(_request(tmp_path, **fields), backend="local", dry_run=True)
    assert report["dry_run"] is True


def test_empty_mc_object_keeps_sample_count_error(tmp_path):
    with pytest.raises(
        sim.SimError, match=r"request\.monte_carlo\.n must be a positive integer"
    ):
        sim.run_sim(_request(tmp_path, monte_carlo={}), backend="local", dry_run=True)
