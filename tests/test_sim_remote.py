"""Worker support gate for ``measurements[].transfer`` on the ``remote``
backend (issue #2924): an unsupported or unverifiable host must refuse the
request instead of accepting it and omitting the metric.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import sim, sim_batch, sim_remote

_TRANSFER_REQUEST = {
    "netlist": "body.spice",
    "models": {"pdk": "sky130A"},
    "options": {},
    "measurements": [
        {
            "name": "pm",
            "transfer": {
                "num": "v(out)",
                "den": "v(in)",
                "metric": "phase_margin_deg",
            },
        }
    ],
}
_PLAIN_REQUEST = {"netlist": "body.spice", "models": {"pdk": "sky130A"}, "options": {}}


def _image(**runner) -> dict:
    image = {
        "pdk": "sky130A",
        "region": "us-east-1",
        "ami_id": "ami-0123456789abcdef0",
        "pdk_snapshot": "sky130A-2026.10.01",
        "ngspice_version": "46",
        "built_at": "2026-10-01T00:00:00Z",
    }
    image.update(runner)
    return image


def _manifest(tmp_path: Path, images: list[dict]) -> str:
    path = tmp_path / "ami-manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "images": images}))
    return str(path)


def _fake_klt(tmp_path: Path, version_line: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    klt = bin_dir / "klt"
    marker = tmp_path / "sim-ran"
    klt.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "--version" ]; then\n'
        f"echo '{version_line}'; exit 0; fi\n"
        f"touch {marker}\n"
    )
    klt.chmod(0o755)
    return bin_dir


def _run_command(tmp_path: Path, request: dict, version_line: str):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    command = sim_remote._remote_feature_preflight(request) + "klt sim r.json"
    bin_dir = _fake_klt(tmp_path, version_line)
    proc = subprocess.run(
        [bash, "-c", command],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    return proc, (tmp_path / "sim-ran").exists()


@pytest.mark.parametrize("version", ["klt 0.7.0", "klt dirty", "klt"])
def test_host_preflight_refuses_transfer_on_an_old_or_unknown_klt(tmp_path, version):
    proc, ran = _run_command(tmp_path, _TRANSFER_REQUEST, version)
    assert proc.returncode == sim_batch._BATCH_PREFLIGHT_REJECT_EXIT
    assert not ran
    assert "transfer" in proc.stderr and "not known to support" in proc.stderr


@pytest.mark.parametrize("version", ["klt 0.7.1", "klt 0.7.1.dev9+gabc", "klt 0.8.0"])
def test_host_preflight_runs_transfer_on_a_supporting_klt(tmp_path, version):
    proc, ran = _run_command(tmp_path, _TRANSFER_REQUEST, version)
    assert proc.returncode == 0, proc.stderr
    assert ran


def test_preflight_is_empty_without_a_mandatory_feature():
    assert sim_remote._remote_feature_preflight(_PLAIN_REQUEST) == ""


def test_job_description_carries_the_preflight_only_for_transfer(tmp_path):
    (tmp_path / "body.spice").write_text("R1 a b 1k\n")
    netlist = str(tmp_path / "body.spice")
    with_transfer = sim_remote._build_remote_job_description(
        _TRANSFER_REQUEST, netlist, request_dir=str(tmp_path)
    )
    plain = sim_remote._build_remote_job_description(
        _PLAIN_REQUEST, netlist, request_dir=str(tmp_path)
    )
    assert "klt --version" in with_transfer.command
    assert with_transfer.command.endswith(plain.command)
    assert plain.command.startswith("klt sim ")


def test_prelaunch_refuses_transfer_when_the_manifest_image_predates_it(tmp_path):
    manifest = _manifest(tmp_path, [_image(klt_version="0.7.0")])
    with pytest.raises(sim.SimError) as raised:
        sim_remote._assess_remote_runner_features(
            _TRANSFER_REQUEST,
            "sky130A",
            {"region": "us-east-1", "ami_manifest": manifest},
        )
    assert raised.value.code == sim_batch.BATCH_RUNNER_FEATURE_CODE
    assert "no instance was launched" in str(raised.value)


@pytest.mark.parametrize(
    "images",
    [[_image()], [_image(klt_version="0.7.1")], []],
    ids=["legacy-entry", "supporting", "empty"],
)
def test_prelaunch_defers_to_the_host_when_unknown_or_supported(tmp_path, images):
    manifest = _manifest(tmp_path, images)
    sim_remote._assess_remote_runner_features(
        _TRANSFER_REQUEST, "sky130A", {"region": "us-east-1", "ami_manifest": manifest}
    )


def test_prelaunch_ignores_a_request_without_transfer(tmp_path):
    manifest = _manifest(tmp_path, [_image(klt_version="0.7.0")])
    sim_remote._assess_remote_runner_features(
        _PLAIN_REQUEST, "sky130A", {"region": "us-east-1", "ami_manifest": manifest}
    )
