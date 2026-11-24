"""Compare batch dry-run errors with submit preflight for the eight request
settings in ``KNOB_ROWS`` (issue #6437), including on hosts without fleet
configuration. The polling fields ``poll_interval_s`` and ``poll_timeout_s``
are parsed separately, after the provision script and bucket resolve.

The last tests are a drift guard (issue #6646): they compare the resolver
calls of ``_validate_batch_request_knobs`` with those of
``_resolve_batch_config`` so a knob resolver added to submit cannot silently
miss the dry run.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools.cli import main
from klayout_tools.sim_batch_plan import preflight_batch_request

FAKE_BUCKET = "fake-batch-jobs-000000000000"

CORNERS = {"process": ["TYP"], "params": {"vdd": [1.6, 1.8, 2.0]}}


@pytest.fixture(autouse=True)
def _batch_env(monkeypatch):
    """No fleet config from this host: every batch knob comes from the
    request, so a refusal names the request's own value."""
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    monkeypatch.delenv(sb.PROVISION_SCRIPT_ENV, raising=False)
    monkeypatch.delenv(sb.BUCKET_ENV, raising=False)
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)


def _write_request(
    tmp_path: Path,
    *,
    batch: dict | None = None,
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
        "corners": CORNERS,
        "options": {"timeout_s": 60},
        "batch": {
            "bucket": FAKE_BUCKET,
            "region": "us-east-1",
            "provision_script_path": str(tmp_path / "prov.sh"),
            **(batch or {}),
        },
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return str(path)


def _configless_batch_request(tmp_path: Path, batch: dict) -> str:
    """A ``batch`` block carrying knobs but no bucket and no provision
    script: this host can resolve no fleet config from it, while every knob
    in it is still the request's own property."""
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    (tmp_path / "corner.lib").write_text("* lib\n")
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "models": {"lib": "corner.lib"},
                "backend": "batch",
                "corners": CORNERS,
                "options": {"timeout_s": 60},
                "batch": dict(batch),
            }
        )
    )
    return str(path)


KNOB_ROWS = [
    ("job_ceiling_check", "bogus"),
    ("max_concurrent_jobs", -1),
    ("runner_version_check", "x"),
    ("max_job_seconds", 0),
    ("capacity_wait_s", "x"),
    ("interrupted_grace_s", -5),
    ("job_timeout_s", -1),
    ("capacity_retries", -1),
]
KNOB_IDS = [f"{knob}={value!r}" for knob, value in KNOB_ROWS]


def test_sim_batch_reexports_knob_contract_constants():
    from klayout_tools import sim_batch_contract
    from klayout_tools.sim_batch import (
        FLEET_MAX_JOB_SECONDS_KEY,
        RUNNER_VERSION_CHECK_MODES,
    )

    assert FLEET_MAX_JOB_SECONDS_KEY == sim_batch_contract.FLEET_MAX_JOB_SECONDS_KEY
    assert RUNNER_VERSION_CHECK_MODES == sim_batch_contract.RUNNER_VERSION_CHECK_MODES


@pytest.mark.parametrize(("knob", "value"), KNOB_ROWS, ids=KNOB_IDS)
def test_malformed_batch_knob_is_refused_like_the_submit(tmp_path, knob, value):
    path = _write_request(tmp_path, batch={knob: value})
    with pytest.raises(sim.SimError) as dry:
        sim.run_sim(path, backend="batch", dry_run=True)
    with pytest.raises(sim.SimError) as oracle:
        preflight_batch_request(path)
    assert dry.value.code == oracle.value.code
    assert str(dry.value) == str(oracle.value)


@pytest.mark.parametrize(("knob", "value"), KNOB_ROWS, ids=KNOB_IDS)
def test_configless_host_still_refuses_a_malformed_knob(tmp_path, knob, value):
    # The knob is a request property, not host config: it refuses with the
    # message the submit path gives when the configuration *does* resolve.
    (tmp_path / "cfg").mkdir()
    (tmp_path / "nocfg").mkdir()
    configured = _write_request(tmp_path / "cfg", batch={knob: value})
    with pytest.raises(sim.SimError) as oracle:
        preflight_batch_request(configured)
    configless = _configless_batch_request(tmp_path / "nocfg", {knob: value})
    with pytest.raises(sim.SimError) as dry:
        sim.run_sim(configless, backend="batch", dry_run=True)
    assert str(dry.value) == str(oracle.value)


