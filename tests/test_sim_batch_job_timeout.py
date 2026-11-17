"""Caller-stated whole-job wall bound: ``request.batch.job_timeout_s``
(issue #5544, ``src/klayout_tools/sim_batch_job_timeout.py``).

The worked shape throughout is 8 units at ``options.timeout_s: 1500`` --
serial bound ``1500 x 8 + 120 = 12120 s``, far over the 3600 s default
ceiling -- with ``batch.job_timeout_s: 3400``. The same fake-transport
discipline as ``tests/test_sim_batch.py`` applies: no test here ever
invokes the real ``aws`` CLI, touches S3, or makes a network call.
Uploaded ``job.json`` documents are read **in flight** (their staging
tempdir is gone once ``submit_job`` returns).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch as sb
from klayout_tools.sim import SimError
from klayout_tools.sim_batch_contract import JOB_CEILING_BASIS_CALLER
from test_sim_batch import (
    _ceiling_request,
    _corner_points,
    _FakeRunner,
    _install_fake_batch_transport,
    _measurements_spec,
    _sim_report,
    _split_runner,
    _status,
    _write_body,
)

UNITS = 8
PER_UNIT_TIMEOUT_S = 1500.0
SERIAL_BOUND_S = PER_UNIT_TIMEOUT_S * UNITS + 120.0  # 12120, over the ceiling
STATED_BOUND_S = 3400.0  # the worked shape: an honest wall for 8 units
OVER_CEILING_BOUND_S = 4000.0
CEILING_S = 3600.0


def _caller_config(**overrides) -> SimpleNamespace:
    """``BatchConfig``-shaped namespace (as ``test_sim_batch_max_units_warning.py``)."""
    fields = dict(
        max_job_seconds=CEILING_S,
        max_job_seconds_source="default",
        job_ceiling_check="warn",
        max_units_per_job=None,
        job_timeout_s=STATED_BOUND_S,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _capture_uploaded_job_jsons(monkeypatch, runner: _FakeRunner) -> list[dict]:
    """Record every uploaded ``job.json``, in flight (the staging tempdir is
    deleted when ``submit_job`` returns). Chains onto whatever
    ``sb._run_subprocess`` is already patched to -- install a fake transport
    first (``_install_fake_batch_transport`` or ``_split_runner``), never the
    real subprocess default."""
    documents: list[dict] = []
    inner = sb._run_subprocess

    def _capture(argv, timeout_s):
        argv = list(argv)
        if "cp" in argv and "--recursive" not in argv:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            if destination.endswith("/job.json"):
                documents.append(json.loads(Path(source).read_text()))
        return inner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture)
    monkeypatch.setattr(sb.time, "sleep", runner.sleep)
    return documents


def _run_stated(
    tmp_path, monkeypatch, runner: _FakeRunner, *, units: int, **batch
) -> tuple[list[dict], dict | None, list[dict]]:
    """Drive the ``batch`` entry point with the stated bound in the request;
    returns (corners, environment, uploaded job.json documents)."""
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    _install_fake_batch_transport(monkeypatch, runner=runner)
    documents = _capture_uploaded_job_jsons(monkeypatch, runner)
    corners, _engine, environment = sb._run_batch(
        corner_points=_corner_points(units),
        netlist_path=str(_write_body(tmp_path)),
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=_measurements_spec(),
        timeout_s=PER_UNIT_TIMEOUT_S,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        max_workers=None,
        request=_ceiling_request(tmp_path, **batch),
    )
    return corners, environment, documents


# --------------------------------------------------------------------------- #
# Validation (resolve_job_timeout_s)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [0, -1, -3400.0, float("nan"), float("inf"), float("-inf"), "3400", [3400]],
)
def test_invalid_job_timeout_s_is_rejected(tmp_path, raw):
    """The same-shaped SimError as the other ``batch.*`` resolvers, before
    any S3 write."""
    with pytest.raises(SimError) as excinfo:
        sb._resolve_batch_config(
            _ceiling_request(tmp_path, job_timeout_s=raw),
            corner_count=UNITS,
            timeout_s=PER_UNIT_TIMEOUT_S,
            check_runner=False,
        )
    assert "request.batch.job_timeout_s" in str(excinfo.value)
    assert getattr(excinfo.value, "code", None) is None


@pytest.mark.parametrize("raw", [3400, 3400.0, 3.4e3])
def test_valid_job_timeout_s_resolves_to_float(tmp_path, raw):
    config = sb._resolve_batch_config(
        _ceiling_request(tmp_path, job_timeout_s=raw),
        corner_count=UNITS,
        timeout_s=PER_UNIT_TIMEOUT_S,
        check_runner=False,
    )
    assert config.job_timeout_s == 3400.0


def test_absent_job_timeout_s_resolves_to_none(tmp_path):
    config = sb._resolve_batch_config(
        _ceiling_request(tmp_path),
        corner_count=UNITS,
        timeout_s=PER_UNIT_TIMEOUT_S,
        check_runner=False,
    )
    assert config.job_timeout_s is None


# --------------------------------------------------------------------------- #
# The worked shape: fits the ceiling
# --------------------------------------------------------------------------- #


def test_job_ceiling_fields_when_the_bound_fits():
    assessment = sb._assess_job_ceiling(
        unit_count=UNITS, per_unit_timeout_s=PER_UNIT_TIMEOUT_S, config=_caller_config()
    )
    assert assessment == {
        "basis": JOB_CEILING_BASIS_CALLER,
        "unit_count": UNITS,
        "per_unit_timeout_s": PER_UNIT_TIMEOUT_S,
        "overhead_s": 0.0,
        "job_bound_s": STATED_BOUND_S,
        "max_job_seconds": CEILING_S,
        "max_job_seconds_source": "default",
        "exceeds_ceiling": False,
        "check": "warn",
        "max_units_per_job": UNITS,
        "recommended_hosts": 1,
    }


def test_job_ceiling_fields_when_the_bound_exceeds():
    assessment = sb._assess_job_ceiling(
        unit_count=UNITS,
        per_unit_timeout_s=PER_UNIT_TIMEOUT_S,
        config=_caller_config(job_timeout_s=OVER_CEILING_BOUND_S),
    )
    assert assessment["basis"] == JOB_CEILING_BASIS_CALLER
    assert assessment["job_bound_s"] == OVER_CEILING_BOUND_S
    assert assessment["overhead_s"] == 0.0
    assert assessment["exceeds_ceiling"] is True
    assert assessment["max_units_per_job"] == 0
    assert assessment["recommended_hosts"] is None
    warning = assessment["warning"]
    assert "batch.job_timeout_s" in warning
    assert "options.timeout_s" not in warning
    assert "remote.hosts" not in warning


def test_bound_equal_to_the_ceiling_does_not_exceed_it():
    """The existing 'equal does not exceed' rule carries over."""
    assessment = sb._assess_job_ceiling(
        unit_count=UNITS,
        per_unit_timeout_s=PER_UNIT_TIMEOUT_S,
        config=_caller_config(job_timeout_s=CEILING_S),
    )
    assert assessment["exceeds_ceiling"] is False
    assert assessment["max_units_per_job"] == UNITS


@pytest.mark.parametrize("check", ["warn", "enforce", "split"])
def test_worked_shape_is_one_job_with_the_stated_timeout(
    tmp_path, monkeypatch, capsys, check
):
    """8 units, ``options.timeout_s: 1500``, ``batch.job_timeout_s: 3400``:
    one job whose ``job.json`` carries ``timeout_seconds: 3400``, with no
    ceiling warning and no split notice -- under all three check modes."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(
            corner_ids=tuple(f"tt_1.800_{i}" for i in range(UNITS))
        )
    }
    corners, environment, documents = _run_stated(
        tmp_path,
        monkeypatch,
        runner,
        units=UNITS,
        job_timeout_s=STATED_BOUND_S,
        job_ceiling_check=check,
    )

    assert len(runner.argvs_matching("launch", "--apply")) == 1
    assert [document["timeout_seconds"] for document in documents] == [3400]
    stderr = capsys.readouterr().err
    assert "warning" not in stderr
    assert "splitting" not in stderr
    assert len(corners) == UNITS
    ceiling = environment["job_ceiling"]
    assert ceiling["basis"] == JOB_CEILING_BASIS_CALLER
    assert ceiling["job_bound_s"] == STATED_BOUND_S
    assert ceiling["exceeds_ceiling"] is False
    assert "warning" not in ceiling


