"""Issue #5378: a deck list splits into several batch jobs under
``max_units_per_job`` / ``max_concurrent_jobs``, writes a shard map, merges
the reports in request order, and ``--attach <map> [--collect-partial]``
collects it later."""

from __future__ import annotations

import json
import os
from threading import Barrier
from unittest.mock import Mock

import pytest

from klayout_tools import sim, sim_decks
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_shards as sbs
from test_sim_decks import (
    _FakeFleet,
    batch_deck_request,
    install_fake_batch,
    write_request,
    write_stub_engine,
)


class _MultiFleet(_FakeFleet):
    """A fake fleet that keeps one spec per job id."""

    def __init__(self, status=None):
        super().__init__(status or {"state": "done", "exit_code": "0"})
        self.statuses: dict[str, dict] = {}

    def submit(self, config, job_id, spec, **kwargs):
        self.specs[job_id] = spec

    def poll(self, config, job_id, **kwargs):
        return dict(self.statuses.get(job_id, self.status))

    def collect(self, config, job_id, local_dir, **kwargs):
        worker = _FakeFleet(self.status, fail_collect=self.fail_collect)
        worker.submit(config, job_id, self.specs[job_id])
        worker.collect(config, job_id, local_dir, **kwargs)


def _install(monkeypatch, fleet):
    install_fake_batch(monkeypatch, fleet)
    monkeypatch.setattr(sb, "job_exists", lambda config, job_id: True)


def _request(tmp_path, count=5, **batch):
    request = batch_deck_request(tmp_path)
    base = request["decks"][0]
    request["decks"] = [
        {"id": f"d{i}", "deck": base["deck"], "outputs": [], "timeout_s": 30}
        for i in range(count)
    ]
    request["batch"].update(batch)
    return request


def _run(tmp_path, request, **kwargs):
    path = write_request(tmp_path, request)
    return sim.run_sim(
        str(path),
        backend="batch",
        artifacts_dir=str(tmp_path / "out"),
        **kwargs,
    )


def _staged_ids(spec):
    worker = json.loads(
        next(i.content for i in spec.inputs if i.name == "request.json")
    )
    return [d["id"] for d in worker["decks"]]


def test_split_submits_balanced_jobs_and_merges_in_order(tmp_path, monkeypatch):
    # Let all three shards select their job before any fake worker reads its spec.
    collectors = Barrier(3, timeout=10)
    collect = _FakeFleet.collect

    def collect_together(self, config, job_id, local_dir, **kwargs):
        collectors.wait()
        return collect(self, config, job_id, local_dir, **kwargs)

    monkeypatch.setattr(_FakeFleet, "collect", collect_together)
    (tmp_path / ".git").mkdir()
    fleet = _install_and_fleet(monkeypatch)
    report = _run(tmp_path, _request(tmp_path), max_units_per_job=2)
    sizes = sorted(len(_staged_ids(s)) for s in fleet.specs.values())
    assert sizes == [1, 2, 2]
    assert [u["id"] for u in report["units"]] == [f"d{i}" for i in range(5)]
    assert report["unit_count"] == 5 and report["status"] == "pass"
    remote = report["environment"]["remote"]
    assert remote["split"] and remote["shard_count"] == 3
    # Issue #5542: inside a git repo the deck split's shard_map is emitted
    # repo-relative, like every other shard_map emission.
    assert remote["shard_map"] == "out/batch-shards.json"
    doc = json.load(open(os.path.join(tmp_path / "out", sb.BATCH_SHARDS_FILENAME)))
    assert len(doc["shards"]) == 3 and doc["deck_slices"][0] == ["d0", "d1"]


def _install_and_fleet(monkeypatch):
    fleet = _MultiFleet()
    _install(monkeypatch, fleet)
    return fleet


def test_request_field_and_concurrency_bound(tmp_path, monkeypatch):
    fleet = _install_and_fleet(monkeypatch)
    report = _run(
        tmp_path,
        _request(tmp_path, count=4, max_units_per_job=1, max_concurrent_jobs=1),
    )
    assert len(fleet.specs) == 4
    assert report["environment"]["remote"]["shard_count"] == 4


