"""The #5645 five: post-run ``*_unsupported_worker`` echo checks become
pre-launch refusals through the runner-feature registry
(``src/klayout_tools/sim_batch.py``).

Split out of ``tests/test_sim_batch.py`` when that file hit its file-size
ratchet ceiling (#5645); every helper is imported from there, so the same
no-``aws``/no-S3 fake-transport discipline holds. The registry-coverage
ratchet itself lives in ``tests/test_sim_runner_feature_registry_coverage.py``.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    _PRELAUNCH_CLIENT,
    FAKE_BUCKET,
    _batch_request,
    _config,
    _FakeRunner,
    _image,
    _install_fake_batch_transport,
    _pin_client_version,
    _provision_script,
    _publish_manifest,
    _write_body,
    _write_request,
)

# --- the #5645 five: post-run echo fields become pre-launch refusals -------

_ECHO_FIELD_CASES = [
    # (feature, minimum runner release, request declaring it, requests that
    # do not: absent and empty values are not a use)
    (
        "analysis.op_check",
        "0.9.2",
        {"analysis": {"kind": "op", "args": "", "op_check": {"nodes": ["q"]}}},
        [
            {"analysis": {"kind": "op", "args": ""}},
            {"analysis": {"kind": "op", "args": "", "op_check": None}},
        ],
    ),
    (
        "analysis_overrides",
        "0.9.1",
        {"analysis_overrides": [{"match": {"corner": "tt"}, "args": "2n 2u"}]},
        [{}, {"analysis_overrides": []}],
    ),
    (
        "options.nodeset",
        "0.8.0",
        {"options": {"nodeset": [{"match": {"corner": "tt"}, "values": {"x": 0}}]}},
        [{"options": {}}, {"options": {"nodeset": []}}],
    ),
    (
        "options.initial_conditions",
        "0.8.0",
        {
            "options": {
                "initial_conditions": [{"match": {"corner": "tt"}, "values": {"x": 0}}]
            }
        },
        [{"options": {}}, {"options": {"initial_conditions": []}}],
    ),
    (
        "corners.options",
        "0.8.0",
        {"corners": {"options": {"reltol": [1e-4, 1e-3]}}},
        [{"corners": {}}, {"corners": {"options": {}}}],
    ),
]


@pytest.mark.parametrize(("feature", "minimum", "declared", "quiet"), _ECHO_FIELD_CASES)
def test_request_features_detect_the_5645_echo_fields(
    feature, minimum, declared, quiet
):
    """#5645: each post-run *_unsupported_worker field now declares a
    registry feature (mandatory, mirroring its echo check's severity); an
    absent or empty value is not a use."""
    assert sb._request_features(declared) == [feature]
    for request in quiet:
        assert sb._request_features(request) == []
    assert sb.REQUEST_FEATURE_MIN_RUNNER_VERSION[feature] == minimum
    assert feature in sb._MANDATORY_RUNNER_FEATURES


@pytest.mark.parametrize(
    ("feature", "_minimum", "declared", "_quiet"), _ECHO_FIELD_CASES
)
def test_prelaunch_refuses_each_echo_field_on_an_old_image_even_under_warn(
    tmp_path, monkeypatch, feature, _minimum, declared, _quiet
):
    """#5645: the 0.5.0 fleet image predates every entry, so the submit path
    refuses before any S3 write or launch -- mandatory, so `warn` does not
    relax it -- with the nothing-was-submitted text."""
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.5.0")])
    for check in ("enforce", "warn"):
        assessment = sb._assess_published_runner(
            declared, _config(tmp_path, runner_version_check=check)
        )
        with pytest.raises(sim.SimError) as raised:
            sb._enforce_published_runner_features(declared, assessment)
        assert raised.value.code == sb.BATCH_RUNNER_FEATURE_CODE
        message = str(raised.value)
        assert feature in message
        assert "the request was not submitted and no instance was launched" in message


def test_a_request_without_the_5645_fields_declares_no_features(tmp_path):
    """A request declaring none of the five is unchanged: no registry
    feature, so the job command gains no mandatory-feature gate."""
    plain = {
        "models": {"pdk": "sky130A"},
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "options": {},
        "corners": {},
    }
    assert sb._request_features(plain) == []
    command = sb._build_batch_job_spec(
        plain,
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        client_version="0.9.2.dev1+gabc1234",
    ).cmd
    assert "mmiss" not in command


def _op_check_dry_run_request(tmp_path) -> dict:
    return {
        "models": {"pdk": "sky130A"},
        "analysis": {"kind": "op", "args": "", "op_check": {"nodes": ["q"]}},
        "batch": {
            "bucket": FAKE_BUCKET,
            "provision_script_path": str(_provision_script(tmp_path)),
        },
    }


def test_dry_run_check_refuses_op_check_on_an_all_predating_manifest(
    tmp_path, monkeypatch
):
    """#5645: `--dry-run` is backend-aware through the same registry, so an
    op_check request against a manifest whose every image predates 0.9.2 now
    propagates the refusal instead of reporting the request valid."""
    _pin_client_version(monkeypatch, "0.9.2.dev1+gabc1234")
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.9.0")])
    with pytest.raises(sim.SimError) as raised:
        sb.dry_run_runner_check(
            _op_check_dry_run_request(tmp_path), corner_count=1, timeout_s=30.0
        )
    assert raised.value.code == sb.BATCH_RUNNER_FEATURE_CODE
    assert "analysis.op_check" in str(raised.value)


_UNVERIFIED = "batch_runner_request_feature_unverified"


def _overrides_request(tmp_path, policy):
    _write_body(tmp_path)
    req = _batch_request(
        tmp_path, analysis_overrides=[{"match": {"temperature_c": 27}, "args": "2n 2u"}]
    )
    req["batch"]["runner_version_check"] = policy
    return req


@pytest.mark.parametrize("policy", ["enforce", "warn"])
def test_unknown_manifest_warns_on_batch_dry_run(tmp_path, monkeypatch, policy):
    _pin_client_version(monkeypatch, "0.9.2")
    _publish_manifest(tmp_path, monkeypatch, [])
    path = _write_request(tmp_path, _overrides_request(tmp_path, policy))
    report = sim.run_sim(str(path), dry_run=True)
    (warning,) = [d for d in report["diagnostics"] if d["code"] == _UNVERIFIED]
    assert warning["severity"] == "warning"
    assert warning["features"] == ["analysis_overrides"]
    assert "before launch" in warning["message"]
    published = report["published_runner"]
    assert published["status"] == "unknown" and published["images"] == []
    assert report["status"] == "valid"


@pytest.mark.parametrize("policy", ["enforce", "warn"])
def test_unknown_manifest_warns_before_submission(
    tmp_path, monkeypatch, capsys, policy
):
    _pin_client_version(monkeypatch, "0.9.2")
    _publish_manifest(tmp_path, monkeypatch, [])
    runner = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=runner)
    first_upload = []

    def record(argv, timeout_s):
        if not first_upload and "cp" in argv and "--recursive" in argv:
            first_upload.append(capsys.readouterr().err)
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", record)
    path = _write_request(tmp_path, _overrides_request(tmp_path, policy))
    submitted = sim.run_sim(
        str(path), submit_only=True, artifacts_dir=str(tmp_path / "out")
    )
    assert submitted["status"] == "submitted"
    (stderr,) = first_upload
    assert "analysis_overrides" in stderr and "before launch" in stderr


@pytest.mark.parametrize(("version", "overrides"), [(None, False), ("0.9.2", True)])
def test_no_unverified_warning_for_plain_or_supported_request(
    tmp_path, monkeypatch, version, overrides
):
    _pin_client_version(monkeypatch, "0.9.2")
    _publish_manifest(
        tmp_path, monkeypatch, [_image(klt_version=version)] if version else []
    )
    req = _overrides_request(tmp_path, "enforce")
    if not overrides:
        req["analysis_overrides"] = []
    assert sb.dry_run_runner_check(req, corner_count=1, timeout_s=30) is None


def test_known_old_image_refuses_mandatory_overrides_even_with_warn(
    tmp_path, monkeypatch
):
    _pin_client_version(monkeypatch, "0.9.2")
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.9.0")])
    with pytest.raises(sim.SimError, match="analysis_overrides") as error:
        sb.dry_run_runner_check(
            _overrides_request(tmp_path, "warn"), corner_count=1, timeout_s=30
        )
    assert error.value.code == sb.BATCH_RUNNER_FEATURE_CODE