# --------------------------------------------------------------------------- #
# The worked shape with an explicit cap
# --------------------------------------------------------------------------- #


def test_cap_splits_into_two_shards_without_the_serial_bound_warning(
    tmp_path, monkeypatch, capsys
):
    """``max_units_per_job: 4`` under the default ``warn``: two shard jobs
    of 4 units, each carrying the stated timeout, and no over-serial-bound
    warning (its "lower ``options.timeout_s``" advice is wrong once the
    caller stated a bound). Not rejected as ``batch_max_units_per_job_invalid``."""
    runner, shard_sizes = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    documents = _capture_uploaded_job_jsons(monkeypatch, runner)
    corners, _engine, _environment = sb._run_batch(
        corner_points=_corner_points(UNITS),
        netlist_path=str(_write_body(tmp_path)),
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=_measurements_spec(),
        timeout_s=PER_UNIT_TIMEOUT_S,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        max_workers=None,
        request=_ceiling_request(
            tmp_path, job_timeout_s=STATED_BOUND_S, max_units_per_job=4
        ),
    )

    assert sorted(shard_sizes) == [4, 4]
    assert [document["timeout_seconds"] for document in documents] == [3400, 3400]
    assert [c["corner_id"] for c in corners] == [f"tt_1.800_{i}" for i in range(UNITS)]
    stderr = capsys.readouterr().err
    assert "serial upper bound" not in stderr
    assert "warning" not in stderr
    ceiling = _environment["job_ceiling"]
    assert ceiling["basis"] == JOB_CEILING_BASIS_CALLER
    assert ceiling["split"] is True
    assert ceiling["effective_hosts"] == 2


