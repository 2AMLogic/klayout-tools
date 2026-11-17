"""Tests for the machine-readable "incomplete, resumable" signal that
`_run_batch_fleet` adds to `environment.remote` (issue #4856). Split out of
`test_sim_batch.py` to keep that file under its size ceiling. Also pins the
issue #5542 contract: an unlaunched shard is a `null` `fleet[]` slot at its
own index (skipped by the `job_ids` roll-up), and every `shard_map` emission
is repo-relative when the map lies inside a git repo (absolute otherwise),
so an exit-4 envelope passes `klt env-provenance lint-envelope` cleanly."""

from __future__ import annotations

import json

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    _CLIENT_VERSION,
    _batch_request,
    _FakeRunner,
    _identity,
    _install_fake_batch_transport,
    _pin_client_version,
    _run,
    _shard_resume_fixture,
    _sim_report,
    _status,
    _write_body,
    _write_request,
    uploaded_request_of,
)


def _capacity_refused_fleet(tmp_path, monkeypatch):
    """`_shard_resume_fixture` whose second shard's launch is refused for
    capacity (`batch_no_capacity`): shard 0 runs, shard 1 stays unlaunched."""
    _, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    real_launch = sb.launch_job
    seen: list[str] = []

    def _launch(config, job_id, **kwargs):
        if seen:
            raise sb.BatchError("refused", code=sb.BATCH_NO_CAPACITY_CODE)
        seen.append(job_id)
        return real_launch(config, job_id, **kwargs)

    monkeypatch.setattr(sb, "launch_job", _launch)
    return fleet, manifest_path


def test_unlaunched_shards_are_a_machine_readable_resumable_signal(
    tmp_path, monkeypatch
):
    """Issue #4856: a capacity-refused shard (capacity_wait_s 0) surfaces
    ``unlaunched_shards`` / ``resumable`` in ``environment`` without any new
    exit code or changed corner message. Issue #5542: with the artifacts dir
    inside a git repo, ``shard_map`` is emitted repo-relative."""
    (tmp_path / ".git").mkdir()
    fleet, _manifest_path = _capacity_refused_fleet(tmp_path, monkeypatch)
    _, _, environment = fleet()
    assert environment["unlaunched_shards"] == 1
    assert environment["resumable"] is True
    assert environment["shard_map"] == "artifacts/batch-shards.json"


def test_shard_map_stays_absolute_outside_a_repo(tmp_path, monkeypatch):
    """Issue #5542's outside-a-repo boundary: with no repo around the run,
    the string field keeps the absolute path instead of retyping to null."""
    fleet, manifest_path = _capacity_refused_fleet(tmp_path, monkeypatch)
    _, _, environment = fleet()
    assert environment["unlaunched_shards"] == 1
    assert environment["shard_map"] == str(manifest_path)


def _unlaunched_split_report(tmp_path, monkeypatch):
    """A 4-corner request split into 2 batch jobs (`max_units_per_job`) whose
    second launch is capacity-refused, driven through `sim.run_sim` end to
    end against the faked transport, so the report's `environment.remote` is
    the finished block a client actually reads."""
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
            index = int(points[0]["temperature_c"]) // 2
            shard_of_job[job_id] = index
            runner.outputs_by_job[job_id] = {
                "report.json": _sim_report(
                    corner_ids=tuple(
                        f"tt_1.800_{int(p['temperature_c'])}" for p in points
                    )
                ),
                "runner_identity.json": _identity(_CLIENT_VERSION),
            }
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture)

    real_launch = sb.launch_job

    def _refuse_second(config, job_id, *, runner=None):
        if shard_of_job.get(job_id) == 1:
            raise sb.BatchError("refused", code=sb.BATCH_NO_CAPACITY_CODE)
        return real_launch(config, job_id, runner=runner)

    monkeypatch.setattr(sb, "launch_job", _refuse_second)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)

    _write_body(tmp_path)
    request = _batch_request(
        tmp_path, corners={"supply_v": {"vdd": [1.8]}, "temperature_c": [0, 1, 2, 3]}
    )
    request["batch"]["max_units_per_job"] = 2
    report = _run(_write_request(tmp_path, request), tmp_path)
    return report, report["environment"]["remote"]


def test_unlaunched_shard_is_a_null_fleet_slot_at_its_index(tmp_path, monkeypatch):
    """Issue #5542: an unlaunched shard is a `null` entry in `fleet[]` at its
    own shard index -- a dict at every shard that reported -- and the
    `job_ids` roll-up omits it, so a client reading per-job metadata can
    skip the null slots instead of crashing on them."""
    (tmp_path / ".git").mkdir()
    report, remote = _unlaunched_split_report(tmp_path, monkeypatch)

    fleet = remote["fleet"]
    assert len(fleet) == 2
    assert isinstance(fleet[0], dict)
    assert fleet[1] is None
    assert remote["unlaunched_shards"] == 1
    assert remote["resumable"] is True
    assert remote["job_ids"] == [fleet[0]["job_id"]]
    assert remote["runner_compatibility"] == "unknown"
    assert any(
        diagnostic["code"] == "lost_shard"
        for corner in report["corners"]
        for diagnostic in corner["diagnostics"]
    )
    assert remote["shard_map"] == "artifacts/batch-shards.json"