@pytest.mark.parametrize(
    "kwargs", [{}, {"max_units_per_job": 5}, {"max_units_per_job": 50}]
)
def test_no_split_is_one_job_and_no_shard_map(tmp_path, monkeypatch, kwargs):
    fleet = _install_and_fleet(monkeypatch)
    report = _run(tmp_path, _request(tmp_path), **kwargs)
    assert len(fleet.specs) == 1
    assert not os.path.exists(tmp_path / "out" / sb.BATCH_SHARDS_FILENAME)
    assert "split" not in report["environment"]["remote"]


def test_single_deck_with_cap_one_job(tmp_path, monkeypatch):
    fleet = _install_and_fleet(monkeypatch)
    _run(tmp_path, _request(tmp_path, count=1), max_units_per_job=1)
    assert len(fleet.specs) == 1


def test_failed_shard_keeps_other_shards(tmp_path, monkeypatch):
    fleet = _install_and_fleet(monkeypatch)
    real_submit = fleet.submit
    calls = []

    def submit(config, job_id, spec, **kw):
        calls.append(job_id)
        if len(calls) == 2:
            raise sb.BatchError("boom", code="batch_submit_failed")
        real_submit(config, job_id, spec, **kw)

    monkeypatch.setattr(sb, "submit_job", submit)
    report = _run(tmp_path, _request(tmp_path, count=4), max_units_per_job=2)
    statuses = [u["status"] for u in report["units"]]
    assert statuses[:2] == ["pass", "pass"] and statuses[2:] == ["error", "error"]
    shards = report["environment"]["remote"]["shards"]
    assert [s["state"] for s in shards] == ["collected", "failed"]


def _submit_split(tmp_path, monkeypatch, count=4):
    fleet = _install_and_fleet(monkeypatch)
    request = _request(tmp_path, count=count)
    _run(tmp_path, request, max_units_per_job=2)
    return fleet, request


def test_attach_shard_map_collects_all(tmp_path, monkeypatch):
    fleet, request = _submit_split(tmp_path, monkeypatch)
    map_path = str(tmp_path / "out" / sb.BATCH_SHARDS_FILENAME)
    path = write_request(tmp_path, request)
    report = sim.run_sim(
        str(path),
        backend="batch",
        artifacts_dir=str(tmp_path / "out"),
        attach_job_id=map_path,
        max_units_per_job=2,
    )
    assert [u["id"] for u in report["units"]] == ["d0", "d1", "d2", "d3"]
    assert report["status"] == "pass"


@pytest.mark.parametrize(
    "legacy_map", [False, True], ids=["per-option", "whole-options"]
)
def test_attach_shard_map_repeats_max_workers(tmp_path, monkeypatch, legacy_map):
    _install_and_fleet(monkeypatch)
    request = _request(tmp_path, count=4, max_units_per_job=2)
    _run(tmp_path, request, max_workers=3)
    map_path = tmp_path / "out" / sb.BATCH_SHARDS_FILENAME
    if legacy_map:
        document = json.loads(map_path.read_text())
        sections = document["request_sections"]
        for key in list(sections):
            if key.startswith("options."):
                del sections[key]
        submitted_options = {**request["options"], "max_workers": 3}
        sections["options"] = sbs.legacy_request_sha256(submitted_options)[:12]
        map_path.write_text(json.dumps(document))

    with pytest.raises(sim.SimError) as err:
        _run(tmp_path, request, attach_job_id=str(map_path))
    assert err.value.code == sb.BATCH_SHARD_MANIFEST_CODE
    field = "options" if legacy_map else "options.max_workers"
    assert f"differing request field(s): {field})" in str(err.value)

    report = _run(tmp_path, request, attach_job_id=str(map_path), max_workers=3)
    assert report["status"] == "pass"