def test_cap_plus_enforce_is_still_refused(tmp_path):
    """Unchanged from main: a cap combined with ``job_ceiling_check:
    "enforce"`` is contradictory and refused, whether or not the stated
    bound is set."""
    for batch in (
        {"max_units_per_job": 4, "job_ceiling_check": "enforce"},
        {
            "max_units_per_job": 4,
            "job_ceiling_check": "enforce",
            "job_timeout_s": STATED_BOUND_S,
        },
    ):
        with pytest.raises(SimError) as excinfo:
            sb._resolve_batch_config(
                _ceiling_request(tmp_path, **batch),
                corner_count=UNITS,
                timeout_s=PER_UNIT_TIMEOUT_S,
                check_runner=False,
            )
        assert excinfo.value.code == "batch_max_units_per_job_invalid"


# --------------------------------------------------------------------------- #
# The stated bound over the ceiling
# --------------------------------------------------------------------------- #


def test_bound_over_the_ceiling_warns_under_warn(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _corners, environment, documents = _run_stated(
        tmp_path,
        monkeypatch,
        runner,
        units=UNITS,
        job_timeout_s=OVER_CEILING_BOUND_S,
    )

    stderr = capsys.readouterr().err
    assert "batch.job_timeout_s" in stderr
    assert "remote.hosts" not in stderr
    assert "options.timeout_s" not in stderr
    assert [document["timeout_seconds"] for document in documents] == [4000]
    ceiling = environment["job_ceiling"]
    assert ceiling["exceeds_ceiling"] is True
    assert ceiling["max_units_per_job"] == 0
    assert ceiling["recommended_hosts"] is None
    assert "batch.job_timeout_s" in ceiling["warning"]


@pytest.mark.parametrize("check", ["enforce", "split"])
def test_bound_over_the_ceiling_is_refused_under_enforce_and_split(
    tmp_path, monkeypatch, check
):
    """``split`` refuses too: a unit-count-independent bound cannot be
    helped by splitting (like the 'even one unit exceeds' case)."""
    runner = _FakeRunner()
    with pytest.raises(SimError) as excinfo:
        _run_stated(
            tmp_path,
            monkeypatch,
            runner,
            units=UNITS,
            job_timeout_s=OVER_CEILING_BOUND_S,
            job_ceiling_check=check,
        )
    assert excinfo.value.code == "batch_job_ceiling_exceeded"
    assert "batch.job_timeout_s" in str(excinfo.value)
    assert "options.timeout_s" not in str(excinfo.value)
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# Poll budget
# --------------------------------------------------------------------------- #


def test_poll_budget_defaults_to_the_stated_bound_plus_slack(tmp_path):
    config = sb._resolve_batch_config(
        _ceiling_request(tmp_path, job_timeout_s=STATED_BOUND_S),
        corner_count=UNITS,
        timeout_s=PER_UNIT_TIMEOUT_S,
        check_runner=False,
    )
    assert config.poll_timeout_s == STATED_BOUND_S + 1800.0


def test_poll_budget_default_without_the_key_is_unchanged(tmp_path):
    config = sb._resolve_batch_config(
        _ceiling_request(tmp_path),
        corner_count=UNITS,
        timeout_s=PER_UNIT_TIMEOUT_S,
        check_runner=False,
    )
    assert config.poll_timeout_s == SERIAL_BOUND_S + 1800.0


def test_explicit_poll_timeout_s_still_wins(tmp_path):
    config = sb._resolve_batch_config(
        _ceiling_request(tmp_path, job_timeout_s=STATED_BOUND_S, poll_timeout_s=99.0),
        corner_count=UNITS,
        timeout_s=PER_UNIT_TIMEOUT_S,
        check_runner=False,
    )
    assert config.poll_timeout_s == 99.0


# --------------------------------------------------------------------------- #
# Unset path and unaffected shapes
# --------------------------------------------------------------------------- #


def test_unset_path_assessment_is_the_serial_bound(tmp_path, monkeypatch):
    """No ceiling warning and the serial basis when the serial bound fits
    (8 units x 1500 s unset would warn, so this uses a 100 s timeout)."""
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    config = sb._resolve_batch_config(
        _ceiling_request(tmp_path),
        corner_count=UNITS,
        timeout_s=100.0,
        check_runner=False,
    )
    assessment = sb._assess_job_ceiling(
        unit_count=UNITS, per_unit_timeout_s=100.0, config=config
    )
    assert assessment["basis"] == "conservative_serial_bound"
    assert assessment["job_bound_s"] == 100.0 * UNITS + 120.0
    assert assessment["exceeds_ceiling"] is False
    assert assessment["max_units_per_job"] >= UNITS
    assert "warning" not in assessment


def test_explicit_multi_hosts_shards_each_carry_the_stated_timeout(
    tmp_path, monkeypatch, capsys
):
    """An explicit ``remote.hosts > 1`` is honored as given and its shards
    are not assessed: each shard job still carries the stated timeout."""
    runner, shard_sizes = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    documents = _capture_uploaded_job_jsons(monkeypatch, runner)
    corners, _engine, _environment = sb._run_batch_fleet(
        corner_points=_corner_points(16),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=PER_UNIT_TIMEOUT_S,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request=_ceiling_request(tmp_path, job_timeout_s=STATED_BOUND_S),
        hosts=2,
        measurements_spec=_measurements_spec(),
    )

    assert sorted(shard_sizes) == [8, 8]
    assert [document["timeout_seconds"] for document in documents] == [3400, 3400]
    assert len(corners) == 16
    assert "warning" not in capsys.readouterr().err


def test_options_max_workers_does_not_change_anything(tmp_path):
    """``options.max_workers`` changes the job command, not the sizing of
    the stated bound."""
    request = _ceiling_request(tmp_path, job_timeout_s=STATED_BOUND_S)
    request["options"] = {"max_workers": 4}
    config = sb._resolve_batch_config(
        request,
        corner_count=UNITS,
        timeout_s=PER_UNIT_TIMEOUT_S,
        check_runner=False,
    )
    assert config.job_timeout_s == STATED_BOUND_S
    assert config.poll_timeout_s == STATED_BOUND_S + 1800.0
    assessment = sb._assess_job_ceiling(
        unit_count=UNITS, per_unit_timeout_s=PER_UNIT_TIMEOUT_S, config=config
    )
    assert assessment["basis"] == JOB_CEILING_BASIS_CALLER
    assert assessment["exceeds_ceiling"] is False


def test_fractional_stated_bound_is_ceiled_into_job_json(tmp_path):
    """``timeout_seconds`` is an integer: the ceiling of the stated bound."""
    from klayout_tools.sim_batch_contract_jobspec import _build_batch_job_spec

    spec = _build_batch_job_spec(
        {"models": {}, "netlist": "n.cir"},
        str(_write_body(tmp_path)),
        corner_count=UNITS,
        timeout_s=PER_UNIT_TIMEOUT_S,
        job_timeout_s=3399.5,
        keep_artifacts=False,
    )
    assert spec.to_job_json()["timeout_seconds"] == math.ceil(3399.5) == 3400


def test_split_plan_refuses_only_when_the_bound_exceeds():
    """``_plan_job_split``'s existing branches produce the issue's three
    cases without a special case: fits + no cap -> one job; exceeds -> the
    ceiling refusal (``max_units_per_job == 0``); fits + cap -> balanced
    shards, no warning."""
    fits = sb._assess_job_ceiling(
        unit_count=UNITS, per_unit_timeout_s=PER_UNIT_TIMEOUT_S, config=_caller_config()
    )
    assert sb._plan_job_split(fits, _caller_config()) == 1

    capped = _caller_config(max_units_per_job=4)
    assert sb._plan_job_split(fits, capped) == 2

    exceeds = sb._assess_job_ceiling(
        unit_count=UNITS,
        per_unit_timeout_s=PER_UNIT_TIMEOUT_S,
        config=_caller_config(job_timeout_s=OVER_CEILING_BOUND_S),
    )
    with pytest.raises(SimError) as excinfo:
        sb._plan_job_split(exceeds, _caller_config(job_ceiling_check="split"))
    assert excinfo.value.code == "batch_job_ceiling_exceeded"
    assert "batch.job_timeout_s" in str(excinfo.value)
