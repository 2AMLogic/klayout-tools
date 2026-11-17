"""Top-level roll-ups on a sharded batch ``environment.remote`` (issue #5522):
``job_ids`` and a worst-of ``runner_compatibility``.

Split from ``test_sim_batch.py`` to keep it under its file-size ceiling; the
sharded fixtures are imported from there rather than re-built, so these tests
exercise the real split path (a `batch.max_units_per_job` request driven
through `sim.run_sim` end to end against the faked S3 transport), not a
hand-written `fleet[]` block.
"""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_features as sbf
from test_sim_batch import (
    _CLIENT_VERSION,
    _batch_request,
    _FakeRunner,
    _identity,
    _install_fake_batch_transport,
    _pin_client_version,
    _run,
    _sim_report,
    _status,
    _write_body,
    _write_request,
    uploaded_request_of,
)


def _split_rollup_report(
    tmp_path,
    monkeypatch,
    *,
    runner_versions=(_CLIENT_VERSION, _CLIENT_VERSION),
    lost_shard=None,
    cap=2,
    via="max_units_per_job",
):
    """Run a 4-unit request end to end as ``4 / cap`` batch jobs; return the
    finished report and its ``environment.remote``.

    ``via`` picks which of the two sharding entry points gets there:
    ``"max_units_per_job"`` (a `remote.hosts: 1` split) or ``"hosts"`` (an
    explicit `remote.hosts: 4 / cap`). Both must produce the roll-ups.

    ``runner_versions`` is the klt version each shard's own
    ``runner_identity.json`` reports, **in shard order** -- the shard index is
    derived from the first unit each job was sent, never from upload order,
    which concurrent shards do not fix. ``lost_shard`` makes that one shard's
    launch raise, so it comes back as a ``null`` ``fleet[]`` entry while its
    siblings still report.
    """
    _pin_client_version(monkeypatch)
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    shard_of_job: dict[str, int] = {}

    def _capture(argv, timeout_s):
        captured = uploaded_request_of(argv)
        if captured is not None:
            document, job_id = captured
            points = document["_explicit_points"]
            index = int(points[0]["temperature_c"]) // cap
            shard_of_job[job_id] = index
            runner.outputs_by_job[job_id] = {
                "report.json": _sim_report(
                    corner_ids=tuple(
                        f"tt_1.800_{int(p['temperature_c'])}" for p in points
                    )
                ),
                "runner_identity.json": _identity(runner_versions[index]),
            }
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture)

    real_launch = sb.launch_job

    def _maybe_lose(config, job_id, *, runner=None):
        if lost_shard is not None and shard_of_job.get(job_id) == lost_shard:
            raise sb.BatchError("create-fleet refused every pool")
        return real_launch(config, job_id, runner=runner)

    monkeypatch.setattr(sb, "launch_job", _maybe_lose)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)

    _write_body(tmp_path)
    request = _batch_request(
        tmp_path, corners={"supply_v": {"vdd": [1.8]}, "temperature_c": [0, 1, 2, 3]}
    )
    if via == "hosts":
        request["remote"] = {"hosts": 4 // cap}
    else:
        request["batch"]["max_units_per_job"] = cap
    report = _run(_write_request(tmp_path, request), tmp_path)
    return report, report["environment"]["remote"]


@pytest.mark.parametrize("via", ["max_units_per_job", "hosts"])
def test_sharded_run_rolls_up_job_ids_and_a_matching_runner_compatibility(
    tmp_path, monkeypatch, via
):
    """Issue #5522: a sharded run's job identity and runner-skew check used to
    live only inside ``fleet[]``, so a reader of the single-job shape got
    ``None`` for both. Both are now rolled up to the top level -- reached
    through a `remote.hosts: 1` split and through an explicit `hosts > 1`
    alike, since both finalise the same block."""
    report, remote = _split_rollup_report(tmp_path, monkeypatch, via=via)

    assert [corner["corner_id"] for corner in report["corners"]] == [
        f"tt_1.800_{index}" for index in range(4)
    ]
    fleet = remote["fleet"]
    assert len(fleet) == 2
    assert remote["job_ids"] == [entry["job_id"] for entry in fleet]
    assert all(job_id.startswith("klt-sim-") for job_id in remote["job_ids"])
    assert remote["runner_compatibility"] == "match"
    # The per-shard entries stay the contract for everything else, untouched.
    assert [entry["runner_compatibility"] for entry in fleet] == ["match", "match"]
    assert all("job_ids" not in entry for entry in fleet)


def test_split_run_with_a_skewed_shard_rolls_up_mismatch(tmp_path, monkeypatch):
    """One mismatching shard makes the roll-up ``mismatch`` -- the worst-of
    rule, so a skewed runner can never be recorded as a clean one."""
    _, remote = _split_rollup_report(
        tmp_path, monkeypatch, runner_versions=(_CLIENT_VERSION, "0.4.2")
    )

    assert [entry["runner_compatibility"] for entry in remote["fleet"]] == [
        "match",
        "mismatch",
    ]
    assert remote["runner_compatibility"] == "mismatch"
    assert len(remote["job_ids"]) == 2


def test_split_run_with_a_lost_shard_rolls_up_unknown(tmp_path, monkeypatch):
    """A lost (``null``) shard is not evidence of a match: the roll-up is
    ``unknown``, and ``job_ids`` carries only the shards that have one."""
    report, remote = _split_rollup_report(tmp_path, monkeypatch, lost_shard=1)

    fleet = remote["fleet"]
    assert fleet[1] is None
    assert fleet[0]["runner_compatibility"] == "match"
    assert remote["runner_compatibility"] == "unknown"
    assert remote["job_ids"] == [fleet[0]["job_id"]]
    assert any(
        diagnostic["code"] == "lost_shard"
        for corner in report["corners"]
        for diagnostic in corner["diagnostics"]
    )


def test_single_job_run_gains_neither_fleet_nor_job_ids(tmp_path, monkeypatch):
    """The single-job shape is unchanged: no ``fleet``, no ``job_ids``, and
    ``runner_compatibility`` is still that one job's own value."""
    _pin_client_version(monkeypatch)
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(),
        "runner_identity.json": _identity(_CLIENT_VERSION),
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)

    report = _run(_write_request(tmp_path, _batch_request(tmp_path)), tmp_path)

    remote = report["environment"]["remote"]
    assert "fleet" not in remote
    assert "job_ids" not in remote
    assert remote["job_id"].startswith("klt-sim-")
    assert remote["runner_compatibility"] == "match"


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["match", "match"], "match"),
        (["match", "mismatch"], "mismatch"),
        (["unknown", "mismatch"], "mismatch"),  # mismatch outranks unknown
        (["match", "unknown"], "unknown"),
        (["unknown", "unknown"], "unknown"),
        ([None, "match"], "unknown"),  # a lost shard proves nothing
        ([None, "mismatch"], "mismatch"),
        (["match", "__absent__"], "unknown"),  # pre-#2719 block, no field
        (["match", "something-new"], "unknown"),  # a value this klt cannot read
        ([], "unknown"),  # never a vacuous "match"
    ],
)
def test_fleet_runner_compatibility_is_worst_of(statuses, expected):
    fleet = [
        (
            None
            if status is None
            else ({} if status == "__absent__" else {"runner_compatibility": status})
        )
        for status in statuses
    ]
    assert sbf.fleet_runner_compatibility(fleet) == expected


