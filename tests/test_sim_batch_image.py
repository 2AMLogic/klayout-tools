"""Exercise the published sim image through the same S3 command used by preflight."""

import json
import subprocess
from pathlib import Path

import pytest

from klayout_tools import sim_batch as sb
from klayout_tools.sim_batch_image import read_batch_runner_images
from klayout_tools.sim_errors import SimError
from test_sim_batch import _pin_client_version, _provision_script


@pytest.fixture
def pointer():
    return {
        "schema": "eda-batch-image/1",
        "ami_id": "ami-0123456789abcdef0",
        "region": "us-east-1",
        "source_commit": "2am-bake-commit",
        "tools": {"klt_build": "0.9.2", "klt": "0.5.0", "ngspice": "ngspice-47"},
        "pdk": {
            "variants": "sky130A gf180mcuD ihp-sg13g2",
            "gf180mcu_open_pdks": "a" * 40,
        },
    }


def _preflight(tmp_path, monkeypatch, payload, *, pdk=None, code=0):
    calls = []

    def get_object(argv, timeout):
        calls.append(argv[:-1])
        if isinstance(payload, Exception):
            raise payload
        if code == 0:
            Path(argv[-1]).write_text(payload)
        return subprocess.CompletedProcess(argv, code)

    _pin_client_version(monkeypatch, "0.9.1")
    monkeypatch.setattr(
        sb.sim_batch_image, "read_batch_runner_images", read_batch_runner_images
    )
    monkeypatch.setattr(sb, "_run_subprocess", get_object)
    runner, resolved = {}, {}
    provision = _provision_script(tmp_path)
    provision.with_name("batch-fleet.env").write_text(
        'BATCH_IMAGES_PREFIX="published"\nBATCH_LAUNCH_TEMPLATE="test-runner"\n'
        'BATCH_JOB_BUCKET="test-job-bucket"\nBATCH_REGION="us-east-1"\n'
    )
    request = {
        "batch": {"provision_script_path": str(provision), "profile": "test-submit"}
    }
    if pdk:
        request["models"] = {"pdk": pdk}
    sb.dry_run_runner_check(
        request, corner_count=75, timeout_s=30, runner_out=runner, config_out=resolved
    )
    sb._assess_deck_support(request, resolved["config"])
    return runner, calls


def test_batch_preflight_uses_one_resolved_pointer(tmp_path, monkeypatch, pointer):
    runner, calls = _preflight(
        tmp_path, monkeypatch, json.dumps(pointer), pdk="gf180mcuD"
    )
    assert calls == [
        [
            "aws",
            "--region",
            "us-east-1",
            "--profile",
            "test-submit",
            "s3api",
            "get-object",
            "--bucket",
            "test-job-bucket",
            "--key",
            "published/test-runner/current.json",
        ]
    ]
    assert runner["status"] == "match"
    assert runner["images"] == [
        {
            "pdk": "gf180mcu",
            "region": "us-east-1",
            "ami_id": pointer["ami_id"],
            "klt_version": "0.9.2",
            "klt_ref": None,
            "pdk_variants": pointer["pdk"]["variants"].split(),
            "open_pdks_commit": "a" * 40,
            "ngspice_version": "47",
            "pdk_snapshot": None,
        }
    ]


def test_fallback_version_refuses_under_enforce(tmp_path, monkeypatch, pointer):
    del pointer["tools"]["klt_build"]
    with pytest.raises(SimError) as error:
        _preflight(tmp_path, monkeypatch, json.dumps(pointer))
    assert error.value.code == "batch_runner_version_mismatch"
    assert pointer["ami_id"] in str(error.value)


def test_unpinned_pdk_and_pre_pin_images(tmp_path, monkeypatch, pointer):
    pointer["pdk"]["sky130_open_pdks"] = "unknown"
    runner, calls = _preflight(tmp_path, monkeypatch, json.dumps(pointer))
    assert len(calls) == 1
    assert {r["pdk"]: r["open_pdks_commit"] for r in runner["images"]} == {
        "sky130A": None,
        "gf180mcu": "a" * 40,
        "sg13g2": None,
    }


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("", 1),
        ("AccessDenied", 254),
        ("{", 0),
        ("[]", 0),
        (FileNotFoundError("aws"), 0),
        (subprocess.TimeoutExpired("aws", 5), 0),
    ],
)
def test_unavailable_pointer_is_unknown(tmp_path, monkeypatch, payload, code):
    runner, calls = _preflight(tmp_path, monkeypatch, payload, code=code)
    assert len(calls) == 1
    assert (runner["status"], runner["images"]) == ("unknown", [])


@pytest.mark.parametrize(
    "change",
    [
        {"region": "us-west-2"},
        {"schema": "eda-batch-image/2"},
        {"ami_id": None},
        {"tools": []},
        {"pdk": {"variants": ["gf180mcuD", None]}},
    ],
)
def test_unusable_record_is_unknown(tmp_path, monkeypatch, pointer, change):
    runner, _ = _preflight(tmp_path, monkeypatch, json.dumps(pointer | change))
    assert (runner["status"], runner["images"]) == ("unknown", [])


def test_default_pointer_does_not_describe_cmos5l(tmp_path, monkeypatch, pointer):
    runner, calls = _preflight(
        tmp_path, monkeypatch, json.dumps(pointer), pdk="ihp-sg13cmos5l"
    )
    assert (runner["status"], runner["images"], calls) == ("unknown", [], [])
