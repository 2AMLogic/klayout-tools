"""The corner request file is read and expanded once per entry point (#6878).

``run_sim`` and batch preflight each load the request once; that loaded
object is what corner preparation (``_prepare_sim_request``) now uses, so
the JSON parse and the ``options.measurement_cases`` template expansion
happen exactly once per operation -- and a caller replacing the file
between reads can no longer make preparation operate on a different
request from the one that selected the execution path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools.sim_batch_plan import preflight_batch_request

FAKE_BUCKET = "fake-batch-jobs-000000000000"

CORNERS = {"process": ["TYP"], "params": {"vdd": [1.6, 1.8, 2.0]}}


@pytest.fixture(autouse=True)
def _no_host_batch_env(monkeypatch):
    """No fleet config from this host: every batch knob comes from the
    request, so the preflight assessment is deterministic."""
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    monkeypatch.delenv(sb.PROVISION_SCRIPT_ENV, raising=False)
    monkeypatch.delenv(sb.BUCKET_ENV, raising=False)
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)


def _write_body(tmp_path: Path) -> None:
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )


def _write_request(tmp_path: Path, **extra) -> str:
    """A one-corner-grid request whose measurements are templated over two
    ``options.measurement_cases`` scopes, so ``expand_request`` does real
    work on every load."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _write_body(tmp_path)
    (tmp_path / "corner.lib").write_text("* lib\n")
    (tmp_path / "prov.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (tmp_path / "prov.sh").chmod(0o755)
    request: dict = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "models": {"lib": "corner.lib"},
        "corners": CORNERS,
        "options": {"timeout_s": 60, "measurement_cases": ["case0_", "case1_"]},
        "measurements": [
            {
                "name": "tpd_{case}",
                "spice": (
                    ".meas tran tpd_{case} TRIG v({case}in) VAL=0.5 RISE=1 "
                    "TARG v({case}out) VAL=0.5 RISE=1"
                ),
            }
        ],
        **extra,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return str(path)


class _LoadCounter:
    """Wrap ``sim.load_request``/``sim.expand_request`` with call counters,
    the instrumentation the issue's premise table was measured with."""

    def __init__(self, monkeypatch):
        self.loads = 0
        self.expansions = 0
        real_load = sim.load_request
        real_expand = sim.expand_request

        def counting_load(request_path):
            self.loads += 1
            return real_load(request_path)

        def counting_expand(request):
            self.expansions += 1
            return real_expand(request)

        monkeypatch.setattr(sim, "load_request", counting_load)
        monkeypatch.setattr(sim, "expand_request", counting_expand)


def test_local_corner_dry_run_reads_and_expands_once(tmp_path, monkeypatch):
    path = _write_request(tmp_path)
    counter = _LoadCounter(monkeypatch)
    report = sim.run_sim(path, dry_run=True)
    assert report["dry_run"] is True
    assert report["status"] == "valid"
    assert report["unit_count"] == 3
    # The relative `netlist` is still resolved against the request file's
    # own directory (`request_path` is kept for exactly this).
    assert report["netlist"] == str(tmp_path / "body.spice")
    # The expansion ran (2 cases templated) and dropped no unknown key.
    assert counter.loads == 1
    assert counter.expansions == 1
    assert report["warnings"] == []


def test_batch_dry_run_reads_and_expands_once(tmp_path, monkeypatch):
    path = _write_request(
        tmp_path,
        backend="batch",
        batch={
            "bucket": FAKE_BUCKET,
            "region": "us-east-1",
            "provision_script_path": str(tmp_path / "prov.sh"),
        },
    )
    counter = _LoadCounter(monkeypatch)
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    assert counter.loads == 1
    assert counter.expansions == 1


def _batch_extra(dir_path: Path) -> dict:
    return {
        "backend": "batch",
        "batch": {
            "bucket": FAKE_BUCKET,
            "region": "us-east-1",
            "provision_script_path": str(dir_path / "prov.sh"),
        },
    }


def test_batch_preflight_reads_and_expands_once_and_agrees_with_dry_run(
    tmp_path, monkeypatch
):
    dry_dir = tmp_path / "dry"
    preflight_dir = tmp_path / "preflight"
    dry = sim.run_sim(
        _write_request(dry_dir, **_batch_extra(dry_dir)),
        backend="batch",
        dry_run=True,
    )
    counter = _LoadCounter(monkeypatch)
    ceiling = preflight_batch_request(
        _write_request(preflight_dir, **_batch_extra(preflight_dir))
    )
    assert counter.loads == 1
    assert counter.expansions == 1
    assert ceiling == dry["job_ceiling"]


def test_file_replaced_after_load_cannot_change_the_run(tmp_path, monkeypatch):
    """The race the second read opened: a caller replacing the request file
    after the first load used to feed preparation a different document.
    The loaded request is now the one and only input to the sweep."""
    path = _write_request(tmp_path)
    real_load = sim.load_request

    def load_once_then_replace(request_path):
        request = real_load(request_path)
        # A gutted document: no analysis, no corners -- a second read of the
        # file would refuse it (or sweep a different grid).
        Path(request_path).write_text(json.dumps({"netlist": "body.spice"}))
        return request

    monkeypatch.setattr(sim, "load_request", load_once_then_replace)
    report = sim.run_sim(path, dry_run=True)
    assert report["status"] == "valid"
    assert report["unit_count"] == 3
    # The replacement really happened; the run simply never re-read it.
    assert json.loads(Path(path).read_text()) == {"netlist": "body.spice"}


def test_file_replaced_after_load_cannot_change_the_preflight(tmp_path, monkeypatch):
    path = _write_request(
        tmp_path,
        backend="batch",
        batch={
            "bucket": FAKE_BUCKET,
            "region": "us-east-1",
            "provision_script_path": str(tmp_path / "prov.sh"),
        },
    )
    real_load = sim.load_request

    def load_once_then_replace(request_path):
        request = real_load(request_path)
        Path(request_path).write_text(json.dumps({"netlist": "body.spice"}))
        return request

    monkeypatch.setattr(sim, "load_request", load_once_then_replace)
    ceiling = preflight_batch_request(path)
    assert ceiling["unit_count"] == 3
    assert ceiling["exceeds_ceiling"] is False
