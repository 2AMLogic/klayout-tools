"""Worker and client must agree about step-scoped on_unreached (issue #7770)."""

import json
import os
import subprocess

import pytest

from klayout_tools import sim, sim_batch
from klayout_tools.sim_batch_contract_preflight import _runner_preflight_script


def request_with_policy(policy):
    measurement = {"name": "miss", "spice": ".meas ac miss find v(out) when v(out)=2"}
    if policy is not None:
        measurement["on_unreached"] = policy
    return {"analysis_steps": [{"name": "sweep", "measurements": [measurement]}]}


def test_only_step_scoped_null_uses_the_capability():
    assert sim_batch._request_features(request_with_policy("null")) == [
        "analysis_steps",
        "step_on_unreached",
    ]
    for policy in (None, "error"):
        assert sim_batch._request_features(request_with_policy(policy)) == [
            "analysis_steps"
        ]
    step = request_with_policy("null")["analysis_steps"][0]
    assert sim_batch._request_features({"measurements": step["measurements"]}) == []


@pytest.mark.parametrize(
    ("version", "status"),
    [
        ("0.9.0", "unsupported"),
        ("0.9.3.dev3404+g3d6e05b43", "unsupported"),
        ("0.9.3.dev3411+g123", "unsupported"),
        ("0.9.3.dev3412+g80d17af74", "supported"),
        ("0.9.3", "supported"),
        ("0.10.0.dev1+g123", "supported"),
        (None, "unknown"),
    ],
)
def test_collector_and_worker_agree_on_step_unreached(tmp_path, version, status):
    request = request_with_policy("null")
    features = tuple(sim_batch._request_features(request))
    prefix = _runner_preflight_script(
        "0.9.3.dev4954", required_features=features, enforce=False
    )
    # A warn-only major/minor check must still refuse a mandatory capability.
    cmd = (
        'klt() { printf "klt %s\\n" "$RUNNER_VERSION"; }; '
        + prefix
        + "echo start-solver"
    )
    result = subprocess.run(
        ["bash", "-c", cmd],
        env={
            **os.environ,
            "RUNNER_VERSION": version or "",
            "EDA_OUTPUT_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
    )
    fields = sim_batch._read_runner_identity(str(tmp_path), request)[
        "runner_request_features"
    ]
    assert fields["status"] == status
    assert fields["required"] == ["analysis_steps", "step_on_unreached"]
    if status == "supported":
        assert fields["missing"] == []
        assert result.returncode == 0
        assert "start-solver" in result.stdout
    else:
        assert fields["missing"] == (None if version is None else ["step_on_unreached"])
        assert result.returncode == sim_batch._BATCH_PREFLIGHT_REJECT_EXIT
        assert "start-solver" not in result.stdout
        error = json.loads((tmp_path / sim_batch.BATCH_REPORT_FILENAME).read_text())[
            "error"
        ]
        assert error["code"] == sim_batch.BATCH_RUNNER_FEATURE_CODE
        assert "step_on_unreached" in error["message"]


def test_known_old_image_is_refused_before_submission():
    assessment = {
        "images": [{"klt_version": "0.9.0"}],
        "client_klt_version": "0.9.3.dev4954",
    }
    with pytest.raises(sim.SimError, match="step_on_unreached") as error:
        sim_batch._enforce_published_runner_features(
            request_with_policy("null"), assessment
        )
    assert error.value.code == sim_batch.BATCH_RUNNER_FEATURE_CODE
