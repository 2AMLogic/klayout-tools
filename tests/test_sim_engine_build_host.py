"""`environment.engine_build` on an off-host `klt sim` report (issue #5566).

Split out of `tests/test_sim.py` / `tests/test_sim_batch.py` (size ratchet,
#2766). Before this fix an `engine_host: "worker"` report paired a
worker-sourced `environment.engine_version` with the *submitting client's*
`engine_build` banner, so one report could name two different ngspice
versions as "the engine that ran this" -- the gap #3110 left when it made
`ngspice_binary` the worker's.

Every test here is offline: the batch end-to-end case drives
`sim.run_sim(..., backend="batch")` against `test_sim_batch`'s fake command
runner and a faked worker capture, and the rest call `apply_engine_host`
directly. No fleet job is submitted and no real `ngspice` is probed.
"""

from __future__ import annotations

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from test_sim_batch import (
    _batch_request,
    _capture,
    _FakeRunner,
    _install_fake_batch_transport,
    _sim_report,
    _status,
    _write_body,
    _write_request,
)

# The submitting client's own `ngspice --version`: a *different* major from
# the worker's, which is the only case where the distinction is visible.
_CLIENT_BANNER = "ngspice-47 : client build\nfeatures: fftw"
# What `_capture()` reports as the worker's banner.
_WORKER_BANNER = "ngspice-46 build A\nfeatures: osdi"

_CLIENT_BUILD = {
    "version_banner": _CLIENT_BANNER,
    "binary_sha256": "d" * 64,
    "probe": "ok",
    "reason": None,
}


# --------------------------------------------------------------------------- #
# End to end over a faked batch run
# --------------------------------------------------------------------------- #


def _pin_client_probe(monkeypatch, banner=_CLIENT_BANNER):
    """Pin the submitter's own `--version` banner; everything else that calls
    `subprocess.run` in `sim` is left alone."""
    inner = sim.subprocess.run

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return fake_completed(banner)
        return inner(cmd, **kwargs)

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _batch_report(tmp_path, monkeypatch, **capture_overrides):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        # The runner's own report: `engine_version` 46, the worker's.
        "report.json": _sim_report(),
        "worker_environment.json": _capture(**capture_overrides),
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    _pin_client_probe(monkeypatch)
    return sim.run_sim(str(_write_request(tmp_path, _batch_request(tmp_path))))


def test_batch_engine_build_is_the_workers_and_the_client_probe_is_kept(
    tmp_path, monkeypatch
):
    """The whole defect in one report: client ngspice-47, runner report 46,
    worker capture's banner 46."""
    environment = _batch_report(tmp_path, monkeypatch)["environment"]

    assert environment["engine_host"] == "worker"
    assert environment["engine_version"] == "46"  # already the worker's
    assert environment["engine_build"] == {
        "version_banner": _WORKER_BANNER,
        # The worker capture carries no digest, and the client's would
        # describe the wrong machine.
        "binary_sha256": None,
        "probe": "ok",
        "reason": None,
    }
    # The submitter's probe is kept verbatim, like `client_ngspice_binary`.
    client = environment["client_engine_build"]
    assert client["version_banner"] == _CLIENT_BANNER
    assert client["probe"] == "ok"
    # ... and the comparison still contrasts the two, unchanged in value.
    comparison = environment["remote"]["worker_environment"]["engine_build_comparison"]
    assert comparison["status"] == "mismatch" and comparison["warning"]


