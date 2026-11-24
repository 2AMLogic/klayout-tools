"""Worker and client must agree about step-scoped on_unreached (issue #7770)."""

import json
import os
import subprocess
import sys
from pathlib import Path

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
    ("version", "replayed_status", "status"),
    [
        ("0.9.0", None, "unsupported"),
        ("0.9.3.dev3404+g3d6e05b43", "error", "unsupported"),
        ("0.9.3.dev3329+gcecd89e6d", "not_reached", "supported"),
        ("0.9.3.dev5005+g123", "error", "unsupported"),
        ("0.9.3.dev3412+g80d17af74", None, "unknown"),
        ("0.9.3", None, "supported"),
        ("0.10.0.dev1+g123", None, "supported"),
        (None, None, "unknown"),
    ],
)
def test_collector_and_worker_agree_on_step_unreached(
    tmp_path, version, replayed_status, status
):
    request = request_with_policy("null")
    features = tuple(sim_batch._request_features(request))
    prefix = _runner_preflight_script(
        "0.9.3.dev4954", required_features=features, enforce=False
    )
    # Model each worker's real CLI grading response to the same replay. The
    # development build with the smaller distance is the capable branch.
    measurements = [
        {"name": "edge.hit", "status": "pass", "value": 1},
        {"name": "edge.miss", "status": replayed_status, "value": None},
    ]
    response = json.dumps({"corners": [{"measurements": measurements}]})
    cmd = (
        'klt() { if [ "$1" = --version ]; then echo "klt $RUNNER_VERSION"; '
        'else printf "%s" "$PROBE_RESULT"; fi; }; ' + prefix + "echo start-solver"
    )
    result = subprocess.run(
        ["bash", "-c", cmd],
        env={
            **os.environ,
            "RUNNER_VERSION": version or "",
            "PROBE_RESULT": response if replayed_status else "invalid output",
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
        assert fields["missing"] == (
            None if status == "unknown" else ["step_on_unreached"]
        )
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


def test_untested_development_manifest_defers_to_worker(tmp_path):
    request = request_with_policy("null")
    version = "0.9.3.dev3329+gcecd89e6d"
    assessment = {"images": [{"klt_version": version}], "client_klt_version": version}
    assert sim_batch._enforce_published_runner_features(request, assessment) is None
    identity = tmp_path / sim_batch.BATCH_RUNNER_IDENTITY_FILENAME
    identity.write_text(json.dumps({"runner_klt_version": version}))
    fields = sim_batch._read_runner_identity(str(tmp_path), request)
    assert fields["runner_request_features"] == {
        "required": ["analysis_steps", "step_on_unreached"],
        "missing": None,
        "status": "unknown",
    }


def test_installed_grader_accepts_replayed_step_without_a_solver(tmp_path):
    # Run the real CLI in this checkout, using an ambiguous version for the
    # gate. The only selected engine executable is the generated log replay.
    features = tuple(sim_batch._request_features(request_with_policy("null")))
    prefix = _runner_preflight_script(
        "0.9.3.dev1", required_features=features, enforce=False
    )
    cmd = (
        'klt() { if [ "$1" = --version ]; then echo "klt 0.9.3.dev1"; '
        'else "$PYTHON" -c "$ENTRY" "$@"; fi; }; ' + prefix + "echo admitted"
    )
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        ["bash", "-c", cmd],
        env={
            **os.environ,
            "PYTHON": sys.executable,
            "ENTRY": "from klayout_tools.cli import main; raise SystemExit(main())",
            "PYTHONPATH": str(source),
            "EDA_OUTPUT_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "admitted" in result.stdout
    identity = tmp_path / sim_batch.BATCH_RUNNER_IDENTITY_FILENAME
    assert json.loads(identity.read_text())["step_on_unreached"] == "supported"
