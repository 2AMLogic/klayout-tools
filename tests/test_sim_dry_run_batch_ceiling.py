"""``klt sim --dry-run`` runs the batch ceiling/split plan (issue #5674).

The single-request dry run must agree with the submit path's own
``preflight_batch_request`` on accept-vs-refuse (same ``error.code``, same
message), report the assessment additively as ``job_ceiling`` -- the
``environment.remote.job_ceiling`` name and shape -- without redefining
``hosts``, and keep every pre-existing dry-run posture (a configless host
dries-run clean; decks/command-list requests carry no corner ceiling).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools.cli import main
from klayout_tools.sim_batch_contract import JOB_CEILING_BASIS_CALLER
from klayout_tools.sim_batch_plan import preflight_batch_request

FAKE_BUCKET = "fake-batch-jobs-000000000000"

CORNERS = {"process": ["TYP"], "params": {"vdd": [1.6, 1.8, 2.0]}}


@pytest.fixture(autouse=True)
def _batch_env(monkeypatch):
    """No fleet config from this host: every batch knob comes from the
    request, so the ceiling arithmetic is deterministic."""
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    monkeypatch.delenv(sb.PROVISION_SCRIPT_ENV, raising=False)
    monkeypatch.delenv(sb.BUCKET_ENV, raising=False)
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)


@pytest.fixture(autouse=True)
def _reset_serial_bound_warned():
    import klayout_tools.sim_batch_warn as sbw

    sbw.SERIAL_BOUND_WARNED.clear()
    yield
    sbw.SERIAL_BOUND_WARNED.clear()


def _write_request(
    tmp_path: Path,
    *,
    timeout_s: float = 60,
    batch: dict | None = None,
    corners: dict | None = None,
    remote: dict | None = None,
    **extra,
) -> str:
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    (tmp_path / "corner.lib").write_text("* lib\n")
    (tmp_path / "prov.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (tmp_path / "prov.sh").chmod(0o755)
    request: dict = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "models": {"lib": "corner.lib"},
        "backend": "batch",
        "corners": corners or CORNERS,
        "options": {"timeout_s": timeout_s},
        "batch": {
            "bucket": FAKE_BUCKET,
            "region": "us-east-1",
            "provision_script_path": str(tmp_path / "prov.sh"),
            **(batch or {}),
        },
    }
    if remote is not None:
        request["remote"] = remote
    request.update(extra)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return str(path)


def _configless_request(tmp_path: Path) -> str:
    """backend batch, but nothing on this host can resolve a fleet config."""
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    (tmp_path / "corner.lib").write_text("* lib\n")
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "models": {"lib": "corner.lib"},
        "backend": "batch",
        "corners": CORNERS,
        "options": {"timeout_s": 60},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return str(path)


# --- refusals the submit path raises, the dry run now raises too ---------- #


def test_cap_with_enforce_is_refused_like_the_submit(tmp_path):
    path = _write_request(
        tmp_path, batch={"max_units_per_job": 2, "job_ceiling_check": "enforce"}
    )
    with pytest.raises(sim.SimError) as dry:
        sim.run_sim(path, backend="batch", dry_run=True)
    with pytest.raises(sim.SimError) as oracle:
        preflight_batch_request(path)
    assert dry.value.code == sb.BATCH_MAX_UNITS_CODE
    assert str(dry.value) == str(oracle.value)


@pytest.mark.parametrize("bad", [0, -1, "3", True, 1.5])
def test_malformed_cap_is_refused_like_the_submit(tmp_path, bad):
    path = _write_request(tmp_path, batch={"max_units_per_job": bad})
    with pytest.raises(sim.SimError) as dry:
        sim.run_sim(path, backend="batch", dry_run=True)
    assert dry.value.code == sb.BATCH_MAX_UNITS_CODE
    assert "positive integer" in str(dry.value)


def test_one_unit_over_ceiling_enforce_is_refused_like_the_submit(tmp_path):
    # 3 units x 400s + 120 = 1320s of bound, and a single unit alone
    # (400 + 120 = 520s) exceeds the 360s ceiling: no shard can fit.
    path = _write_request(
        tmp_path,
        timeout_s=400,
        batch={"max_job_seconds": 360, "job_ceiling_check": "enforce"},
    )
    with pytest.raises(sim.SimError) as dry:
        sim.run_sim(path, backend="batch", dry_run=True)
    with pytest.raises(sim.SimError) as oracle:
        preflight_batch_request(path)
    assert dry.value.code == sb.BATCH_JOB_CEILING_CODE
    assert str(dry.value) == str(oracle.value)


def test_split_mode_over_ceiling_is_refused_too(tmp_path):
    # "split" cannot help when a single unit's bound exceeds the ceiling.
    path = _write_request(
        tmp_path,
        timeout_s=400,
        batch={"max_job_seconds": 360, "job_ceiling_check": "split"},
    )
    with pytest.raises(sim.SimError) as dry:
        sim.run_sim(path, backend="batch", dry_run=True)
    assert dry.value.code == sb.BATCH_JOB_CEILING_CODE


def test_cli_enforce_refusal_exits_1_with_the_code(tmp_path, capsys):
    path = _write_request(
        tmp_path,
        timeout_s=400,
        batch={"max_job_seconds": 360, "job_ceiling_check": "enforce"},
    )
    code = main(["sim", path, "--backend", "batch", "--dry-run", "--format", "json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == sb.BATCH_JOB_CEILING_CODE


# --- the common case: a field, never a refusal ----------------------------- #


def test_in_ceiling_request_reports_the_assessment_and_stays_valid(tmp_path):
    path = _write_request(tmp_path)
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    ceiling = report["job_ceiling"]
    assert ceiling["exceeds_ceiling"] is False
    assert ceiling["basis"] == sb.JOB_CEILING_BASIS
    assert ceiling["unit_count"] == 3
    assert ceiling["check"] == "warn"
    assert "warning" not in ceiling
    # No split mode active: no split annotation, exactly as the submit's
    # environment.remote.job_ceiling.
    assert "split" not in ceiling
    assert "effective_hosts" not in ceiling
    # `hosts` keeps its meaning: the request's resolved remote.hosts.
    assert report["hosts"] == 1


def test_hosts_keeps_its_meaning_under_a_split(tmp_path):
    path = _write_request(tmp_path, batch={"max_units_per_job": 1})
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    assert report["hosts"] == 1  # the request's remote.hosts, not the plan
    assert report["job_ceiling"]["split"] is True
    assert report["job_ceiling"]["effective_hosts"] == 3


def test_ceiling_keys_match_the_documented_remote_shape(tmp_path):
    path = _write_request(
        tmp_path,
        batch={"max_job_seconds": 480},  # 3 x 120 + 120 = 480: equal
    )
    report = sim.run_sim(path, backend="batch", dry_run=True, timeout_s=120)
    ceiling = report["job_ceiling"]
    for key in (
        "basis",
        "unit_count",
        "per_unit_timeout_s",
        "overhead_s",
        "job_bound_s",
        "max_job_seconds",
        "max_job_seconds_source",
        "exceeds_ceiling",
        "check",
        "max_units_per_job",
        "recommended_hosts",
    ):
        assert key in ceiling
    # A bound *equal* to the ceiling does not exceed it.
    assert ceiling["job_bound_s"] == ceiling["max_job_seconds"]
    assert ceiling["exceeds_ceiling"] is False


def test_bound_equal_to_ceiling_is_not_exceeding(tmp_path):
    path = _write_request(tmp_path, timeout_s=120, batch={"max_job_seconds": 480})
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    assert report["job_ceiling"]["exceeds_ceiling"] is False


# --- request.batch.job_timeout_s (#5544): same agreement in the new mode ---- #
# A stated job bound replaces the whole serial assessment
# (`assess_caller_job_timeout`), so the dry run's `job_ceiling` has a second
# shape; the differential oracle must hold there too, not be assumed.


def test_caller_job_timeout_in_ceiling_matches_the_submit_assessment(tmp_path):
    # 90s stated vs the 3 x 60 + 120 = 300s the serial formula would give.
    path = _write_request(tmp_path, batch={"job_timeout_s": 90})
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    assert report["job_ceiling"] == preflight_batch_request(path)
    ceiling = report["job_ceiling"]
    assert ceiling["basis"] == JOB_CEILING_BASIS_CALLER
    assert ceiling["job_bound_s"] == 90.0
    assert ceiling["exceeds_ceiling"] is False
    assert report["hosts"] == 1


@pytest.mark.parametrize("check", ["enforce", "split"])
def test_caller_job_timeout_over_ceiling_is_refused_like_the_submit(tmp_path, check):
    # Splitting cannot help: every shard carries the same stated bound.
    path = _write_request(
        tmp_path,
        batch={
            "job_timeout_s": 500,
            "max_job_seconds": 360,
            "job_ceiling_check": check,
        },
    )
    with pytest.raises(sim.SimError) as dry:
        sim.run_sim(path, backend="batch", dry_run=True)
    with pytest.raises(sim.SimError) as oracle:
        preflight_batch_request(path)
    assert dry.value.code == sb.BATCH_JOB_CEILING_CODE
    assert str(dry.value) == str(oracle.value)
    assert "batch.job_timeout_s" in str(dry.value)


def test_caller_job_timeout_over_ceiling_warn_reports_like_the_submit(tmp_path, capsys):
    path = _write_request(
        tmp_path, batch={"job_timeout_s": 500, "max_job_seconds": 360}
    )
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    assert report["job_ceiling"] == preflight_batch_request(path)
    ceiling = report["job_ceiling"]
    assert ceiling["basis"] == JOB_CEILING_BASIS_CALLER
    assert ceiling["exceeds_ceiling"] is True
    assert ceiling["recommended_hosts"] is None
    assert "batch.job_timeout_s" in ceiling["warning"]
    # The caller-bound warning is the assessment's own text, printed once.
    err = capsys.readouterr().err
    assert err.count("klt sim: warning: batch.job_timeout_s 500s exceeds") == 1


# --- warn and #4982: proceed, but say so ----------------------------------- #


def test_warn_over_ceiling_reports_and_exits_0(tmp_path, capsys):
    # 3 units x 1300s + 120 = 4020s of bound against the 3600s ceiling.
    path = _write_request(tmp_path, timeout_s=1300, batch={"max_job_seconds": 3600})
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    ceiling = report["job_ceiling"]
    assert ceiling["exceeds_ceiling"] is True
    assert ceiling["recommended_hosts"] == 2
    err = capsys.readouterr().err
    assert err.count("klt sim: warning: batch job bound") == 1
    assert "whole-job ceiling of 3600" in ceiling["warning"]


def test_over_cap_honored_with_serial_bound_warning_once(tmp_path, capsys):
    # cap == unit_count (3), per_job = floor((240-120)/60) = 2: the cap is
    # the caller's call (#4982) -- one job of 3 over the 2-unit serial bound.
    path = _write_request(
        tmp_path, batch={"max_units_per_job": 3, "max_job_seconds": 240}
    )
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    err = capsys.readouterr().err
    assert err.count("serial upper bound") == 1
    ceiling = report["job_ceiling"]
    assert ceiling["split"] is False
    assert ceiling["effective_hosts"] == 1


def test_split_notice_and_plan_are_reported(tmp_path, capsys):
    path = _write_request(tmp_path, batch={"max_units_per_job": 1})
    report = sim.run_sim(path, backend="batch", dry_run=True)
    err = capsys.readouterr().err
    assert "splitting 3 units into 3 batch jobs" in err
    assert report["job_ceiling"]["effective_hosts"] == 3


# --- preserved postures ----------------------------------------------------- #


def test_configless_host_still_dry_runs_clean(tmp_path):
    path = _configless_request(tmp_path)
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    assert "job_ceiling" not in report


def test_decks_request_has_no_corner_ceiling(tmp_path):
    (tmp_path / "a.spice").write_text("tran 1n 1u\n")
    request = {"decks": [{"id": "a", "deck": "a.spice", "outputs": []}]}
    path = tmp_path / "decks.json"
    path.write_text(json.dumps(request))
    report = sim.run_sim(str(path), dry_run=True)
    assert report["mode"] == "decks"
    assert report["dry_run"] is True
    assert "job_ceiling" not in report


def test_monte_carlo_units_come_from_samples(tmp_path):
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    (tmp_path / "corner.lib").write_text("* lib\n")
    (tmp_path / "prov.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (tmp_path / "prov.sh").chmod(0o755)
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "models": {"lib": "corner.lib"},
        "backend": "batch",
        "monte_carlo": {"n": 5, "seed": 7, "vary": "both"},
        "options": {"timeout_s": 60},
        "batch": {
            "bucket": FAKE_BUCKET,
            "region": "us-east-1",
            "provision_script_path": str(tmp_path / "prov.sh"),
        },
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    report = sim.run_sim(str(path), backend="batch", dry_run=True)
    assert report["unit_count"] == 5
    assert report["job_ceiling"]["unit_count"] == 5


def test_cli_max_units_override_wins_over_request_cap(tmp_path, capsys):
    path = _write_request(
        tmp_path,
        batch={"max_units_per_job": 1, "max_job_seconds": 240},
    )
    report = sim.run_sim(path, backend="batch", dry_run=True, max_units_per_job=3)
    ceiling = report["job_ceiling"]
    assert ceiling["split"] is False
    assert ceiling["effective_hosts"] == 1
    assert "serial upper bound" in capsys.readouterr().err


# --- remote.hosts sizes the assessed job (issue #8011) -------------------- #
# Three corners at 1800 s: as one job the request is bound at 5520 s, but each
# shard of ceil(3 / hosts) units -- what that job's own job.json
# timeout_seconds carries -- is 1920 s (one unit) or 3720 s (two).


def _sharded(tmp_path: Path, hosts: int, ceiling: int = 3600, **batch) -> str:
    return _write_request(
        tmp_path,
        timeout_s=1800,
        batch={"max_job_seconds": ceiling, **batch},
        remote={"hosts": hosts},
    )


@pytest.mark.parametrize(
    ("declared", "override", "ceiling", "job_units", "bound", "exceeds"),
    [
        (3, None, 3600, 1, 1920.0, False),
        (2, None, 3600, 2, 3720.0, True),
        (1, None, 3600, 3, 5520.0, True),
        (2, None, 3720, 2, 3720.0, False),  # a bound equal to the ceiling passes
        (1, 3, 3600, 1, 1920.0, False),  # `--hosts` lifts a one-host request
        (3, 2, 3600, 2, 3720.0, True),  # ... and overflows a three-host one
    ],
)
def test_bound_describes_the_largest_shard_of_the_resolved_run(
    tmp_path, capsys, declared, override, ceiling, job_units, bound, exceeds
):
    """``declared`` is the request's ``remote.hosts`` and ``override`` a
    ``--hosts`` value: the assessed job is the largest shard of the run they
    resolve to, never the whole request as one job."""
    path = _sharded(tmp_path, declared, ceiling)
    report = sim.run_sim(path, backend="batch", dry_run=True, hosts=override)
    job_ceiling = report["job_ceiling"]
    assert job_ceiling["job_bound_s"] == bound
    assert job_ceiling["job_unit_count"] == job_units
    assert job_ceiling["exceeds_ceiling"] is exceeds
    # The request is still all three units; `hosts` and the plan count its jobs.
    assert job_ceiling["unit_count"] == report["unit_count"] == 3
    assert job_ceiling.get("effective_hosts", 1) == (override or declared)
    assert report["hosts"] == (override or declared)
    assert ("klt sim: warning: batch job bound" in capsys.readouterr().err) is exceeds
    if exceeds:
        assert job_ceiling["warning"].startswith(
            f"batch job bound {bound:g}s (options.timeout_s 1800s x {job_units} units"
        )
        assert job_ceiling["recommended_hosts"] == 3
        assert "Set request.remote.hosts to at least 3" in job_ceiling["warning"]
    # The --batch-set preflight assesses the same resolved run.
    assert job_ceiling == preflight_batch_request(path, hosts=override)


def test_enforce_passes_a_fitting_shard_and_refuses_an_overflowing_one(tmp_path):
    fitting = _sharded(tmp_path, 3, job_ceiling_check="enforce")
    report = sim.run_sim(fitting, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    assert report["job_ceiling"]["exceeds_ceiling"] is False

    overflowing = _sharded(tmp_path, 2, job_ceiling_check="enforce")
    with pytest.raises(sim.SimError) as dry:
        sim.run_sim(overflowing, backend="batch", dry_run=True)
    with pytest.raises(sim.SimError) as oracle:
        preflight_batch_request(overflowing)
    assert dry.value.code == sb.BATCH_JOB_CEILING_CODE
    assert str(dry.value) == str(oracle.value)
    assert "batch job bound 3720s" in str(dry.value)


# --- text and json surfaces ------------------------------------------------- #


def test_text_format_prints_the_shard_plan(tmp_path, capsys):
    path = _write_request(tmp_path, batch={"max_units_per_job": 1})
    code = main(["sim", path, "--backend", "batch", "--dry-run", "--format", "text"])
    assert code == 0
    out = capsys.readouterr().out
    plan = [line for line in out.splitlines() if "batch plan:" in line]
    assert plan == [
        "  batch plan: 3 unit(s) in 3 job(s) of at most 1 unit(s) "
        "each (bound 300s of the 3600s ceiling)"
    ]


def test_text_plan_counts_every_unit_of_a_remote_hosts_run(tmp_path, capsys):
    path = _sharded(tmp_path, 3)
    argv = ["sim", path, "--backend", "batch", "--dry-run", "--format", "text"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if "batch plan:" in line] == [
        "  batch plan: 3 unit(s) in 3 job(s) of at most 1 unit(s) "
        "each (bound 1920s of the 3600s ceiling)"
    ]


def test_text_format_has_no_plan_line_without_an_assessment(tmp_path, capsys):
    path = _configless_request(tmp_path)
    code = main(["sim", path, "--backend", "batch", "--dry-run", "--format", "text"])
    assert code == 0
    assert "batch plan:" not in capsys.readouterr().out


def test_json_format_carries_job_ceiling_additively(tmp_path, capsys):
    path = _write_request(tmp_path)
    code = main(["sim", path, "--backend", "batch", "--dry-run", "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    report = payload.get("data", payload)
    assert report["dry_run"] is True
    assert report["job_ceiling"]["exceeds_ceiling"] is False
    assert report["hosts"] == 1