def test_attach_fleet_rollups_skips_a_non_batch_backend_and_a_flat_block():
    """The SSH ``remote`` backend's ``fleet[]`` entries carry no runner
    identity, so it is left alone; so is a block with no ``fleet`` at all."""
    ssh = {"remote": {"fleet": [{"instance_id": "i-0", "attempts": 1}]}}
    sbf.attach_fleet_rollups(ssh, "remote")
    assert ssh == {"remote": {"fleet": [{"instance_id": "i-0", "attempts": 1}]}}

    flat = {"remote": {"job_id": "klt-sim-1", "runner_compatibility": "match"}}
    before = json.loads(json.dumps(flat))
    sbf.attach_fleet_rollups(flat, "batch")
    assert flat == before

    # No `environment.remote` at all (every shard lost): nothing to roll up.
    empty: dict = {}
    sbf.attach_fleet_rollups(empty, "batch")
    assert empty == {}


def test_finalize_batch_remote_runs_both_post_processing_steps():
    """``sim.apply_engine_host`` calls one finaliser, so the models_lib
    comparison (#5321) and the roll-ups (#5522) must both come out of it."""
    environment = {
        "models_lib_sha256": "a" * 64,
        "remote": {"fleet": [{"job_id": "j0", "runner_compatibility": "match"}]},
    }
    sbf.finalize_batch_remote(environment, "batch")

    remote = environment["remote"]
    assert remote["job_ids"] == ["j0"]
    assert remote["runner_compatibility"] == "match"
    assert remote["fleet"][0]["models_lib_comparison"]["status"] == "not_comparable"