def test_batch_matching_builds_still_compare_match(tmp_path, monkeypatch):
    """A client whose banner equals the worker's: `engine_build` is the
    worker's either way, and the comparison says `match`."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(),
        "worker_environment.json": _capture(),
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    _pin_client_probe(monkeypatch, _WORKER_BANNER)

    environment = sim.run_sim(str(_write_request(tmp_path, _batch_request(tmp_path))))[
        "environment"
    ]

    assert environment["engine_build"]["version_banner"] == _WORKER_BANNER
    assert environment["client_engine_build"]["version_banner"] == _WORKER_BANNER
    assert environment["remote"]["worker_environment"]["engine_build_comparison"] == {
        "status": "match"
    }


# --------------------------------------------------------------------------- #
# `apply_engine_host` directly
# --------------------------------------------------------------------------- #


def _banner_block(capture, banner=None, probe=None):
    simulator = (
        {"version_banner": banner, "version_probe": probe} if capture == "ok" else None
    )
    return {"worker_environment": {"capture": capture, "simulator": simulator}}


def _offhost(remote, *, backend="batch", client_build=_CLIENT_BUILD):
    environment = {
        "ngspice_binary": "/home/me/ngspice",
        "engine_build": dict(client_build) if client_build else client_build,
        "remote": remote,
    }
    sim.apply_engine_host(
        environment, backend=backend, client_ngspice_binary="/home/me/ngspice"
    )
    return environment


def test_worker_banner_wins_and_carries_no_client_digest():
    environment = _offhost(_banner_block("ok", _WORKER_BANNER, "ok"))
    assert environment["engine_build"] == {
        "version_banner": _WORKER_BANNER,
        "binary_sha256": None,
        "probe": "ok",
        "reason": None,
    }
    assert environment["client_engine_build"] == _CLIENT_BUILD


@pytest.mark.parametrize(
    "remote",
    [_banner_block("absent"), _banner_block("malformed"), {}, None, "nonsense", []],
)
def test_without_a_worker_capture_the_banner_is_null_not_the_clients(remote):
    build = _offhost(remote)["engine_build"]
    assert build["version_banner"] is None and build["binary_sha256"] is None
    assert build["probe"] == "absent" and build["reason"]


def test_ssh_remote_backend_reports_null_rather_than_the_clients():
    """The SSH `remote` backend writes no worker capture at all (#3110)."""
    environment = _offhost(None, backend="remote")
    assert environment["engine_build"]["version_banner"] is None
    assert environment["client_engine_build"] == _CLIENT_BUILD


@pytest.mark.parametrize("probe", ["timeout", "error", "exit_1", "unavailable"])
def test_a_failed_worker_probe_carries_the_workers_own_code(probe):
    build = _offhost(_banner_block("ok", None, probe))["engine_build"]
    assert build["version_banner"] is None
    assert build["probe"] == probe and probe in build["reason"]


def test_a_blank_banner_claiming_ok_is_reported_as_error():
    """`probe: "ok"` with no banner is the case the client-side probe calls
    `"error"` -- never `"ok"` next to a null banner."""
    build = _offhost(_banner_block("ok", "   \n ", "ok"))["engine_build"]
    assert build["version_banner"] is None
    assert build["probe"] == "error" and build["reason"]


def test_sharded_fleet_agreement_and_disagreement():
    agree = {"fleet": [_banner_block("ok", _WORKER_BANNER, "ok")] * 2}
    mixed = {
        "fleet": [
            _banner_block("ok", _WORKER_BANNER, "ok"),
            _banner_block("ok", "ngspice-45 other", "ok"),
        ]
    }
    partial = {
        "fleet": [_banner_block("ok", _WORKER_BANNER, "ok"), _banner_block("absent")]
    }
    for remote, expected in ((agree, _WORKER_BANNER), (mixed, None), (partial, None)):
        build = _offhost(remote)["engine_build"]
        assert build["version_banner"] == expected
        assert build["probe"] == ("ok" if expected else "disagreement")


def test_whitespace_only_difference_between_shards_still_agrees():
    remote = {
        "fleet": [
            _banner_block("ok", _WORKER_BANNER, "ok"),
            _banner_block("ok", f"  {_WORKER_BANNER}\n", "ok"),
        ]
    }
    assert _offhost(remote)["engine_build"]["version_banner"] == _WORKER_BANNER


def test_xyce_keeps_a_null_build_and_gains_no_worker_banner():
    environment = _offhost(_banner_block("ok", _WORKER_BANNER, "ok"), client_build=None)
    assert environment["engine_build"] is None
    assert environment["client_engine_build"] is None


def test_a_local_report_is_byte_identical_to_before():
    environment = {
        "ngspice_binary": "/usr/bin/ngspice",
        "engine_build": dict(_CLIENT_BUILD),
    }
    sim.apply_engine_host(
        environment, backend="local", client_ngspice_binary="/usr/bin/ngspice"
    )
    assert environment == {
        "ngspice_binary": "/usr/bin/ngspice",
        "engine_build": _CLIENT_BUILD,
        "engine_host": "local",
    }


def test_a_report_without_engine_build_gains_neither_key():
    """Deck-list (`units[]`) reports carry no `engine_build` at all."""
    environment = {
        "ngspice_binary": "/home/me/ngspice",
        "remote": _banner_block("ok", _WORKER_BANNER, "ok"),
    }
    sim.apply_engine_host(
        environment, backend="batch", client_ngspice_binary="/home/me/ngspice"
    )
    assert "engine_build" not in environment
    assert "client_engine_build" not in environment
    assert environment["ngspice_binary"] is None  # #3110 behaviour unchanged
