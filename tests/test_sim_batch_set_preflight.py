"""``klt sim --batch-set`` all-or-nothing ceiling preflight (issue #4972)."""

import json

import pytest

import klayout_tools.sim_batch as sb
from test_sim_batch import (
    _batch_request,
    _fake_report,
    _write_body,
    _write_set,
)
from test_sim_batch_set_forwarding import _patch_set_run

CORNERS = {"process": ["TYP"], "params": {"vdd": [1.6, 1.8, 2.0]}}


def _member(
    tmp_path, name, *, timeout_s=60, max_job_seconds=3600, check="enforce", **batch
):
    request = _batch_request(tmp_path)
    request["corners"] = CORNERS
    request["models"] = {"lib": "corner.lib"}
    (tmp_path / "corner.lib").write_text("* lib\n")
    request["options"] = {"timeout_s": timeout_s}
    request["batch"].update(
        job_ceiling_check=check, max_job_seconds=max_job_seconds, **batch
    )
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(request))
    return str(path)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)


def test_bad_member_blocks_every_submission_and_all_failures_named(
    tmp_path, monkeypatch
):
    from klayout_tools import sim_set
    from klayout_tools.sim import SimError

    _write_body(tmp_path)
    calls = []
    _patch_set_run(monkeypatch, lambda path, **kw: calls.append(path))
    paths = [
        _member(tmp_path, "ok"),
        _member(tmp_path, "bad1", timeout_s=100, max_job_seconds=150),
        _member(tmp_path, "bad2", timeout_s=100, max_job_seconds=150),
    ]

    with pytest.raises(SimError) as excinfo:
        sim_set.run_sim_set(
            paths,
            outdir=str(tmp_path / "out"),
            preflight=sim_set.preflight_batch_request,
        )

    assert calls == []
    assert excinfo.value.code == sim_set.SET_PREFLIGHT_CODE
    message = str(excinfo.value)
    assert "bad1.json" in message and "bad2.json" in message
    assert "ok.json" not in message
    assert message.count(sb.BATCH_JOB_CEILING_CODE) == 2
    assert not (tmp_path / "out" / sim_set.MANIFEST_FILENAME).exists()


def test_cli_override_is_applied_to_each_member_at_preflight(tmp_path, monkeypatch):
    from klayout_tools import sim_set

    _write_body(tmp_path)
    calls = []

    def fake_run(path, **kw):
        calls.append(path)
        return _fake_report()

    _patch_set_run(monkeypatch, fake_run)
    # The member's own max_units_per_job=3 x 100s = 300s exceeds the 250s
    # ceiling, but the serial bound is advisory (#4982): the cap is honored,
    # so preflight accepts it; the CLI override of 1 unit/job also fits.
    path = _member(
        tmp_path, "big", timeout_s=100, max_job_seconds=250, check="warn",
        max_units_per_job=3,
    )  # fmt: skip
    report = sim_set.run_sim_set(
        [path],
        outdir=str(tmp_path / "o2"),
        preflight=sim_set.preflight_batch_request,
        max_units_per_job=1,
    )
    assert report["status"] == "pass"
    assert calls == [path]


def test_all_valid_set_is_unchanged(tmp_path, monkeypatch):
    from klayout_tools import sim_set

    _write_body(tmp_path)
    _patch_set_run(monkeypatch, lambda path, **kw: _fake_report())
    paths = [_member(tmp_path, "a"), _member(tmp_path, "b")]

    report = sim_set.run_sim_set(
        paths, outdir=str(tmp_path / "out"), preflight=sim_set.preflight_batch_request
    )

    assert report["schema_version"] == 1
    assert report["submitted"] == 2


def test_cli_preflight_failure_exits_1(tmp_path, monkeypatch, capsys):
    from klayout_tools.cli import main

    _write_body(tmp_path)
    paths = [_member(tmp_path, "bad", timeout_s=100, max_job_seconds=150)]

    # Real run_sim: the preflight refuses before anything could be submitted.
    assert main(["sim", "--batch-set", *paths, "--format", "json"]) == 1
    assert "batch_set_preflight_failed" in capsys.readouterr().err


def test_injected_run_without_preflight_skips_it(tmp_path, monkeypatch):
    from klayout_tools import sim_set

    paths = _write_set(tmp_path, 1)
    report = sim_set.run_sim_set(
        paths, outdir=str(tmp_path / "out"), run=lambda p, **kw: _fake_report()
    )
    assert report["submitted"] == 1


def _deck_member(tmp_path, name, *, max_job_seconds=3600):
    """A ``decks[]`` member (no ``netlist``), in its own subdirectory."""
    from test_sim_decks import batch_deck_request, write_request

    sub = tmp_path / name
    sub.mkdir()
    request = batch_deck_request(sub)
    request["batch"].update(
        job_ceiling_check="enforce", max_job_seconds=max_job_seconds
    )
    return str(write_request(sub, request))


def test_decks_members_preflight_and_submit_in_a_set(tmp_path, monkeypatch):
    from klayout_tools import sim_set

    calls = []
    _patch_set_run(monkeypatch, lambda path, **kw: calls.append(path) or _fake_report())
    paths = [_deck_member(tmp_path, "d1"), _deck_member(tmp_path, "d2")]

    report = sim_set.run_sim_set(
        paths, outdir=str(tmp_path / "out"), preflight=sim_set.preflight_batch_request
    )

    assert report["submitted"] == 2
    assert calls == paths


def test_over_ceiling_deck_member_refused_by_name(tmp_path, monkeypatch):
    from klayout_tools import sim_set
    from klayout_tools.sim import SimError

    calls = []
    _patch_set_run(monkeypatch, lambda path, **kw: calls.append(path))
    paths = [
        _deck_member(tmp_path, "ok"),
        _deck_member(tmp_path, "big", max_job_seconds=1),
    ]

    with pytest.raises(SimError) as excinfo:
        sim_set.run_sim_set(
            paths,
            outdir=str(tmp_path / "out"),
            preflight=sim_set.preflight_batch_request,
        )

    assert calls == []
    assert excinfo.value.code == sim_set.SET_PREFLIGHT_CODE
    message = str(excinfo.value)
    assert "big" in message and sb.BATCH_JOB_CEILING_CODE in message
    assert "ok/request.json" not in message


def test_mixed_corner_and_deck_set_preflights_both(tmp_path, monkeypatch):
    from klayout_tools import sim_set

    _write_body(tmp_path)
    _patch_set_run(monkeypatch, lambda path, **kw: _fake_report())
    paths = [_member(tmp_path, "corner"), _deck_member(tmp_path, "deck")]

    report = sim_set.run_sim_set(
        paths, outdir=str(tmp_path / "out"), preflight=sim_set.preflight_batch_request
    )
    assert report["submitted"] == 2


def test_member_with_neither_netlist_nor_decks_is_structured_not_keyerror(tmp_path):
    from klayout_tools.sim import SimError
    from klayout_tools.sim_batch_plan import preflight_batch_request

    path = tmp_path / "odd.json"
    path.write_text(json.dumps({"commands": []}))
    with pytest.raises(SimError) as excinfo:
        preflight_batch_request(str(path))
    assert excinfo.value.code == "batch_set_unsupported_request"