def test_unlaunched_envelope_lints_clean_inside_the_repo(tmp_path, monkeypatch):
    """Issue #5542: the unlaunched-shard signal produced inside a repo passes
    `klt env-provenance lint-envelope` with an empty allow-list -- the
    committed-artifact rule the absolute shard_map path used to break."""
    from klayout_tools import env_provenance as ep

    (tmp_path / ".git").mkdir()
    _report, remote = _unlaunched_split_report(tmp_path, monkeypatch)
    envelope = tmp_path / "envelope.json"
    envelope.write_text(json.dumps({"environment": {"remote": remote}}))
    assert ep.lint_envelope_files([str(envelope)])["status"] == "clean"


def test_clean_sharded_run_has_no_unlaunched_signal(tmp_path, monkeypatch):
    _, fleet, _ = _shard_resume_fixture(tmp_path, monkeypatch)
    _, _, environment = fleet()
    assert "unlaunched_shards" not in environment
    assert "resumable" not in environment


def test_unrun_units_are_a_resumable_signal():
    """Issue #5364: lost/interrupted shards carry the same additive signal."""
    from klayout_tools import _batch_common

    env, hint = _batch_common.with_unrun_signal({"job": "x"}, 3, "/o/batch-shards.json")
    assert env == {
        "job": "x",
        "unrun_units": 3,
        "resumable": True,
        "shard_map": "/o/batch-shards.json",
    }
    assert hint is True


def test_unrun_signal_shard_map_is_repo_relative_inside_a_repo(tmp_path):
    """Issue #5542: the `unrun_units` emission routes its retained state
    (shard map or checkpoint) through the repo-relative normalizer too."""
    from klayout_tools import _batch_common

    (tmp_path / ".git").mkdir()
    state = tmp_path / "out" / "batch-shards.json"
    env, hint = _batch_common.with_unrun_signal({"job": "x"}, 3, str(state))
    assert env["shard_map"] == "out/batch-shards.json"
    assert hint is True


def test_unrun_signal_absent_when_nothing_unrun_or_no_state():
    from klayout_tools import _batch_common

    base = {"job": "x"}
    assert _batch_common.with_unrun_signal(base, 0, "/p") == (base, False)
    assert _batch_common.with_unrun_signal(base, 2, None) == (base, False)


def test_unrun_signal_mixed_with_unlaunched_keeps_one_hint():
    from klayout_tools import _batch_common

    base = {"unlaunched_shards": 1, "resumable": True, "shard_map": "/m"}
    env, hint = _batch_common.with_unrun_signal(base, 4, "/other")
    assert env["shard_map"] == "/m" and env["unrun_units"] == 4
    assert hint is False


def _wired(tmp_path, *, manifest, checkpoint, unrun=2):
    from klayout_tools import _batch_common
    from klayout_tools.sim_batch import BATCH_SHARDS_FILENAME

    if manifest:
        (tmp_path / BATCH_SHARDS_FILENAME).write_text("{}")
    ckpt = str(tmp_path / "ckpt.json") if checkpoint else None
    return _batch_common.apply_unrun_signal({"job": "x"}, unrun, str(tmp_path), ckpt)


def test_apply_unrun_signal_prefers_manifest_and_hints_once(tmp_path, capsys):
    env = _wired(tmp_path, manifest=True, checkpoint=True)
    assert env["unrun_units"] == 2 and env["resumable"] is True
    assert env["shard_map"] == str(tmp_path / "batch-shards.json")
    err = capsys.readouterr().err
    assert err.count("rerun with --resume") == 1
    assert "2 unit(s) never ran" in err and env["shard_map"] in err


def test_apply_unrun_signal_checkpoint_only(tmp_path, capsys):
    env = _wired(tmp_path, manifest=False, checkpoint=True)
    assert env["shard_map"] == str(tmp_path / "ckpt.json")
    assert "rerun with --resume" in capsys.readouterr().err


def test_apply_unrun_signal_no_state_or_nothing_unrun(tmp_path, capsys):
    assert _wired(tmp_path, manifest=False, checkpoint=False) == {"job": "x"}
    assert _wired(tmp_path, manifest=True, checkpoint=True, unrun=0) == {"job": "x"}
    assert capsys.readouterr().err == ""