def test_configless_host_stays_valid_for_a_valid_batch_block(tmp_path):
    # #5449 posture: valid knobs plus no bucket/provision script dries-run
    # clean -- only malformed knobs refuse ahead of the host-dependent
    # resolution.
    path = _configless_batch_request(
        tmp_path,
        {
            "job_ceiling_check": "split",
            "runner_version_check": "warn",
            "capacity_wait_s": 30,
            "interrupted_grace_s": 0,
        },
    )
    report = sim.run_sim(path, backend="batch", dry_run=True)
    assert report["status"] == "valid"
    assert "job_ceiling" not in report


def test_cli_knob_refusal_exits_1_with_the_code(tmp_path, capsys):
    path = _write_request(tmp_path, batch={"max_concurrent_jobs": -1})
    code = main(["sim", path, "--backend", "batch", "--dry-run", "--format", "json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == sb.BATCH_MAX_CONCURRENT_CODE


# --- Resolver-parity drift guard (issue #6646) -------------------------------
#
# ``_validate_batch_request_knobs`` hand-mirrors the request-only resolvers
# ``_resolve_batch_config`` calls; drift has happened twice (#5674, #6437). The
# guard reads both function bodies with ``ast`` and compares the names they
# call. Accepted gap: knobs read inline (``batch_spec.get("bucket")``,
# ``profile``, ``region``, ``jobs_prefix``) go through no resolver function, so
# a name-based guard cannot see them.

#: Resolvers ``_resolve_batch_config`` calls that the validator deliberately
#: does not. A new resolver must either be mirrored in the validator or be
#: added here with its reason; an entry that stops matching also fails.
UNMIRRORED_RESOLVERS = {
    "_resolve_provision_script": (
        "host config: it locates the provision script on this host, so a dry "
        "run on a bare host must not fail on it before the bucket resolves"
    ),
    "resolve_poll_interval_s": (
        "parsed only after the provision script and bucket resolve; its dry-run "
        "refusal is carried by dry_run_runner_check re-raising "
        "BATCH_POLL_KNOB_CODE (#7543, tests/test_sim_batch_poll_knobs.py)"
    ),
    "resolve_poll_timeout_s": (
        "same as resolve_poll_interval_s, and it also takes "
        "_default_batch_poll_timeout_s(corner_count, timeout_s, ...), which is "
        "not request-only"
    ),
}

_RESOLVER_NAME = re.compile(r"^_?resolve_")


def _resolver_calls_in(source: str) -> list[str]:
    """Names matching ``^_?resolve_`` called in ``source``, in source order.

    ``ast.walk`` is breadth-first, so calls are sorted by position; a call
    inside an ``if`` still counts as called.
    """
    tree = ast.parse(textwrap.dedent(source))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if _RESOLVER_NAME.match(name):
            found.append((node.lineno, node.col_offset, name))
    return [name for _, _, name in sorted(found)]


def _resolver_calls(fn) -> list[str]:
    return _resolver_calls_in(inspect.getsource(fn))


def test_resolver_calls_reads_source_order_and_conditional_calls():
    sample = """
def sample(spec):
    _resolve_first(spec)
    if spec.get("x") is not None:
        helpers.resolve_second(spec)
    not_a_resolver(spec)
    return _resolve_third(spec)
"""
    assert _resolver_calls_in(sample) == [
        "_resolve_first",
        "resolve_second",
        "_resolve_third",
    ]


def test_validator_covers_every_submit_resolver_or_allowlists_it():
    submit = set(_resolver_calls(sb._resolve_batch_config))
    validator = set(_resolver_calls(sb._validate_batch_request_knobs))
    # An empty side means the AST read broke, not that nothing drifted.
    assert submit
    assert validator

    unmirrored = submit - validator
    assert not (unmirrored - set(UNMIRRORED_RESOLVERS)), (
        f"_resolve_batch_config calls {sorted(unmirrored - set(UNMIRRORED_RESOLVERS))} "
        "but _validate_batch_request_knobs does not: mirror it there, in "
        "submit order, or add it to UNMIRRORED_RESOLVERS with a reason"
    )
    assert not (set(UNMIRRORED_RESOLVERS) - unmirrored), (
        f"UNMIRRORED_RESOLVERS lists {sorted(set(UNMIRRORED_RESOLVERS) - unmirrored)}, "
        "which _resolve_batch_config no longer calls or the validator now "
        "mirrors: drop the stale entry"
    )


def test_validator_calls_shared_resolvers_in_submit_order():
    submit = _resolver_calls(sb._resolve_batch_config)
    validator = _resolver_calls(sb._validate_batch_request_knobs)
    shared_in_submit_order = [name for name in submit if name in validator]
    assert validator == shared_in_submit_order, (
        "_validate_batch_request_knobs must call only resolvers "
        "_resolve_batch_config also calls, in the same relative order, so both "
        f"paths report the same first error:\n  submit:    {submit}\n"
        f"  validator: {validator}"
    )
