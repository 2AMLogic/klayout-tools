"""Poll-knob validation for ``backend: batch`` (issue #7543).

``request.batch.poll_interval_s`` and ``request.batch.poll_timeout_s`` were
the last two ``batch`` seconds knobs parsed with a bare ``float()`` in
``_resolve_batch_config``: a malformed value escaped the ``klt sim`` JSON
error envelope as an uncaught ``ValueError``/``TypeError`` traceback, and a
``bool``/non-finite/negative value was silently accepted. Both now route
through :func:`~klayout_tools.sim_batch_job_timeout.resolve_poll_interval_s`
/ :func:`~klayout_tools.sim_batch_job_timeout.resolve_poll_timeout_s` (the
module already holding the sibling ``resolve_job_timeout_s``), refusing
through :class:`~klayout_tools.sim.SimError` before any S3 write like every
other knob. ``test_sim_batch.py`` sits at its file-size-baseline ceiling, so
these regressions live here, modeled on ``test_sim_batch_huge_int.py`` and
the CLI dry-run pattern of ``test_sim_dry_run_batch_ceiling.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch_job_timeout as sbjt
from klayout_tools.cli import main
from klayout_tools.sim_batch_contract import DEFAULT_POLL_INTERVAL_S
from klayout_tools.sim_batch_job_timeout import BATCH_POLL_KNOB_CODE

FAKE_BUCKET = "fake-batch-jobs-000000000000"

CORNERS = {"process": ["TYP"], "params": {"vdd": [1.6, 1.8, 2.0]}}

#: Every refusal class: non-numeric (string, null, list, dict), ``bool``
#: (an ``int`` subclass), non-finite (``nan``/``inf``), negative, and the
#: #5994 huge-int literal (an ``int`` too large for a ``float``).
BAD_VALUES = ["x", None, [30], {"s": 30}, True, float("nan"), float("inf"), -1, 10**400]


@pytest.fixture(autouse=True)
def _batch_env(monkeypatch):
    """No fleet config from this host: every batch knob comes from the
    request, so the resolution under test is deterministic."""
    from klayout_tools import sim_batch as sb

    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    monkeypatch.delenv(sb.PROVISION_SCRIPT_ENV, raising=False)
    monkeypatch.delenv(sb.BUCKET_ENV, raising=False)
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)


def test_absent_poll_interval_s_keeps_the_documented_default():
    assert sbjt.resolve_poll_interval_s({}) == DEFAULT_POLL_INTERVAL_S


def test_absent_poll_timeout_s_keeps_the_callers_derived_default():
    assert sbjt.resolve_poll_timeout_s({}, 4321.5) == 4321.5


@pytest.mark.parametrize("default", [-5, float("nan")])
def test_invalid_derived_timeout_names_its_source(default):
    with pytest.raises(sim.SimError, match="derived batch poll timeout") as exc:
        sbjt.resolve_poll_timeout_s({}, default)
    assert exc.value.code == BATCH_POLL_KNOB_CODE
    assert "request.batch.poll_timeout_s" not in str(exc.value)


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_bad_poll_interval_s_is_a_documented_error_not_a_traceback(bad):
    with pytest.raises(sim.SimError, match=r"request\.batch\.poll_interval_s") as exc:
        sbjt.resolve_poll_interval_s({"poll_interval_s": bad})
    assert exc.value.code == BATCH_POLL_KNOB_CODE


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_bad_poll_timeout_s_is_a_documented_error_not_a_traceback(bad):
    with pytest.raises(sim.SimError, match=r"request\.batch\.poll_timeout_s") as exc:
        sbjt.resolve_poll_timeout_s({"poll_timeout_s": bad}, 4321.5)
    assert exc.value.code == BATCH_POLL_KNOB_CODE


@pytest.mark.parametrize("good", [0, 0.5, 30])
def test_good_poll_interval_s_is_returned_as_a_float(good):
    value = sbjt.resolve_poll_interval_s({"poll_interval_s": good})
    assert value == float(good)
    assert isinstance(value, float)


@pytest.mark.parametrize("good", [0, 0.5, 30])
def test_good_poll_timeout_s_is_returned_as_a_float(good):
    value = sbjt.resolve_poll_timeout_s({"poll_timeout_s": good}, 4321.5)
    assert value == float(good)
    assert isinstance(value, float)


def test_refusal_message_names_the_offending_value():
    with pytest.raises(sim.SimError) as exc:
        sbjt.resolve_poll_interval_s({"poll_interval_s": "x"})
    assert repr("x") in str(exc.value)


# --- the end-to-end contract: a parseable error envelope, no traceback ---- #


def _write_request(tmp_path: Path, batch: dict) -> str:
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
        "corners": CORNERS,
        "options": {"timeout_s": 60},
        "batch": {
            "bucket": FAKE_BUCKET,
            "region": "us-east-1",
            "provision_script_path": str(tmp_path / "prov.sh"),
            **batch,
        },
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return str(path)


@pytest.mark.parametrize("field", ["poll_interval_s", "poll_timeout_s"])
def test_cli_dry_run_refuses_a_malformed_poll_knob_through_the_envelope(
    tmp_path, capsys, field
):
    """``klt sim --dry-run --format json`` on a configuration that otherwise
    resolves (bucket + provision script) exits 1 with the documented error
    envelope (stderr, per docs/json-contract.md -- stdout stays empty), not
    an uncaught traceback."""
    path = _write_request(tmp_path, {field: "x"})
    code = main(["sim", path, "--backend", "batch", "--dry-run", "--format", "json"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["command"] == "sim"
    assert payload["error"]["code"] == BATCH_POLL_KNOB_CODE
    assert field in payload["error"]["message"]


@pytest.mark.parametrize("field", ["poll_interval_s", "poll_timeout_s"])
def test_submit_preflight_refuses_a_malformed_poll_knob_too(tmp_path, field):
    """The submit path's own preflight raises the same refusal, so the dry
    run and the submit agree on accept-vs-refuse (the #5674 discipline)."""
    from klayout_tools.sim_batch_plan import preflight_batch_request

    path = _write_request(tmp_path, {field: "x"})
    with pytest.raises(sim.SimError) as exc:
        preflight_batch_request(path)
    assert exc.value.code == BATCH_POLL_KNOB_CODE
