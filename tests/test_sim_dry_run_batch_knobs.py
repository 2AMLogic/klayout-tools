"""``klt sim --dry-run`` refuses every malformed ``request.batch`` knob the
submit path refuses (issue #6437).

Split out of ``test_sim_dry_run_batch_ceiling.py`` for the file-size
ratchet; same differential-oracle shape -- the dry run versus the submit
path's own ``preflight_batch_request``, asserting the same ``error.code``
and message. The rows here are the knobs #5674's differential left
dry-running clean: each knob's own resolver refuses on the submit path
while ``dry_run_runner_check``'s swallow-all-but-runner-codes filter hid
it. ``capacity_retries`` is the same defect at the same site and travels
with them. A malformed knob refuses even on a host that resolves no batch
configuration: the knob is the request's own defect, not this host's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools.cli import main
from klayout_tools.sim_batch_plan import preflight_batch_request

FAKE_BUCKET = "fake-batch-jobs-000000000000"

CORNERS = {"process": ["TYP"], "params": {"vdd": [1.6, 1.8, 2.0]}}


@pytest.fixture(autouse=True)
def _batch_env(monkeypatch):
    """No fleet config from this host: every batch knob comes from the
    request, so a refusal names the request's own value."""
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    monkeypatch.delenv(sb.PROVISION_SCRIPT_ENV, raising=False)
    monkeypatch.delenv(sb.BUCKET_ENV, raising=False)
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)


def _write_request(
    tmp_path: Path,
    *,
    batch: dict | None = None,
) -> str:
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    (tmp_path / "corner.lib").write_text("* lib\n")
    (tmp_path / "prov.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (tmp_path / "prov.sh").chmod(0o755)
    request: dict = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "models": {"lib": "corner.lib"},
        "backend": "batch",
        "corners": CORNERS,
        "options": {"timeout_s": 60},
        "batch": {
            "bucket": FAKE_BUCKET,
            "region": "us-east-1",
            "provision_script_path": str(tmp_path / "prov.sh"),
            **(batch or {}),
        },
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return str(path)


def _configless_batch_request(tmp_path: Path, batch: dict) -> str:
    """A ``batch`` block carrying knobs but no bucket and no provision
    script: this host can resolve no fleet config from it, while every knob
    in it is still the request's own property."""
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    (tmp_path / "corner.lib").write_text("* lib\n")
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "models": {"lib": "corner.lib"},
                "backend": "batch",
                "corners": CORNERS,
                "options": {"timeout_s": 60},
                "batch": dict(batch),
            }
        )
    )
    return str(path)


KNOB_ROWS = [
    ("job_ceiling_check", "bogus"),
    ("max_concurrent_jobs", -1),
    ("runner_version_check", "x"),
    ("max_job_seconds", 0),
    ("capacity_wait_s", "x"),
    ("interrupted_grace_s", -5),
    ("job_timeout_s", -1),
    ("capacity_retries", -1),
]
KNOB_IDS = [f"{knob}={value!r}" for knob, value in KNOB_ROWS]


@pytest.mark.parametrize(("knob", "value"), KNOB_ROWS, ids=KNOB_IDS)
def test_malformed_batch_knob_is_refused_like_the_submit(tmp_path, knob, value):
    path = _write_request(tmp_path, batch={knob: value})
    with pytest.raises(sim.SimError) as dry:
        sim.run_sim(path, backend="batch", dry_run=True)
    with pytest.raises(sim.SimError) as oracle:
        preflight_batch_request(path)
    assert dry.value.code == oracle.value.code
    assert str(dry.value) == str(oracle.value)


@pytest.mark.parametrize(("knob", "value"), KNOB_ROWS, ids=KNOB_IDS)
def test_configless_host_still_refuses_a_malformed_knob(tmp_path, knob, value):
    # The knob is a request property, not host config: it refuses with the
    # message the submit path gives when the configuration *does* resolve.
    (tmp_path / "cfg").mkdir()
    (tmp_path / "nocfg").mkdir()
    configured = _write_request(tmp_path / "cfg", batch={knob: value})
    with pytest.raises(sim.SimError) as oracle:
        preflight_batch_request(configured)
    configless = _configless_batch_request(tmp_path / "nocfg", {knob: value})
    with pytest.raises(sim.SimError) as dry:
        sim.run_sim(configless, backend="batch", dry_run=True)
    assert str(dry.value) == str(oracle.value)


def test_configless_host_stays_valid_for_a_valid_batch_block(tmp_path):
    # #5449 posture: valid knobs plus no bucket/provision script dries-run
    # clean -- only malformed knobs refuse ahead of the host-dependent
    # resolution.
    path = _configless_batch_request(
        tmp_path,
        {
            "job_ceiling_check": "split",
            "runner_version_check": "warn",
            "capacity_wait_s": 30,
            "interrupted_grace_s": 0,
        },
    )
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    assert "job_ceiling" not in report


def test_cli_knob_refusal_exits_1_with_the_code(tmp_path, capsys):
    path = _write_request(tmp_path, batch={"max_concurrent_jobs": -1})
    code = main(["sim", path, "--backend", "batch", "--dry-run", "--format", "json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == sb.BATCH_MAX_CONCURRENT_CODE
