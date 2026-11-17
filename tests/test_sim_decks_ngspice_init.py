"""`klt sim` deck path: `options.ngspice_init` (issue #5738).

Split out of `tests/test_sim_decks.py` when that file reached its per-file
size ceiling (scripts/check_file_size_baseline.py, issue #2766); the
materialize-per-unit, pre-run validation, staged-`.spiceinit` collision,
worker-request forwarding, and runner-feature-gate coverage live here. The
shared deck fixtures come from `test_sim_decks`.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools import sim_decks as sd
from test_sim_decks import deck_request, run_local


class TestNgspiceInit:
    """`options.ngspice_init` honoured on the deck path (issue #5738):
    materialized per unit, validated pre-run, forwarded off-host, and
    refused by name when a unit stages its own `.spiceinit`."""

    def test_local_run_writes_requested_lines_in_every_unit(self, tmp_path):
        request = deck_request(tmp_path)
        request["options"]["ngspice_init"] = ["set ngbehavior=hsa", "set numdgt=12"]
        report = run_local(tmp_path, request)
        assert report["status"] == "pass"
        for unit_id in ("tt_27c", "ss_125c"):
            assert (tmp_path / "out" / unit_id / ".spiceinit").read_text(
                encoding="utf-8"
            ) == "set ngbehavior=hsa\nset numdgt=12\n"

    def test_absent_option_writes_no_file(self, tmp_path):
        report = run_local(tmp_path, deck_request(tmp_path))
        assert report["status"] == "pass"
        assert not list((tmp_path / "out").rglob(".spiceinit"))

    def test_empty_array_is_treated_as_unset(self, tmp_path):
        request = deck_request(tmp_path)
        request["options"]["ngspice_init"] = []
        report = run_local(tmp_path, request)
        assert report["status"] == "pass"
        assert not list((tmp_path / "out").rglob(".spiceinit"))

    @pytest.mark.parametrize("bad", ["set ngbehavior=hsa", 42, [42], [""], [None]])
    def test_malformed_values_rejected_before_anything_runs(self, tmp_path, bad):
        request = deck_request(tmp_path)
        request["options"]["ngspice_init"] = bad
        with pytest.raises(sim.SimError, match="ngspice_init"):
            run_local(tmp_path, request)
        assert not (tmp_path / "out").exists()

    def test_staged_spiceinit_input_colliding_with_option_refused(self, tmp_path):
        (tmp_path / "spiceinit.in").write_text("set ngbehavior=hsa\n", encoding="utf-8")
        request = deck_request(tmp_path)
        request["decks"][0]["inputs"].append(
            {"path": "spiceinit.in", "name": ".spiceinit"}
        )
        request["options"]["ngspice_init"] = ["set ngbehavior=hsa"]
        with pytest.raises(sim.SimError, match=r"'.spiceinit'"):
            run_local(tmp_path, request)
        assert not (tmp_path / "out").exists()

    def test_staged_spiceinit_refused_before_offhost_staging(self, tmp_path):
        (tmp_path / "spiceinit.in").write_text("set ngbehavior=hsa\n", encoding="utf-8")
        request = deck_request(tmp_path)
        request["decks"][0]["inputs"].append(
            {"path": "spiceinit.in", "name": ".spiceinit"}
        )
        request["options"]["ngspice_init"] = ["set ngbehavior=hsa"]
        with pytest.raises(sim.SimError, match="options.ngspice_init"):
            sd.stage_deck_job(request, request_dir=str(tmp_path))

    def test_staged_spiceinit_still_works_without_the_option(self, tmp_path):
        # The documented pre-#5738 workaround keeps working when the option
        # is unset: the staged file lands where the engine reads it.
        (tmp_path / "spiceinit.in").write_text("set ngbehavior=hsa\n", encoding="utf-8")
        request = deck_request(tmp_path, decks=[{"id": "u0", "deck": "deck.spice"}])
        request["decks"][0]["inputs"] = [{"path": "spiceinit.in", "name": ".spiceinit"}]
        report = run_local(tmp_path, request)
        assert report["status"] == "pass"
        assert (tmp_path / "out" / "u0" / ".spiceinit").read_text(
            encoding="utf-8"
        ) == "set ngbehavior=hsa\n"

    def test_local_parallel_each_unit_gets_its_own_file(self, tmp_path):
        request = deck_request(tmp_path)
        request["backend"] = "local-parallel"
        request["options"]["max_workers"] = 2
        request["options"]["ngspice_init"] = ["set ngbehavior=hsa"]
        report = run_local(tmp_path, request)
        assert report["status"] == "pass"
        for unit_id in ("tt_27c", "ss_125c"):
            assert (tmp_path / "out" / unit_id / ".spiceinit").read_text(
                encoding="utf-8"
            ) == "set ngbehavior=hsa\n"

    def test_forwarded_in_worker_request(self, tmp_path):
        request = deck_request(tmp_path)
        request["options"]["ngspice_init"] = ["set ngbehavior=hsa"]
        _, worker_request = sd.stage_deck_job(request, request_dir=str(tmp_path))
        assert worker_request["options"]["ngspice_init"] == ["set ngbehavior=hsa"]

    def test_not_forwarded_when_unset(self, tmp_path):
        _, worker_request = sd.stage_deck_job(
            deck_request(tmp_path), request_dir=str(tmp_path)
        )
        assert "ngspice_init" not in worker_request["options"]

    def test_runner_feature_gate_fires_on_forwarded_worker_request(
        self, tmp_path, monkeypatch
    ):
        # Once the option ships in the worker request (the document the
        # batch collector hands _read_job_identity), the generic
        # min-runner registry entry (options.ngspice_init, 0.7.0) gates a
        # deck job too -- it never fired before issue #5738.
        monkeypatch.setattr(sb._provenance, "_klt_version", lambda: "0.9.0")
        (tmp_path / sb.BATCH_RUNNER_IDENTITY_FILENAME).write_text(
            json.dumps({"runner_klt_version": "0.6.0"}), encoding="utf-8"
        )
        request = deck_request(tmp_path)
        request["options"]["ngspice_init"] = ["set ngbehavior=hsa"]
        _, worker_request = sd.stage_deck_job(request, request_dir=str(tmp_path))
        fields = sb._read_runner_identity(str(tmp_path), worker_request)
        features = fields["runner_request_features"]
        assert "options.ngspice_init" in features["required"]
        assert "options.ngspice_init" in features["missing"]
        assert features["status"] == "unsupported"

    def test_runner_feature_gate_passes_on_supporting_runner(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sb._provenance, "_klt_version", lambda: "0.9.0")
        (tmp_path / sb.BATCH_RUNNER_IDENTITY_FILENAME).write_text(
            json.dumps({"runner_klt_version": "0.8.0"}), encoding="utf-8"
        )
        request = deck_request(tmp_path)
        request["options"]["ngspice_init"] = ["set ngbehavior=hsa"]
        _, worker_request = sd.stage_deck_job(request, request_dir=str(tmp_path))
        fields = sb._read_runner_identity(str(tmp_path), worker_request)
        assert "options.ngspice_init" in fields["runner_request_features"]["required"]
        assert fields["runner_request_features"]["missing"] == []
