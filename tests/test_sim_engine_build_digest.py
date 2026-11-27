"""`engine_build_comparison.digest` on an off-host `klt sim` report (issue
#7959).

`worker_environment.engine_build_comparison.status` compares `--version`
banners (issue #3095) and keeps doing only that, but one banner covers many
builds: a worker can report `status: "match"` while having run a different
binary than the submitter's. Since #7356 put both digests in the report, the
comparison also carries `digest` whenever the worker's
`simulator.binary_sha256` and the submitter's
`client_engine_build.binary_sha256` are both valid, and omits it otherwise,
so a legacy capture's comparison is byte-identical to before.

Split from `test_sim_engine_build_host.py` (file-size ceiling, #2766) and
reuses its fakes: the end-to-end cases drive `sim.run_sim(...,
backend="batch")` against `test_sim_batch`'s fake command runner and a faked
worker capture. No fleet job is submitted and no real `ngspice` is probed.
"""

from __future__ import annotations

import pytest

from helpers.subprocess_fakes import fake_completed, uploaded_request_of
from klayout_tools import sim
from klayout_tools import sim_batch as sb
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
from test_sim_engine_build_host import (
    _CLIENT_BANNER,
    _CLIENT_BUILD,
    _WORKER_BANNER,
    _batch_report,
)

_CLIENT_DIGEST = _CLIENT_BUILD["binary_sha256"]
_WORKER_DIGEST = "a" * 64


# --------------------------------------------------------------------------- #
# End to end over a faked batch run
# --------------------------------------------------------------------------- #


def test_batch_report_reads_a_banner_match_alongside_a_digest_mismatch(
    tmp_path, monkeypatch
):
    """The reported case: the same `--version` banner on both hosts, different
    binaries. `status` stays the banner result; `digest` says which ran."""
    monkeypatch.setattr(sim, "_probe_engine_build", lambda _: dict(_CLIENT_BUILD))
    environment = _batch_report(
        tmp_path,
        monkeypatch,
        simulator={"version_banner": _CLIENT_BANNER, "binary_sha256": _WORKER_DIGEST},
    )["environment"]

    assert environment["engine_build"]["binary_sha256"] == _WORKER_DIGEST
    assert environment["client_engine_build"]["binary_sha256"] == _CLIENT_DIGEST
    assert environment["remote"]["worker_environment"]["engine_build_comparison"] == {
        "status": "match",
        "digest": "mismatch",
    }


def test_each_fleet_member_compares_its_own_digest(tmp_path, monkeypatch):
    """Two shards share a banner but ran different binaries: each `fleet[]`
    entry says whether *that* worker is the client's binary, where the
    fleet-wide `engine_build.binary_sha256` is `null` (the digests disagree)."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    def _shard_outputs(argv, timeout_s):
        """The job whose shard carries unit 0 ran the client's binary; the
        other, a different one. Both banners are the client's."""
        captured = uploaded_request_of(argv)
        if captured is not None:
            document, job_id = captured
            units = document["_explicit_points"]
            digest = (
                _CLIENT_DIGEST if units[0]["temperature_c"] == 0 else _WORKER_DIGEST
            )
            runner.outputs_by_job[job_id] = {
                "report.json": _sim_report(
                    corner_ids=tuple(
                        f"tt_1.800_{int(point['temperature_c'])}" for point in units
                    )
                ),
                "worker_environment.json": _capture(
                    simulator={
                        "version_banner": _CLIENT_BANNER,
                        "binary_sha256": digest,
                    }
                ),
            }
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _shard_outputs)
    monkeypatch.setattr(sim, "_probe_engine_build", lambda _: dict(_CLIENT_BUILD))
    _write_body(tmp_path)
    request = _batch_request(
        tmp_path,
        corners={"supply_v": {"vdd": [1.8]}, "temperature_c": [0, 1, 2, 3]},
        remote={"hosts": 2},
    )

    environment = sim.run_sim(str(_write_request(tmp_path, request)))["environment"]

    comparisons = {
        member["worker_environment"]["simulator"]["binary_sha256"]: member[
            "worker_environment"
        ]["engine_build_comparison"]
        for member in environment["remote"]["fleet"]
    }
    assert comparisons == {
        _CLIENT_DIGEST: {"status": "match", "digest": "match"},
        _WORKER_DIGEST: {"status": "match", "digest": "mismatch"},
    }
    assert environment["engine_build"]["binary_sha256"] is None
    assert environment["client_engine_build"]["binary_sha256"] == _CLIENT_DIGEST


# --------------------------------------------------------------------------- #
# `_compare_engine_builds` directly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("worker_banner", "client_banner", "banner_result"),
    [
        (_WORKER_BANNER, "ngspice-46 build A features: osdi", {"status": "match"}),
        (_WORKER_BANNER, "ngspice-46 build B (fftw)", {"status": "mismatch"}),
        (None, _WORKER_BANNER, {"status": "not_comparable"}),
        (" \n ", _WORKER_BANNER, {"status": "not_comparable"}),
        (_WORKER_BANNER, None, {"status": "not_comparable"}),
    ],
    ids=[
        "match",
        "mismatch",
        "no-worker-banner",
        "blank-worker-banner",
        "no-client-banner",
    ],
)
@pytest.mark.parametrize(
    ("worker_digest", "client_digest", "digest"),
    [("a" * 64, "A" * 64, "match"), ("a" * 64, "b" * 64, "mismatch")],
    ids=["same-binary-other-case", "different-binary"],
)
def test_engine_build_digest_is_independent_of_the_banner_result(
    worker_banner, client_banner, banner_result, worker_digest, client_digest, digest
):
    comparison = sb._compare_engine_builds(
        {"version_banner": worker_banner, "binary_sha256": worker_digest},
        {"version_banner": client_banner, "binary_sha256": client_digest},
    )

    if banner_result["status"] == "mismatch":
        assert comparison.pop("warning")  # the banner mismatch still warns
    assert comparison == {**banner_result, "digest": digest}


@pytest.mark.parametrize("side", ["worker", "client"])
@pytest.mark.parametrize(
    "unavailable",
    ["absent", None, "", "a" * 63, "a" * 65, "g" * 64, 123, "sha256:" + "a" * 64],
    ids=["absent", "null", "empty", "short", "long", "non-hex", "int", "prefixed"],
)
def test_engine_build_digest_is_omitted_without_two_valid_hashes(side, unavailable):
    """A legacy capture (no key), a failed hash (`null`) and a malformed value
    all leave the banner-only object, so their report shape is unchanged."""
    builds = {
        "worker": {"version_banner": _WORKER_BANNER, "binary_sha256": "a" * 64},
        "client": {"version_banner": _WORKER_BANNER, "binary_sha256": "a" * 64},
    }
    if unavailable == "absent":
        builds[side].pop("binary_sha256")
    else:
        builds[side]["binary_sha256"] = unavailable

    assert sb._compare_engine_builds(builds["worker"], builds["client"]) == {
        "status": "match"
    }
