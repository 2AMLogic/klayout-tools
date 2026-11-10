"""Direct tests for the helpers the batch modules share via ``_batch_common``
(``job_timeout_seconds``, ``rewrite_runner_paths``) and for the collected-report
error branches in ``extract_batch`` / ``place_and_route_batch`` that those
helpers' callers keep (issue #5238 moved the helpers; the coverage ratchet
counts them per module)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import _batch_common, sim_batch
from klayout_tools import extract_batch as eb
from klayout_tools import place_and_route_batch as par_batch
from klayout_tools.extract import ExtractError


def _write_report(collected: Path, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (collected / sim_batch.BATCH_REPORT_FILENAME).write_text(text, encoding="utf-8")


# --- _batch_common.job_timeout_seconds ------------------------------------- #


def test_job_timeout_adds_the_per_job_overhead():
    expected = int(sim_batch._default_batch_job_timeout_s(1, 100.0) + 0.999999)
    got = _batch_common.job_timeout_seconds(100.0)
    assert isinstance(got, int)
    assert got > 100
    assert got in (expected, expected - 1)


def test_job_timeout_is_monotonic_and_ceilinged():
    assert _batch_common.job_timeout_seconds(10.5) >= _batch_common.job_timeout_seconds(
        10
    )


# --- _batch_common.rewrite_runner_paths ------------------------------------- #


def test_rewrite_reroots_exact_and_prefixed_strings():
    out = _batch_common.rewrite_runner_paths(
        ["/run/a", "/run/a/x.gds", "/run/ab", "/other"], ["/run/a"], "/local/"
    )
    assert out == ["/local", "/local/x.gds", "/run/ab", "/other"]


def test_rewrite_recurses_into_dicts_and_passes_scalars_through():
    out = _batch_common.rewrite_runner_paths(
        {"p": "/run/a/f", "n": 3, "none": None, "nest": {"l": ["/run/a/g"]}},
        ["/run/a"],
        "/local",
    )
    assert out == {"p": "/local/f", "n": 3, "none": None, "nest": {"l": ["/local/g"]}}


def test_rewrite_prefers_the_first_matching_root():
    out = _batch_common.rewrite_runner_paths("/run/a/b/c", ["/run/a/b", "/run/a"], "/L")
    assert out == "/L/c"


# --- extract_batch._read_report -------------------------------------------- #


def test_extract_read_report_missing_file(tmp_path):
    with pytest.raises(ExtractError) as err:
        eb._read_report(str(tmp_path), "job1")
    assert err.value.code == _batch_common.BATCH_JOB_FAILED_CODE


def test_extract_read_report_runner_error_keeps_its_code(tmp_path):
    _write_report(
        tmp_path,
        {
            "schema_version": 1,
            "error": {"command": "extract", "message": "bad", "code": "deck_missing"},
        },
    )
    with pytest.raises(ExtractError) as err:
        eb._read_report(str(tmp_path), "job1")
    assert err.value.code == "deck_missing"
    assert "bad" in str(err.value)


def test_extract_read_report_runner_error_without_code_defaults(tmp_path):
    _write_report(
        tmp_path,
        {"schema_version": 1, "error": {"command": "extract", "message": "bad"}},
    )
    with pytest.raises(ExtractError) as err:
        eb._read_report(str(tmp_path), "job1")
    assert err.value.code == _batch_common.BATCH_JOB_FAILED_CODE


def test_extract_read_report_rejects_a_non_extract_report(tmp_path):
    _write_report(tmp_path, {"unrelated": True})
    with pytest.raises(ExtractError, match="not a `klt extract` report"):
        eb._read_report(str(tmp_path), "job1")


def test_extract_read_report_accepts_an_extract_report(tmp_path):
    _write_report(tmp_path, {"netlist_path": "/x"})
    assert eb._read_report(str(tmp_path), "job1") == {"netlist_path": "/x"}


def test_extract_timeout_must_be_positive():
    with pytest.raises(ExtractError, match="must be positive"):
        eb.run_extract_batch({}, "/tmp", output=None, timeout_s=0)


# --- place_and_route_batch._read_unit_report -------------------------------- #


def test_par_read_unit_report_missing_file(tmp_path):
    with pytest.raises(par_batch._UnitFailed) as err:
        par_batch._read_unit_report(str(tmp_path), "job1")
    assert err.value.code == par_batch.BATCH_JOB_FAILED_CODE


def test_par_read_unit_report_runner_error_keeps_its_code(tmp_path):
    _write_report(
        tmp_path, {"status": "error", "error": {"message": "nope", "code": "x_code"}}
    )
    with pytest.raises(par_batch._UnitFailed) as err:
        par_batch._read_unit_report(str(tmp_path), "job1")
    assert err.value.code == "x_code"
    assert "nope" in str(err.value)


@pytest.mark.parametrize(
    "payload", [{"status": "error"}, {"status": "error", "error": "str"}]
)
def test_par_read_unit_report_without_a_runner_error(tmp_path, payload):
    _write_report(tmp_path, payload)
    with pytest.raises(
        par_batch._UnitFailed, match="not a `klt place-and-route` report"
    ) as err:
        par_batch._read_unit_report(str(tmp_path), "job1")
    assert err.value.code == par_batch.BATCH_JOB_FAILED_CODE


def test_par_read_unit_report_accepts_an_ok_report(tmp_path):
    _write_report(tmp_path, {"status": "ok"})
    assert par_batch._read_unit_report(str(tmp_path), "job1") == {"status": "ok"}


# --- place_and_route_batch._declared_inputs / _local_deck_hash --------------- #


def test_par_declared_inputs_skips_malformed_macros_and_resolves_paths(tmp_path):
    netlist = tmp_path / "top.v"
    netlist.write_text("module top; endmodule\n")
    abs_lef = tmp_path / "abs.lef"
    request = {
        "netlist": "top.v",
        "macros": [
            "not-a-dict",
            {"lef": "rel.lef", "gds": ""},
            {"lef": str(abs_lef), "gds": None},
        ],
    }
    paths = par_batch._declared_inputs(request, str(tmp_path))
    assert paths == [
        str(netlist),
        str(tmp_path / "rel.lef"),
        str(abs_lef),
    ]


def test_par_declared_inputs_without_macros(tmp_path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    assert par_batch._declared_inputs({"netlist": "top.v"}, str(tmp_path)) == [
        str(tmp_path / "top.v")
    ]


def test_par_local_deck_hash_is_none_when_the_deck_does_not_resolve(monkeypatch):
    def _boom(*args, **kwargs):
        raise par_batch.PlaceAndRouteError("no deck")

    monkeypatch.setattr(par_batch, "_resolve_liberty", _boom)
    assert par_batch._local_deck_hash("lib", "tt", None) is None
