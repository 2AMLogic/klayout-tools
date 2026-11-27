"""Worker-environment capture on deck-list batch jobs (issue #5328)."""

from __future__ import annotations

from klayout_tools import sim_decks as sd
from test_sim_decks import (
    _FakeFleet,
    batch_deck_request,
    install_fake_batch,
    run_local,
)


class TestDeckWorkerCapture:
    def test_job_command_runs_the_worker_capture(self):
        request = {"models": {"pdk": "sky130A"}, "options": {}}
        cmd = sd._deck_batch_command("enforce", request, ["m.lib"])
        assert "worker_environment.json" in cmd
        assert "sky130A" in cmd

    def test_report_carries_worker_environment_absent_when_not_captured(
        self, tmp_path, monkeypatch
    ):
        fleet = install_fake_batch(
            monkeypatch, _FakeFleet({"state": "done", "exit_code": "0"})
        )
        report = run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        assert fleet.spec is not None
        assert "worker_environment.json" in fleet.spec.cmd
        remote = report["environment"]["remote"]
        assert remote["worker_environment"]["capture"] == "absent"
