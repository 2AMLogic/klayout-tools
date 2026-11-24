"""Staged-input provenance for `klt sim` (issue #5325): the `transform` name
on a rewritten staged file, and the caller-asserted `options.pdk_revision`.

Split out of ``test_sim.py`` (file-size ratchet); shares its staging helpers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools import sim
from test_sim import (
    _capture_decks,
    _install_fake_remote_transport,
    _staged_model_fixture,
    _write_request,
)

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def test_staged_entries_name_the_transform_iff_rewritten(tmp_path, monkeypatch):
    """Issue #5325: ``transform`` is present exactly when ``rewritten`` is."""
    src = tmp_path / "src"
    src.mkdir()
    _install_fake_remote_transport(monkeypatch)
    report = sim.run_sim(str(_write_request(src, _staged_model_fixture(src))))
    staged = report["environment"]["staged_model_inputs"]
    assert any(e["rewritten"] for e in staged)
    assert any(not e["rewritten"] for e in staged)
    for entry in staged:
        assert ("transform" in entry) is entry["rewritten"]
        if entry["rewritten"]:
            assert entry["transform"] == "include-path-rewrite"
    assert "pdk_revision_asserted" not in report["environment"]


def test_pdk_revision_is_echoed_as_caller_asserted(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    request = _staged_model_fixture(src)
    request["options"]["pdk_revision"] = "open_pdks@abc123"
    _install_fake_remote_transport(monkeypatch)
    report = sim.run_sim(str(_write_request(src, request)))
    assert report["environment"]["pdk_revision_asserted"] == {
        "value": "open_pdks@abc123",
        "scope": "caller-asserted",
    }


def _unstaged_pdk_revision_request(src: Path, backend: str) -> dict:
    """The staged-model fixture with staging (and its OSDI binaries) turned
    off, so `options.pdk_revision` is the only thing under test."""
    request = _staged_model_fixture(src)
    request["options"].pop("stage_model_inputs")
    request["options"].pop("osdi_preload")
    if backend != "remote":
        request.pop("remote")
        request["backend"] = backend
    return request


@pytest.mark.parametrize("backend", ["local", "remote"])
@pytest.mark.parametrize("bad", [7, "", "  ", ["x"]])
def test_pdk_revision_is_validated_without_staging(tmp_path, backend, bad):
    """Issue #5325: `options.pdk_revision` is validated next to its sibling
    `options.stage_model_inputs`, so a malformed value is a named exit-1
    error on every backend and with staging off -- not only on the staged
    off-host path that echoes it."""
    src = tmp_path / "src"
    src.mkdir()
    request = _unstaged_pdk_revision_request(src, backend)
    request["options"]["pdk_revision"] = bad
    with pytest.raises(sim.SimError, match="pdk_revision must be a non-empty string"):
        sim.run_sim(str(_write_request(src, request)))


def test_valid_pdk_revision_without_staging_is_accepted_and_not_reported(
    tmp_path, monkeypatch
):
    """Issue #5325: a *valid* value on a run that stages nothing is accepted
    and ignored -- no `pdk_revision_asserted` field, as `docs/cli/sim.md`'s
    option table states (the same no-op `options.stage_model_inputs` itself
    is on-host)."""
    src = tmp_path / "src"
    src.mkdir()
    request = _unstaged_pdk_revision_request(src, "local")
    request["options"]["pdk_revision"] = "open_pdks@abc123"
    _capture_decks(monkeypatch)
    report = sim.run_sim(str(_write_request(src, request)))
    assert "pdk_revision_asserted" not in report["environment"]


@pytest.mark.parametrize("bad", [7, "", "  ", ["x"]])
def test_pdk_revision_must_be_a_non_empty_string(tmp_path, monkeypatch, bad):
    src = tmp_path / "src"
    src.mkdir()
    request = _staged_model_fixture(src)
    request["options"]["pdk_revision"] = bad
    _install_fake_remote_transport(monkeypatch)
    with pytest.raises(sim.SimError, match="pdk_revision"):
        sim.run_sim(str(_write_request(src, request)))


@pytest.mark.parametrize(
    "staged, expect_diagnostic", [("1", False), ("0", True)], ids=["staged", "unstaged"]
)
def test_worker_capture_omits_unknown_pdk_diagnostic_when_models_staged(
    tmp_path, staged, expect_diagnostic
):
    """Issue #5325: run the worker capture program the way the runner does
    (``python3 -I -c``, no ``PDK_ROOT``, no ``models.pdk`` variant) and
    assert on the document it writes, so the behaviour is pinned rather than
    the program's source text."""
    from klayout_tools import sim_batch_contract_worker as w

    out = tmp_path / "worker_environment.json"
    env = {key: value for key, value in os.environ.items() if key != "PDK_ROOT"}
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            w._WORKER_CAPTURE_PROGRAM,
            str(out),
            "",
            "",
            staged,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(out.read_text())
    assert doc["pdk"]["models_staged"] is (staged == "1")
    assert doc["pdk"]["root"] is None and doc["pdk"]["variant"] is None
    assert (
        "pdk root or variant unknown on worker" in doc["diagnostics"]
    ) is expect_diagnostic


# --------------------------------------------------------------------------- #
# `options.pdk_revision` as an assertion, not an echo (issue #5593)
# --------------------------------------------------------------------------- #

_TOKEN = "c6d73a35f524070e85faff4a6a9eef49553ebc2b"


def _worker_block(
    *,
    capture="ok",
    stamp_sha256="a" * 64,
    stamp_text=f"open_pdks {_TOKEN}\n",
    models_staged=True,
):
    return {
        "capture": capture,
        "pdk": {
            "variant": "sky130A",
            "stamp_scheme": "sources-sha256",
            "stamp_sha256": stamp_sha256,
            "stamp_text": stamp_text,
            "models_staged": models_staged,
            "stamp_scope": "image" if models_staged else "executed",
        },
        "pdk_stamp_comparison": {"status": "not_comparable", "reason": "no"},
    }


def _comparison(value, remote):
    from klayout_tools import sim_provenance as sp

    return sp.pdk_revision_comparison_environment(
        remote, {"options": {"pdk_revision": value}}
    ).get("pdk_revision_comparison")


@pytest.mark.parametrize("value", ["a" * 64, "A" * 64, _TOKEN])
def test_pdk_revision_matching_stamp_or_token_is_a_match(value):
    comparison = _comparison(value, {"worker_environment": _worker_block()})
    assert comparison["status"] == "match"
    assert comparison["worker_stamp"]["revision_token"] == _TOKEN
    assert comparison["worker_stamp"]["stamp_scope"] == "image"


def test_pdk_revision_differing_from_stamp_is_an_advisory_mismatch():
    comparison = _comparison(
        "f6eeac7bf3e04b1c", {"worker_environment": _worker_block()}
    )
    assert comparison["status"] == "mismatch"
    assert comparison["worker_stamp"]["stamp_sha256"] == "a" * 64
    assert "does not fail the run" in comparison["warning"]
    assert "runner image's tree" in comparison["warning"]  # stamp_scope: image


def test_pdk_revision_without_readable_worker_stamp_is_not_comparable():
    comparison = _comparison(
        "abc123", {"worker_environment": _worker_block(stamp_sha256=None)}
    )
    assert comparison["status"] == "not_comparable"
    assert comparison["reason"] == "worker_stamp_unavailable"


@pytest.mark.parametrize(
    "remote",
    [
        {"worker_environment": {"capture": "absent", "pdk": None}},
        {"worker_environment": {"capture": "malformed", "pdk": None}},
    ],
    ids=["absent", "malformed"],
)
def test_pdk_revision_with_unusable_capture_is_not_comparable(remote):
    assert _comparison("abc123", remote) == {
        "status": "not_comparable",
        "reason": "worker_stamp_unavailable",
    }


@pytest.mark.parametrize(
    "remote",
    [
        {},
        {"worker_environment": None},
        {"fleet": [{"job_id": "j1"}, {"job_id": "j2"}]},
    ],
    ids=["empty-remote", "no-block", "fleet-without-blocks"],
)
def test_pdk_revision_without_any_worker_block_adds_no_field(remote):
    assert _comparison("abc123", remote) is None


def test_pdk_revision_without_asserted_option_adds_no_field():
    from klayout_tools import sim_provenance as sp

    assert (
        sp.pdk_revision_comparison_environment(
            {"worker_environment": _worker_block()}, {"options": {}}
        )
        == {}
    )


def test_pdk_revision_fleet_members_stay_individually_visible():
    remote = {
        "fleet": [
            {"job_id": "j1", "worker_environment": _worker_block()},
            {
                "job_id": "j2",
                "worker_environment": _worker_block(stamp_sha256="b" * 64),
            },
        ]
    }
    comparison = _comparison("a" * 64, remote)
    assert comparison["status"] == "mismatch"
    assert [m["status"] for m in comparison["fleet"]] == ["match", "mismatch"]
    assert comparison["fleet"][0]["job_id"] == "j1"


def test_pdk_revision_on_staged_batch_run_records_comparison_and_keeps_outcome(
    tmp_path, monkeypatch
):
    """Issue #5593 AC: a declared `options.pdk_revision` on a staged batch
    run whose (fake) worker stamp is readable records the comparison beside
    `environment.pdk_revision_asserted`, and the run's outcome is unchanged
    by it -- the same request without the option grades identically."""
    src = tmp_path / "src"
    src.mkdir()
    request = _staged_model_fixture(src)
    request.pop("remote")
    request["backend"] = "batch"
    # `models.pdk_root` is a submitting-host path refused for batch; the
    # install is reachable through $PDK_ROOT instead.
    request["models"] = {"pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice"}
    monkeypatch.setenv("PDK_ROOT", str(src / "pdk"))

    def fake_backend(**kwargs):
        return (
            [],
            "46",
            {
                "provider": "aws-batch-fleet",
                "job_id": "klt-sim-fake",
                "worker_environment": _worker_block(),
            },
        )

    monkeypatch.setitem(sim._BACKENDS, "batch", fake_backend)
    control = sim.run_sim(str(_write_request(src, request)), backend="batch")
    request["options"]["pdk_revision"] = _TOKEN
    reported = sim.run_sim(str(_write_request(src, request)), backend="batch")

    assert reported["status"] == control["status"]
    assert reported["corners"] == control["corners"]
    assert reported["environment"]["pdk_revision_asserted"] == {
        "value": _TOKEN,
        "scope": "caller-asserted",
    }
    comparison = reported["environment"]["pdk_revision_comparison"]
    assert comparison["status"] == "match"
    assert comparison["worker_stamp"]["revision_token"] == _TOKEN
    assert "pdk_revision_comparison" not in control["environment"]
