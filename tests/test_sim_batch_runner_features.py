"""The #5645 five: post-run ``*_unsupported_worker`` echo checks become
pre-launch refusals through the runner-feature registry
(``src/klayout_tools/sim_batch.py``).

Split out of ``tests/test_sim_batch.py`` when that file hit its file-size
ratchet ceiling (#5645); every helper is imported from there, so the same
no-``aws``/no-S3 fake-transport discipline holds. The registry-coverage
ratchet itself lives in ``tests/test_sim_runner_feature_registry_coverage.py``.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    _PRELAUNCH_CLIENT,
    FAKE_BUCKET,
    _config,
    _image,
    _pin_client_version,
    _provision_script,
    _publish_manifest,
    _write_body,
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


# --- measurements[].crossings.value_from (issue #6913) ---------------------

_CROSSINGS_VALUE_FROM_REQUEST = {
    "models": {"pdk": "sky130A"},
    "options": {},
    "measurements": [
        {
            "name": "f_ro1",
            "crossings": {
                "signal": "v(ro1)",
                "level": 0.9,
                "value_from": "frequency",
            },
        }
    ],
}


def test_crossings_value_from_is_a_registered_mandatory_runner_feature():
    """#6913: `crossings.value_from` is detected pre-launch and mandatory --
    an older runner would reject the unknown key (at best) or grade the raw
    crossing count, so a fleet runner older than v0.9.3 never sees the job.
    First release segment 0.9.3: the field is not in the v0.9.2 tag."""
    assert sb._request_features(_CROSSINGS_VALUE_FROM_REQUEST) == [
        "crossings_value_from"
    ]
    quiet = {
        "models": {"pdk": "sky130A"},
        "options": {},
        "measurements": [
            {"name": "t", "crossings": {"signal": "v(x)", "level": 0.5}},
            {
                "name": "u",
                "crossings": {"signal": "v(x)", "level": 0.5, "value_from": None},
            },
        ],
    }
    assert sb._request_features(quiet) == []
    assert "crossings_value_from" in sb._MANDATORY_RUNNER_FEATURES
    assert sb._missing_runner_features(["crossings_value_from"], "0.9.2") == [
        "crossings_value_from"
    ]
    assert sb._missing_runner_features(["crossings_value_from"], "0.9.3") == []
    assert sb._missing_runner_features(["crossings_value_from"], None) is None


def test_prelaunch_refuses_crossings_value_from_on_an_old_image(tmp_path, monkeypatch):
    """#6913: a v0.9.2 runner image predates `crossings_value_from` (first
    release segment 0.9.3), and the feature is mandatory -- so the submit
    path refuses before any S3 write or launch, `warn` included, with the
    nothing-was-submitted text."""
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.9.2")])
    for check in ("enforce", "warn"):
        assessment = sb._assess_published_runner(
            _CROSSINGS_VALUE_FROM_REQUEST, _config(tmp_path, runner_version_check=check)
        )
        with pytest.raises(sim.SimError) as raised:
            sb._enforce_published_runner_features(
                _CROSSINGS_VALUE_FROM_REQUEST, assessment
            )
        assert raised.value.code == sb.BATCH_RUNNER_FEATURE_CODE
        message = str(raised.value)
        assert "crossings_value_from" in message
        assert "the request was not submitted and no instance was launched" in message


@pytest.mark.parametrize("backend", ["batch", "remote"])
@pytest.mark.parametrize("accepts_selector", [False, True])
def test_crossings_preflight_checks_the_worker_parser(
    tmp_path, backend, accepts_selector
):
    from klayout_tools.sim_batch_contract_preflight import (
        _runner_preflight_script,
        remote_feature_preflight,
    )

    worker = tmp_path / "klt"
    worker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "klt 0.9.3.dev1"; exit 0; fi\n'
        'test "$1" = sim && grep -Fq "value_from" "$2" || exit 1\n'
        'test -f "$(dirname "$2")/deck.spice" || exit 1\n'
        'test "$3 $4 $5" = "--backend local --dry-run" || exit 1\n'
        'touch "$EDA_OUTPUT_DIR/probed"\n'
        + ("exit 0\n" if accepts_selector else "exit 1\n")
    )
    worker.chmod(0o755)
    environment = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        EDA_OUTPUT_DIR=str(tmp_path),
        TMPDIR=str(tmp_path),
    )
    request = _CROSSINGS_VALUE_FROM_REQUEST

    def preflight(req):
        if backend == "remote":
            return remote_feature_preflight(req)
        return _runner_preflight_script(
            "0.9.3", enforce=True, required_features=tuple(sb._request_features(req))
        )

    plain = subprocess.run(["bash", "-c", preflight({}) + " true"], env=environment)
    assert plain.returncode == 0 and not (tmp_path / "probed").exists()
    result = subprocess.run(
        ["bash", "-c", preflight(request) + ' touch "$EDA_OUTPUT_DIR/launched"'],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert (tmp_path / "probed").is_file()
    assert (tmp_path / "launched").exists() == accepts_selector
    assert result.returncode == (0 if accepts_selector else 87)
    if not accepts_selector:
        assert "crossings_value_from" in result.stderr
    assert not list(tmp_path.glob("*/probe.json"))
