"""`klt sim --batch-set` honors ``--dry-run`` and ``--wait-for-capacity`` (#4771).

Split out of ``test_sim_batch.py`` to keep that file under its size ceiling.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from test_sim_batch import (
    SET_SCHEMA_PATH,
    _fake_report,
    _FakeRunner,
    _install_fake_batch_transport,
    _write_set,
)


def test_sim_set_dry_run_writes_nothing_and_isolates_errors(tmp_path):
    from klayout_tools import sim_set

    paths = _write_set(tmp_path, 3)
    out = tmp_path / "out"
    seen = []

    def fake_run(path, **kwargs):
        seen.append(kwargs)
        if path.endswith("req1.json"):
            raise sim_set.SimError("bad request")
        return {"dry_run": True, "status": "valid", "unit_count": 1}

    report = sim_set.run_sim_set(
        paths,
        outdir=str(out),
        run=fake_run,
        dry_run=True,
        resume=True,
        capacity_retries=2,
    )

    assert all(k["dry_run"] is True and "resume" not in k for k in seen)
    assert not out.exists()
    assert report["dry_run"] is True and report["status"] == "error"
    assert [r["status"] for r in report["requests"]] == ["valid", "error", "valid"]
    assert [r["exit_code"] for r in report["requests"]] == [0, 4, 0]
    jsonschema.validate(report, json.loads(SET_SCHEMA_PATH.read_text()))
    ok = sim_set.run_sim_set(paths[:1], run=fake_run, dry_run=True)
    assert ok["status"] == "valid"
    jsonschema.validate(ok, json.loads(SET_SCHEMA_PATH.read_text()))


def test_sim_set_forwards_capacity_wait_and_rejects_negative(tmp_path):
    from klayout_tools import sim_set

    paths = _write_set(tmp_path, 2)
    waits = []

    def fake_run(path, **kwargs):
        waits.append(kwargs["capacity_wait_s"])
        return _fake_report()

    sim_set.run_sim_set(
        paths, outdir=str(tmp_path / "o"), run=fake_run, capacity_wait_s=30
    )
    assert waits == [30, 30]
    with pytest.raises(sim_set.SimError):
        sim_set.run_sim_set(paths, run=fake_run, capacity_wait_s=-1)
    assert waits == [30, 30]


def test_cli_batch_set_accepts_wait_for_capacity_and_dry_run(
    tmp_path, monkeypatch, capsys
):
    from klayout_tools.cli import main

    runner = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=runner)
    paths = _write_set(tmp_path, 2)
    out = tmp_path / "out"
    code = main(
        [
            "sim",
            "--batch-set",
            *paths,
            "--dry-run",
            "--wait-for-capacity",
            "30",
            "-o",
            str(out),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code in (0, 4), payload
    assert payload["dry_run"] is True
    assert not runner.argvs_matching("launch")
    assert not out.exists()
