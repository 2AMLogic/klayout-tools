"""Worker-environment capture on deck-list batch jobs (issue #5328)."""

import json
import os

import pytest

from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_pdk
from klayout_tools import sim_decks as sd
from test_sim_decks import (
    _FakeFleet,
    batch_deck_request,
    install_fake_batch,
    run_local,
)

#: A worker capture of the image's PDK, in the shape of a job whose decks
#: never read it (issue #7981): nothing was staged under
#: ``options.stage_model_inputs``, so ``models_staged`` is false.
_IMAGE_CAPTURE = {
    "schema": 1,
    "simulator": {
        "requested_binary": "ngspice",
        "binary_source": "PATH",
        "selected_path": "/opt/ngspice/bin/ngspice",
        "version_banner": "ngspice-47",
        "version_probe": "ok",
    },
    "pdk": {
        "variant": "sky130A",
        "root": "/opt/pdk",
        "stamp_scheme": "sources-sha256",
        "stamp_sha256": "c" * 64,
        "stamp_text": "open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b",
        "models_staged": False,
    },
    "cpu_model": "Fake CPU",
    "omp_num_threads": "1",
    "image_manifest": {"pdk_snapshot": "sky130A-2026.08.03"},
    "diagnostics": [],
}

#: A deck whose only file reference is the image's own PDK library: the
#: executing host resolves it, nothing is staged.
_IMAGE_DECK = (
    "* image deck\n.include $PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice\n"
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


class _CapturingFleet(_FakeFleet):
    """The fake fleet, plus a worker capture written beside the collected
    report, where the job command's preflight leaves it."""

    def __init__(self, status, capture):
        super().__init__(status)
        self.capture = capture

    def collect(self, config, job_id, local_dir, **kwargs):
        super().collect(config, job_id, local_dir, **kwargs)
        with open(
            os.path.join(local_dir, sb.BATCH_WORKER_ENVIRONMENT_FILENAME),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(self.capture, handle)


def _pinned_deck_request(tmp_path, body, **unit):
    """A one-unit batch request whose deck reads ``model.lib`` beside it and
    declares that library as an input."""
    (tmp_path / "pinned.spice").write_text(body, encoding="utf-8")
    (tmp_path / "model.lib").write_text(
        ".lib tt\n* pinned tt models\n.endl tt\n", encoding="utf-8"
    )
    return batch_deck_request(
        tmp_path,
        decks=[
            {"id": "pinned", "deck": "pinned.spice", "inputs": ["model.lib"], **unit}
        ],
    )


def _worker_pdk(report):
    return report["environment"]["remote"]["worker_environment"]["pdk"]


def _run_captured(tmp_path, monkeypatch, request, status=None):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    fleet = _CapturingFleet(
        status or {"state": "done", "exit_code": "0"}, _IMAGE_CAPTURE
    )
    install_fake_batch(monkeypatch, fleet)
    return run_local(tmp_path, request, outdir="out")


class TestDeckStampScope:
    """Issue #7981: a deck whose directives read one of its declared inputs
    executes client-supplied bytes, so the worker image's PDK stamp is not
    evidence about the model files that ran."""

    @pytest.mark.parametrize("directive", [".include model.lib", ".lib model.lib tt"])
    def test_declared_input_read_by_a_relative_directive_reports_image(
        self, tmp_path, monkeypatch, directive
    ):
        request = _pinned_deck_request(tmp_path, f"* pinned deck\n{directive}\n")
        _, worker_request = sd.stage_deck_job(request, request_dir=str(tmp_path))
        unit = worker_request["decks"][0]
        assert [ref["name"] for ref in unit["inputs"]] == ["model.lib"]
        assert [ref["name"] for ref in unit["includes"]] == ["model.lib"]
        assert unit["host_resolved"] == []

        report = _run_captured(tmp_path, monkeypatch, request)

        assert report["status"] == "pass"
        # The stamp still identifies the image; only its claim changes.
        assert _worker_pdk(report) == {
            **_IMAGE_CAPTURE["pdk"],
            "stamp_scope": "image",
            "install": None,
            "store_layout": None,
        }

    def test_one_qualifying_unit_anywhere_in_the_job_suffices(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "image.spice").write_text(_IMAGE_DECK, encoding="utf-8")
        (tmp_path / "pinned.spice").write_text(
            "* pinned deck\n.include model.lib\n", encoding="utf-8"
        )
        (tmp_path / "model.lib").write_text("* pinned\n", encoding="utf-8")
        request = batch_deck_request(
            tmp_path,
            decks=[
                {"id": "image", "deck": "image.spice"},
                {"id": "pinned", "deck": "pinned.spice", "inputs": ["model.lib"]},
            ],
        )
        _, worker_request = sd.stage_deck_job(request, request_dir=str(tmp_path))
        first, second = worker_request["decks"]
        assert first["includes"] == [] and first["host_resolved"]
        assert not second["host_resolved"]

        report = _run_captured(tmp_path, monkeypatch, request)

        assert _worker_pdk(report)["stamp_scope"] == "image"

    def test_unrelated_input_beside_a_host_resolved_reference_stays_executed(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "image.spice").write_text(_IMAGE_DECK, encoding="utf-8")
        (tmp_path / "extra.spice").write_text("* extra input\n", encoding="utf-8")
        request = batch_deck_request(
            tmp_path,
            decks=[{"id": "image", "deck": "image.spice", "inputs": ["extra.spice"]}],
        )
        _, worker_request = sd.stage_deck_job(request, request_dir=str(tmp_path))
        unit = worker_request["decks"][0]
        assert [ref["name"] for ref in unit["inputs"]] == ["extra.spice"]
        assert unit["includes"] == [] and unit["host_resolved"]

        report = _run_captured(tmp_path, monkeypatch, request)

        assert _worker_pdk(report) == {
            **_IMAGE_CAPTURE["pdk"],
            "stamp_scope": "executed",
            "install": None,
            "store_layout": None,
        }

    def test_failed_job_recovery_reports_the_same_scope(self, tmp_path, monkeypatch):
        request = _pinned_deck_request(
            tmp_path, "* stub-exit: 3\n.include model.lib\n", outputs=[]
        )
        report = _run_captured(
            tmp_path, monkeypatch, request, status={"state": "failed", "exit_code": "4"}
        )
        assert report["units"][0]["exit_code"] == 3
        assert _worker_pdk(report)["stamp_scope"] == "image"

    def test_absent_capture_still_leaves_no_pdk_block(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PDK_ROOT", raising=False)
        request = _pinned_deck_request(tmp_path, "* pinned deck\n.include model.lib\n")
        install_fake_batch(monkeypatch, _FakeFleet({"state": "done", "exit_code": "0"}))
        report = run_local(tmp_path, request, outdir="out")
        worker = report["environment"]["remote"]["worker_environment"]
        assert worker["capture"] == "absent"
        assert worker["pdk"] is None

    def test_destinations_are_compared_within_a_unit(self):
        request = {
            "decks": [
                {"inputs": [{"name": "model.lib"}], "includes": []},
                {"inputs": [], "includes": [{"name": "model.lib"}]},
            ]
        }
        assert sim_batch_pdk.decks_execute_declared_inputs(request) is False

    @pytest.mark.parametrize(
        "shape",
        [
            None,
            {},
            {"decks": "u0"},
            {"decks": ["u0", None]},
            {"decks": [{"inputs": "model.lib", "includes": {"a": 1}}]},
            {"decks": [{"inputs": [{"staged": "a"}], "includes": [{"staged": "b"}]}]},
            {
                "decks": [
                    {"inputs": [{"name": "extra.spice"}], "includes": [{"name": "m"}]}
                ]
            },
        ],
    )
    def test_other_request_shapes_never_qualify(self, shape):
        # The collector's reader must not raise: an odd request shape may
        # only leave the scope alone, never spoil a good capture.
        assert sim_batch_pdk.decks_execute_declared_inputs(shape) is False