def test_attach_collect_partial_lists_unfinished(tmp_path, monkeypatch):
    fleet, request = _submit_split(tmp_path, monkeypatch)
    jobs = list(fleet.specs)
    fleet.statuses[jobs[1]] = {"state": "running"}
    monkeypatch.setattr(
        sb, "_read_status", lambda config, job_id, runner: fleet.poll(config, job_id)
    )
    map_path = str(tmp_path / "out" / sb.BATCH_SHARDS_FILENAME)
    path = write_request(tmp_path, request)
    report = sim.run_sim(
        str(path),
        backend="batch",
        artifacts_dir=str(tmp_path / "out"),
        attach_job_id=map_path,
        collect_partial=True,
        max_units_per_job=2,
    )
    assert [u["status"] for u in report["units"]] == ["pass", "pass", "error", "error"]
    remote = report["environment"]["remote"]
    assert remote["resumable"] is True
    assert remote["unfinished_shards"][0]["unit_ids"] == ["d2", "d3"]
    assert report["units"][2]["diagnostics"][0]["message"].startswith(
        "shard still unfinished"
    )


def test_attach_refuses_foreign_and_corrupt_map(tmp_path, monkeypatch):
    fleet, request = _submit_split(tmp_path, monkeypatch)
    map_path = tmp_path / "out" / sb.BATCH_SHARDS_FILENAME
    other = _request(tmp_path, count=4)
    other["decks"][0]["timeout_s"] = 31
    path = write_request(tmp_path, other)
    with pytest.raises(sim.SimError) as err:
        sim.run_sim(
            str(path),
            backend="batch",
            attach_job_id=str(map_path),
            max_units_per_job=2,
            artifacts_dir=str(tmp_path / "out"),
        )
    assert err.value.code == sb.BATCH_SHARD_MANIFEST_CODE
    map_path.write_text("{not json", encoding="utf-8")
    path = write_request(tmp_path, request)
    with pytest.raises(sim.SimError) as err:
        sim.run_sim(
            str(path),
            backend="batch",
            attach_job_id=str(map_path),
            max_units_per_job=2,
            artifacts_dir=str(tmp_path / "out"),
        )
    assert err.value.code == sb.BATCH_SHARD_MANIFEST_CODE


def test_submit_only_with_split_refused_by_name(tmp_path, monkeypatch):
    _install_and_fleet(monkeypatch)
    with pytest.raises(sim.SimError) as err:
        _run(tmp_path, _request(tmp_path), max_units_per_job=2, submit_only=True)
    assert err.value.code == "batch_decks_split_unsupported"


def test_collect_partial_without_attach_still_refused(tmp_path):
    write_stub_engine(tmp_path)
    with pytest.raises(sim.SimError) as err:
        _run(tmp_path, _request(tmp_path), collect_partial=True)
    assert err.value.code == "collect_partial_requires_attach"


@pytest.mark.parametrize(
    ("cap", "expected"), [(4, [["d0", "d1", "d2"]]), (2, [["d0", "d1"], ["d2"]])]
)
def test_blocking_batch_stages_only_submitted_units(
    tmp_path, monkeypatch, cap, expected
):
    write_stub_engine(tmp_path)
    fleet = _install_and_fleet(monkeypatch)
    request = _request(tmp_path, count=3, max_concurrent_jobs=1)
    stage = Mock(wraps=sim_decks.stage_deck_job)
    monkeypatch.setattr(sim_decks, "stage_deck_job", stage)
    report = _run(tmp_path, request, max_units_per_job=cap, resume=True)

    assert report["status"] == "pass"
    assert [
        [d["id"] for d in c.args[0]["decks"]] for c in stage.call_args_list
    ] == expected
    assert len(fleet.specs) == len(expected)

    stage.reset_mock()
    fleet.specs.clear()
    report = _run(tmp_path, request, max_units_per_job=cap, resume=True)
    assert all(unit["resumed"] for unit in report["units"])
    stage.assert_not_called()
    assert not fleet.specs


def test_late_invalid_unit_rejected_before_any_batch_submission(tmp_path, monkeypatch):
    fleet = _install_and_fleet(monkeypatch)
    request = _request(tmp_path, count=3)
    request["decks"][-1]["deck"] = "missing.sp"

    with pytest.raises(sim.SimError, match="missing.sp"):
        _run(tmp_path, request, max_units_per_job=1)
    assert not fleet.specs
