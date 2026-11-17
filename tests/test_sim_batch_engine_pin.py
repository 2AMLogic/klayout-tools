"""Issue #5851: the request's ``engine_version`` pin is refused before any S3
write or launch when *every* AMI-manifest image the batch fleet publishes
carries an ``ngspice_version`` the pin excludes. Split from `test_sim_batch.py`
(size ratchet, #2766); reuses its manifest and transport fakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    FAKE_BUCKET,
    _batch_request,
    _config,
    _FakeRunner,
    _image,
    _install_fake_batch_transport,
    _publish_manifest,
    _write_body,
    _write_request,
)


def _pinned_request(tmp_path: Path, **overrides) -> dict:
    request = _batch_request(tmp_path)
    request["engine_version"] = "44"
    request.update(overrides)
    return request


def _resolve(request: dict):
    return sb._resolve_batch_config(request, corner_count=1, timeout_s=30.0)


def test_prelaunch_refuses_a_pin_every_published_image_fails(tmp_path, monkeypatch):
    _publish_manifest(tmp_path, monkeypatch, [_image(ngspice_version="46")])
    with pytest.raises(sim.SimError) as raised:
        _resolve(_pinned_request(tmp_path))
    assert raised.value.code == sim.ENGINE_VERSION_UNMET_CODE
    message = str(raised.value)
    assert "not submitted" in message
    assert "no instance was launched" in message
    assert "ngspice 44" in message
    assert "ngspice 46" in message


def test_engine_pin_check_names_every_failing_image(tmp_path, monkeypatch):
    _publish_manifest(
        tmp_path,
        monkeypatch,
        [
            _image(ngspice_version="45.2"),
            _image(region="us-west-2", ngspice_version="45.3"),
        ],
    )
    request = _pinned_request(tmp_path, engine_version="44.1")
    assessment = sb._assess_published_runner(request, _config(tmp_path, region=None))
    with pytest.raises(sim.SimError) as raised:
        sb._enforce_published_engine_version_pin(request, assessment)
    assert raised.value.code == sim.ENGINE_VERSION_UNMET_CODE
    assert "ngspice 44.1" in str(raised.value)
    assert "45.2" in str(raised.value)
    assert "45.3" in str(raised.value)


def test_one_matching_image_defers_to_the_post_run_check(tmp_path, monkeypatch):
    _publish_manifest(
        tmp_path,
        monkeypatch,
        [
            _image(ngspice_version="44"),
            _image(region="us-west-2", ngspice_version="46"),
        ],
    )
    config = _resolve(_pinned_request(tmp_path))
    assert config.bucket == FAKE_BUCKET


def test_a_legacy_entry_without_ngspice_version_defers(tmp_path, monkeypatch):
    legacy = {
        "pdk": "sky130A",
        "region": "us-east-1",
        "ami_id": "ami-0123456789abcdef0",
        "built_at": "2026-01-01T00:00:00Z",
    }
    _publish_manifest(tmp_path, monkeypatch, [legacy])
    config = _resolve(_pinned_request(tmp_path))
    assert config.bucket == FAKE_BUCKET


def test_an_unparseable_ngspice_version_defers(tmp_path, monkeypatch):
    _publish_manifest(tmp_path, monkeypatch, [_image(ngspice_version="bogus")])
    config = _resolve(_pinned_request(tmp_path))
    assert config.bucket == FAKE_BUCKET


def test_a_matching_image_passes(tmp_path, monkeypatch):
    _publish_manifest(tmp_path, monkeypatch, [_image(ngspice_version="44.2")])
    assert _resolve(_pinned_request(tmp_path)).bucket == FAKE_BUCKET
    # A major.minor pin compares both stated components.
    _publish_manifest(tmp_path, monkeypatch, [_image(ngspice_version="44.2")])
    assert _resolve(_pinned_request(tmp_path, engine_version="44.2")).bucket == (
        FAKE_BUCKET
    )


def test_no_pin_set_is_unchanged(tmp_path, monkeypatch):
    _publish_manifest(tmp_path, monkeypatch, [_image(ngspice_version="46")])
    request = _batch_request(tmp_path)
    assert _resolve(request).bucket == FAKE_BUCKET


def test_a_non_ngspice_engine_defers(tmp_path, monkeypatch):
    _publish_manifest(tmp_path, monkeypatch, [_image(ngspice_version="46")])
    request = _pinned_request(tmp_path, engine="xyce")
    config = _resolve(request)
    assert config.bucket == FAKE_BUCKET


def test_dry_run_check_refuses_the_same_refusal(tmp_path, monkeypatch):
    _publish_manifest(tmp_path, monkeypatch, [_image(ngspice_version="46")])
    with pytest.raises(sim.SimError) as raised:
        sb.dry_run_runner_check(
            _pinned_request(tmp_path), corner_count=1, timeout_s=30.0
        )
    assert raised.value.code == sim.ENGINE_VERSION_UNMET_CODE


def test_dry_run_check_reports_ngspice_version_per_image(tmp_path, monkeypatch):
    _publish_manifest(tmp_path, monkeypatch, [_image(ngspice_version="46.1")])
    out: dict = {}
    sb.dry_run_runner_check(
        _pinned_request(tmp_path, engine_version="46"),
        corner_count=1,
        timeout_s=30.0,
        runner_out=out,
    )
    assert out["images"][0]["ngspice_version"] == "46.1"
    assert out["images"][0]["pdk_snapshot"] == "sky130A-2026.10.01"


def test_run_sim_refuses_before_any_submit_or_launch(tmp_path, monkeypatch):
    _publish_manifest(tmp_path, monkeypatch, [_image(ngspice_version="46")])
    runner = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    path = _write_request(
        tmp_path,
        _pinned_request(tmp_path, corners={"temperature_c": [-40, 27, 125]}),
    )
    with pytest.raises(sim.SimError) as raised:
        sim.run_sim(str(path))
    assert raised.value.code == sim.ENGINE_VERSION_UNMET_CODE
    assert runner.argvs_matching("launch", "--apply") == []
