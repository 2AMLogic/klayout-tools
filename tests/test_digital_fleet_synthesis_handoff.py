"""The generated fleet descriptor must route this invocation's actual netlist."""

import json
import re
from pathlib import Path

import pytest

from klayout_tools import digital_fleet, synthesize
from klayout_tools import eval as evaluate
from test_digital_fleet import _base_candidate
from test_synthesize import _setup_success_env, _stub_yosys_success

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def test_fleet_handoff_uses_one_current_isolated_synthesis(tmp_path, monkeypatch):
    _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch, hdl_toplevel="top")
    jobs = [
        digital_fleet.build_digital_job_description(_base_candidate(tmp_path))
        for _ in range(2)
    ]
    observed = []
    actual_synthesize = synthesize.run_synthesize

    def record_synthesis(request):
        report = actual_synthesize(request)
        observed.append(report)
        return report

    def route(request):
        document = json.loads(Path(request).read_text())
        netlist = tmp_path / document["netlist"]
        assert netlist.is_file()  # The synthesis gate must already have run.
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", observed[-1]["run_id"])
        assert netlist.parent.name == observed[-1]["run_id"]
        return {"stage_reached": "route", "die_area_um2": 100.0}

    monkeypatch.setattr(evaluate, "run_synthesize", record_synthesis)
    monkeypatch.setattr(evaluate, "run_place_and_route", route)
    for job in jobs:
        for item in job.inputs:
            if item.content is not None:
                (tmp_path / item.remote_name).write_text(item.content)
        report = evaluate.run_eval(str(tmp_path / "eval_descriptor.json"))
        assert report["valid"]
    assert len(observed) == 2  # Each job's gate and metrics reuse one eval cache entry.
    assert observed[0]["run_id"] != observed[1]["run_id"]
    assert observed[0]["netlist_path"] == {"path": None, "scope": "external"}
