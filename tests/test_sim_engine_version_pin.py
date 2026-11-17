"""The `engine_version` pin's runner-side enforcement chain (issue #5853).

Split out of `tests/test_sim.py` / `tests/test_sim_batch.py` (both above
their `file-size-baseline.json` ceilings): the forwarding contract, the
runner-path pre-corner refusal, the submit-side fail-closed backstop, and
the per-unit `batch_job_failed`/`runner_code` surfacing. Helpers are
imported from the modules they were extracted from.
"""

from __future__ import annotations

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim, sim_remote
from test_sim import _fake_engine_probe, _pin_request
from test_sim_batch import _envelope, _FakeRunner, _run_failed_job, _status

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def test_build_remote_request_forwards_engine_version_and_ngspice_binary():
    """Issue #5853: the ``engine_version`` pin and the
    ``options.ngspice_binary`` override travel with the job, and the runner
    is forced onto the ``local-parallel`` backend whose own pre-corner pin
    check enforces the pin there; only the submit-side ``remote`` block (and
    the submitter's pool size) is stripped. The caller's document is copied,
    not mutated."""
    request = {
        "engine_version": "44",
        "remote": {"batch": {"profile": "p"}},
        "options": {"ngspice_binary": "./ngspice-44", "max_workers": 99},
    }
    kwargs = dict(timeout_s=10.0, keep_artifacts=False, want_waveforms=False)
    forwarded = sim_remote._build_remote_request(request, **kwargs)
    assert forwarded["engine_version"] == "44"
    assert forwarded["options"]["ngspice_binary"] == "./ngspice-44"
    assert forwarded["backend"] == "local-parallel"
    assert "remote" not in forwarded
    assert "max_workers" not in forwarded["options"]
    assert request["options"]["max_workers"] == 99
    assert "backend" not in request


def test_engine_version_unmet_fails_before_simulating_on_local_parallel(
    tmp_path, monkeypatch
):
    """Issue #5853: ``local-parallel`` -- the exact backend a remote/batch
    runner executes on the forwarded request -- enforces the pin before any
    corner runs there; the submit-side report check only backstops it."""
    sims = _fake_engine_probe(monkeypatch, "** ngspice-42 : Circuit level\n")
    request = _pin_request(tmp_path, engine_version="44")
    with pytest.raises(
        sim.SimError, match="requires ngspice 44; .*found ngspice 42"
    ) as exc:
        sim.run_sim(str(request), backend="local-parallel")
    assert exc.value.code == "engine_version_unmet"
    assert sims == []


def test_engine_version_pin_fails_closed_when_the_batch_runner_reports_none(
    tmp_path, monkeypatch
):
    """Issue #5853: a runner-side pin refusal fails the job before any
    corner, so no report -- and no ``environment.engine_version`` -- comes
    back; the submit-side off-host check fails closed on exactly that, so
    the submitter's own error is ``engine_version_unmet`` too. The per-unit
    ``batch_job_failed``/``runner_code`` detail is in the collected job
    outputs, not in this error envelope."""
    monkeypatch.setitem(sim._BACKENDS, "batch", lambda **kwargs: ([], None, None))
    request = _pin_request(tmp_path, engine_version="44")
    with pytest.raises(sim.SimError, match="reported no ngspice version") as exc:
        sim.run_sim(str(request), backend="batch")
    assert exc.value.code == "engine_version_unmet"


def test_failed_job_runner_side_pin_refusal_names_engine_version_unmet(
    tmp_path, monkeypatch
):
    """Issue #5853: when the runner's own ``klt sim`` refuses the forwarded
    ``engine_version`` pin before any corner simulates, the recovered klt
    envelope turns into per-unit ``batch_job_failed`` diagnostics whose
    ``runner_code`` names ``engine_version_unmet`` and whose message quotes
    the runner's reason -- and no engine version comes back, so the
    submitter's post-run pin check fails closed on its own."""
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="1", detail="job command exited 1")
    )
    runner.outputs = {
        "report.json": _envelope(
            "pinned simulator unavailable: request requires ngspice 44; "
            "the local engine probe found ngspice 42",
            code="engine_version_unmet",
        )
    }

    corners, engine_version, _ = _run_failed_job(
        tmp_path, monkeypatch, runner, points=2
    )

    assert engine_version is None
    for corner in corners:
        assert corner["status"] == "error"
        diagnostic = corner["diagnostics"][0]
        assert diagnostic["code"] == "batch_job_failed"
        assert diagnostic["runner_code"] == "engine_version_unmet"
        assert "the job's klt reported" in diagnostic["message"]
        assert "requires ngspice 44" in diagnostic["message"]
        assert "found ngspice 42" in diagnostic["message"]
