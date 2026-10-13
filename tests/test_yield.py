"""Tests for `klt yield` and the `run_yield` library function (issue #816).

Two tiers, deliberately split:

- The **input-reader** tier (`klt sim` report / sample-set document parsing,
  limits merging, every error the CLI can raise before any statistics run)
  needs no Rust and always runs.
- The **statistics** tier needs the `klt_yield_native` extension to be built
  and importable (`maturin develop --release` in `native/yield/`, or
  `uv sync --group yield`); those tests are skipped with a clear reason --
  never silently passed -- when it is not, so an environment without a Rust
  toolchain degrades gracefully. CI's `native-yield` job is what makes sure
  that skip is never the only thing the pipeline ever sees.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys

import pytest

from klayout_tools.cli import main
from klayout_tools.yield_analysis import (
    _SIM_INCONCLUSIVE_STATUS,
    YieldError,
    _measurements_from_sim_report,
    _read_samples,
    derive_sample_set,
    run_yield,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(REPO_ROOT, "examples", "yield")

requires_native = pytest.mark.skipif(
    importlib.util.find_spec("klt_yield_native") is None,
    reason=(
        "klt_yield_native is not built -- run `maturin develop --release` in "
        "native/yield/ (or `uv sync --group yield`); see "
        "docs/cli/yield.md#building-the-native-extension"
    ),
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _sample_set(tmp_path, samples, limits=None, name="m"):
    entry = {"name": name, "unit": "V", "samples": samples}
    if limits is not None:
        entry["limits"] = limits
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"measurements": [entry]}))
    return str(path)


def _sim_report(
    tmp_path,
    values,
    limits=None,
    corner="tt/1.800V/27C",
    netlist="tb.spice",
    schema_version=1,
):
    """A minimal `klt sim` Monte Carlo report over one measurement.

    ``netlist``/``schema_version`` default to `klt sim`'s **schema-v1** shape
    (a raw path string) so every pre-existing caller is unaffected; pass the
    `{path, scope}` object plus ``schema_version=2`` to exercise the shape
    `klt sim` emits from its version 2 onward (issue #1261).
    """
    corners = [
        {
            "corner_id": f"{corner}/mc{i}",
            "status": "pass",
            "measurements": [{"name": "vref", "value": v, "unit": "V"}],
            "monte_carlo": {"sample_index": i, "seed": 1000 + i},
        }
        for i, v in enumerate(values)
    ]
    report = {
        "schema_version": schema_version,
        "netlist": netlist,
        "status": "pass",
        "environment": {"monte_carlo": {"n": len(values), "seed": 1000}},
        "measurements": [{"name": "vref", "unit": "V", "limits": limits or {}}],
        "corners": corners,
    }
    path = tmp_path / "sim.json"
    path.write_text(json.dumps(report))
    return str(path)


def _normal_grid(n: int, mu: float, sigma: float) -> list[float]:
    """`n` inverse-CDF-spaced draws from `N(mu, sigma)` -- deterministic, and
    as close to the analytic distribution as a finite sample gets. Mirrors
    the Rust test helper of the same name."""
    return [mu + sigma * _norm_ppf((i - 0.5) / n) for i in range(1, n + 1)]


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF by bisection on `math.erfc` -- slow but
    exact enough, and independent of the crate's own implementation (which
    is what these tests are checking)."""
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if 0.5 * math.erfc(-mid / math.sqrt(2.0)) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Input readers (no native extension needed)
# --------------------------------------------------------------------------- #


def test_sim_report_is_read_without_an_intermediate_format(tmp_path):
    path = _sim_report(tmp_path, [1.0, 2.0, 3.0], limits={"min": 0.5, "max": 3.5})
    kind, measurements, source = _read_samples(path)
    assert kind == "sim-report"
    assert source["netlist"] == "tb.spice"
    assert source["monte_carlo"]["n"] == 3
    assert measurements[0]["samples"] == [1.0, 2.0, 3.0]
    assert measurements[0]["limits"] == {"min": 0.5, "max": 3.5}
    # The `/mc<i>` suffix is stripped, so the originating corner is named once.
    assert measurements[0]["source_corners"] == ["tt/1.800V/27C"]


def test_sim_report_schema_v2_netlist_object_is_unwrapped_to_a_string(tmp_path):
    """`klt sim` emits `netlist` as `{path, scope}` from its own
    `schema_version` 2 onward (issue #1261, so an absolute path outside the
    invocation's repo never leaks into a committed record). `klt yield`'s
    `source.netlist` contract is a plain string (`docs/cli/yield.md`), and
    `klt yield-campaign` feeds a live `run_sim` report straight into
    `run_yield` -- so the object must be unwrapped here rather than echoed
    through, which would silently break a second command's contract."""
    path = _sim_report(
        tmp_path,
        [1.0, 2.0, 3.0],
        limits={"min": 0.5, "max": 3.5},
        netlist={"path": "tb.spice", "scope": "repo"},
        schema_version=2,
    )
    _kind, _measurements, source = _read_samples(path)
    assert source["netlist"] == "tb.spice"


def test_sim_report_schema_v2_external_netlist_reads_as_null(tmp_path):
    """A netlist outside the repo carries `{"path": null, "scope":
    "external"}` -- `source.netlist` is `null`, the same "no netlist
    recorded" value the sample-set branch already emits. Never the raw
    absolute path, and never the object."""
    path = _sim_report(
        tmp_path,
        [1.0, 2.0, 3.0],
        limits={"min": 0.5, "max": 3.5},
        netlist={"path": None, "scope": "external"},
        schema_version=2,
    )
    _kind, _measurements, source = _read_samples(path)
    assert source["netlist"] is None


def test_sim_report_with_no_netlist_field_reads_as_null(tmp_path):
    """A report carrying no `netlist` field at all still yields the key, set
    to `null` -- `source.netlist` is never absent, in either schema."""
    path = _sim_report(tmp_path, [1.0, 2.0, 3.0], limits={"min": 0.5, "max": 3.5})
    with open(path, encoding="utf-8") as handle:
        report = json.load(handle)
    del report["netlist"]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle)

    _kind, _measurements, source = _read_samples(path)
    assert "netlist" in source
    assert source["netlist"] is None


def test_sim_report_counts_null_measurements_as_errored(tmp_path):
    path = _sim_report(tmp_path, [1.0, None, 3.0], limits={"min": 0.5, "max": 3.5})
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["samples"] == [1.0, 3.0]
    assert measurements[0]["errored"] == 1


def test_sim_report_ignores_non_monte_carlo_corners(tmp_path):
    """A report mixing a PVT matrix with a Monte Carlo request has both; the
    deterministic corners are not draws from any distribution."""
    path = _sim_report(tmp_path, [1.0, 2.0], limits={"min": 0.5, "max": 3.5})
    report = json.loads(open(path).read())
    report["corners"].append(
        {
            "corner_id": "ss/1.620V/125C",
            "status": "pass",
            "measurements": [{"name": "vref", "value": 99.0, "unit": "V"}],
            "monte_carlo": None,
        }
    )
    (tmp_path / "sim.json").write_text(json.dumps(report))
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["samples"] == [1.0, 2.0]


def test_sim_report_without_monte_carlo_samples_is_an_error(tmp_path):
    path = tmp_path / "sim.json"
    path.write_text(
        json.dumps(
            {
                "measurements": [{"name": "vref", "limits": {"max": 1.0}}],
                "corners": [{"corner_id": "tt", "monte_carlo": None}],
            }
        )
    )
    with pytest.raises(YieldError, match="no Monte Carlo samples"):
        _read_samples(str(path))


def test_sample_set_document_is_read(tmp_path):
    path = _sample_set(tmp_path, [1.0, None, 3.0], limits={"max": 4.0})
    kind, measurements, _source = _read_samples(path)
    assert kind == "sample-set"
    assert measurements[0]["samples"] == [1.0, 3.0]
    assert measurements[0]["errored"] == 1
    assert measurements[0]["limits"] == {"max": 4.0}


def test_unrecognised_document_is_an_error(tmp_path):
    path = tmp_path / "junk.json"
    path.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(YieldError, match="neither a `klt sim` report"):
        _read_samples(str(path))


def test_missing_file_is_a_clean_error(tmp_path):
    with pytest.raises(YieldError, match="samples file not found"):
        run_yield(str(tmp_path / "nope.json"))


def test_malformed_json_is_a_clean_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(YieldError, match="could not read samples"):
        run_yield(str(path))


@requires_native
def test_a_measurement_without_limits_is_skipped_with_a_warning(tmp_path):
    """...but only when it was not asked for by name."""
    path = tmp_path / "samples.json"
    path.write_text(
        json.dumps(
            {
                "measurements": [
                    {"name": "a", "samples": [1.0, 2.0]},
                    {"name": "b", "samples": [1.0, 2.0], "limits": {"max": 5.0}},
                ]
            }
        )
    )
    report = run_yield(str(path))
    assert [m["name"] for m in report["measurements"]] == ["b"]
    assert any("'a' declares no spec limits" in w for w in report["warnings"])


def test_an_explicitly_named_measurement_without_limits_is_an_error(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0], name="a")
    with pytest.raises(YieldError, match="requested explicitly but declares no"):
        run_yield(path, measurements=["a"])


def test_no_measurement_with_limits_at_all_is_an_error(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0])
    with pytest.raises(YieldError, match="no measurement has spec limits"):
        run_yield(path)


def test_an_unknown_measurement_name_is_an_error(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0], limits={"max": 5.0}, name="a")
    with pytest.raises(YieldError, match="no such measurement"):
        run_yield(path, measurements=["nope"])


def test_limits_file_without_measurements_is_an_error(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0], limits={"max": 5.0}, name="a")
    limits = tmp_path / "limits.json"
    limits.write_text(json.dumps({"confidence": 0.9}))
    with pytest.raises(YieldError, match="no 'measurements' object"):
        run_yield(path, limits_path=str(limits))


def test_sim_report_with_a_non_numeric_value_is_an_error(tmp_path):
    path = _sim_report(tmp_path, [1.0, 2.0], limits={"max": 3.0})
    report = json.loads(open(path).read())
    report["corners"][0]["measurements"][0]["value"] = "oops"
    with pytest.raises(YieldError, match="non-numeric value"):
        _measurements_from_sim_report(report)


# --------------------------------------------------------------------------- #
# exclusive_min / exclusive_max parsing (issue #1083, no native extension
# needed -- these are input-reader-tier checks)
# --------------------------------------------------------------------------- #


@requires_native
def test_exclusive_min_and_max_are_parsed_from_a_sample_set(tmp_path):
    path = _sample_set(
        tmp_path,
        [1.0, 2.0],
        limits={"min": 0.0, "exclusive_min": True, "max": 5.0, "exclusive_max": True},
    )
    report = run_yield(path)
    m = report["measurements"][0]
    assert m["limits"]["exclusive_min"] is True
    assert m["limits"]["exclusive_max"] is True


@requires_native
def test_exclusive_min_defaults_to_false_when_absent(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0], limits={"min": 0.0, "max": 5.0})
    report = run_yield(path)
    m = report["measurements"][0]
    # Absent in the request -> the native side's default, echoed back as
    # `False` (not present at all) since the field is only serialised when
    # `true`.
    assert not m["limits"].get("exclusive_min")
    assert not m["limits"].get("exclusive_max")


def test_a_non_boolean_exclusive_min_is_an_error(tmp_path):
    path = _sample_set(
        tmp_path, [1.0, 2.0], limits={"min": 0.0, "exclusive_min": "yes"}
    )
    with pytest.raises(YieldError, match="limits.exclusive_min must be a boolean"):
        run_yield(path)


def test_a_non_boolean_exclusive_max_is_an_error(tmp_path):
    path = _sample_set(
        tmp_path, [1.0, 2.0], limits={"max": 5.0, "exclusive_max": "yes"}
    )
    with pytest.raises(YieldError, match="limits.exclusive_max must be a boolean"):
        run_yield(path)


# --------------------------------------------------------------------------- #
# negative_control / analytic_cross_check parsing (issue #817, no native
# extension needed -- these are input-reader-tier checks)
# --------------------------------------------------------------------------- #


def _sample_set_doc(tmp_path, entry):
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"measurements": [entry]}))
    return str(path)


def test_negative_control_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "negative_control": {
            "samples": [9.0, None, 9.5],
            "description": "vos forced to 10x spec",
        },
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    nc = measurements[0]["negative_control"]
    assert nc["samples"] == [9.0, 9.5]
    assert nc["errored"] == 1
    assert nc["description"] == "vos forced to 10x spec"


def test_a_measurement_without_a_negative_control_reads_as_none(tmp_path):
    path = _sample_set_doc(
        tmp_path, {"name": "m", "samples": [1.0, 2.0], "limits": {"max": 5.0}}
    )
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["negative_control"] is None
    assert measurements[0]["analytic_cross_check"] is None


def test_negative_control_must_be_an_object(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "negative_control": 9,
        },
    )
    with pytest.raises(YieldError, match="non-object negative_control"):
        _read_samples(path)


def test_negative_control_without_samples_is_an_error(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "negative_control": {"description": "oops, forgot the samples"},
        },
    )
    with pytest.raises(YieldError, match="negative_control has no 'samples'"):
        _read_samples(path)


# --------------------------------------------------------------------------- #
# `failed_unmeasurable` parsing (issue #1095, no native extension needed --
# these are input-reader-tier checks across all three normalizers).
# --------------------------------------------------------------------------- #


def test_failed_unmeasurable_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "errored": 2,
        "failed_unmeasurable": 17,
        "limits": {"max": 5.0},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["errored"] == 2
    assert measurements[0]["failed_unmeasurable"] == 17


def test_failed_unmeasurable_defaults_to_zero_on_a_sample_set(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0], limits={"max": 5.0})
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["failed_unmeasurable"] == 0


def test_failed_unmeasurable_is_parsed_from_a_sim_report_rollup_entry(tmp_path):
    """Like `negative_control`/`analytic_cross_check`/`sampling`,
    `failed_unmeasurable` is metadata on the rollup entry, not derivable
    from a corner's `value` -- a sim report has no way to distinguish a
    tooling failure from a design failure at the per-corner level."""
    path = _sim_report(tmp_path, [1.0, 2.0, 3.0], limits={"min": 0.5, "max": 3.5})
    report = json.loads(open(path).read())
    report["measurements"][0]["failed_unmeasurable"] = 9
    (tmp_path / "sim.json").write_text(json.dumps(report))
    _kind, measurements, _source = _read_samples(str(tmp_path / "sim.json"))
    assert measurements[0]["failed_unmeasurable"] == 9


def test_failed_unmeasurable_is_parsed_from_a_negative_control(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "negative_control": {
            "samples": [9.0],
            "failed_unmeasurable": 20,
            "description": "the defect drives every draw out of the regime",
        },
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    nc = measurements[0]["negative_control"]
    assert nc["failed_unmeasurable"] == 20


def test_failed_unmeasurable_defaults_to_zero_on_a_negative_control(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "negative_control": {"samples": [9.0]},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["negative_control"]["failed_unmeasurable"] == 0


def test_a_negative_failed_unmeasurable_is_an_error(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "failed_unmeasurable": -1,
        },
    )
    with pytest.raises(YieldError, match="failed_unmeasurable must be a"):
        _read_samples(path)


def test_a_non_integer_failed_unmeasurable_is_an_error(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "failed_unmeasurable": "lots",
        },
    )
    with pytest.raises(YieldError, match="failed_unmeasurable must be a"):
        _read_samples(path)


# --------------------------------------------------------------------------- #
# `censored` parsing (issue #2468, no native extension needed -- these are
# input-reader-tier checks across all three normalizers).
# --------------------------------------------------------------------------- #


def test_censored_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "errored": 2,
        "censored": 13,
        "limits": {"max": 5.0},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["errored"] == 2
    assert measurements[0]["censored"] == 13


def test_censored_defaults_to_zero_on_a_sample_set(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0], limits={"max": 5.0})
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["censored"] == 0


def test_censored_is_parsed_from_a_sim_report_rollup_entry(tmp_path):
    """Like `failed_unmeasurable`, `censored` is metadata on the rollup
    entry, not derivable from a corner's `value`."""
    path = _sim_report(tmp_path, [1.0, 2.0, 3.0], limits={"min": 0.5, "max": 3.5})
    report = json.loads(open(path).read())
    report["measurements"][0]["censored"] = 6
    (tmp_path / "sim.json").write_text(json.dumps(report))
    _kind, measurements, _source = _read_samples(str(tmp_path / "sim.json"))
    assert measurements[0]["censored"] == 6


def test_censored_is_parsed_from_a_negative_control(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "negative_control": {
            "samples": [9.0],
            "censored": 12,
            "description": "some draws never reached the settled state",
        },
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    nc = measurements[0]["negative_control"]
    assert nc["censored"] == 12


def test_censored_defaults_to_zero_on_a_negative_control(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "negative_control": {"samples": [9.0]},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["negative_control"]["censored"] == 0


def test_a_negative_censored_is_an_error(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "censored": -1,
        },
    )
    with pytest.raises(YieldError, match="censored must be a"):
        _read_samples(path)


def test_a_non_integer_censored_is_an_error(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "censored": "lots",
        },
    )
    with pytest.raises(YieldError, match="censored must be a"):
        _read_samples(path)


# --------------------------------------------------------------------------- #
# `inconclusive` exclusion + parsing (issue #2507 -- `klt sim`'s plausibility
# bounds / `options.fail_on_diagnostic` grading, mirrored into `klt yield`'s
# population. No native extension needed -- these are input-reader-tier
# checks across both extraction paths.)
# --------------------------------------------------------------------------- #


def _sim_report_with_statuses(tmp_path, rows, limits=None):
    """A minimal `klt sim` MC report whose corners carry explicit statuses.

    ``rows`` is a list of ``(value, corner_status, measurement_status)``
    triples -- the two levels `klt sim` grades `"inconclusive"` at
    independently (see `sim.py`'s `_monte_carlo_rollup._stats`).
    """
    corners = [
        {
            "corner_id": f"tt/1.800V/27C/mc{i}",
            "status": corner_status,
            "measurements": [
                {"name": "vref", "value": value, "unit": "V", "status": m_status}
            ],
            "monte_carlo": {"sample_index": i, "seed": 1000 + i},
        }
        for i, (value, corner_status, m_status) in enumerate(rows)
    ]
    report = {
        "schema_version": 1,
        "netlist": "tb.spice",
        "status": "pass",
        "environment": {"monte_carlo": {"n": len(rows), "seed": 1000}},
        "measurements": [{"name": "vref", "unit": "V", "limits": limits or {}}],
        "corners": corners,
    }
    path = tmp_path / "sim.json"
    path.write_text(json.dumps(report))
    return str(path)


def test_the_inconclusive_status_token_matches_klt_sims_own():
    """`yield_analysis` repeats the token rather than importing `sim` (which
    would drag in that whole module just to read a report). Assert the two
    agree, so a rename on either side fails loudly here instead of silently
    letting distrusted samples back into the population."""
    from klayout_tools.sim import _INCONCLUSIVE_STATUS

    assert _SIM_INCONCLUSIVE_STATUS == _INCONCLUSIVE_STATUS


def test_sim_report_excludes_a_measurement_graded_inconclusive(tmp_path):
    """The measurement-level half of the two-level check: a plausibility-bound
    violation (issue #2493) grades the *measurement* `"inconclusive"` while
    its corner still reads `pass`. The value is real and non-null, so the
    pre-existing `value is not None` filter never caught it."""
    path = _sim_report_with_statuses(
        tmp_path,
        [
            (1.0, "pass", "pass"),
            (9.9e9, "pass", "inconclusive"),
            (3.0, "pass", "pass"),
        ],
        limits={"min": 0.5, "max": 3.5},
    )
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["samples"] == [1.0, 3.0]
    assert measurements[0]["inconclusive"] == 1
    assert measurements[0]["errored"] == 0


def test_sim_report_excludes_a_corner_graded_inconclusive(tmp_path):
    """The corner-level half: `options.fail_on_diagnostic` (issue #2492)
    disqualifies the sample's whole *corner*; its measurement's own
    pass/fail grading is intact, since it was graded against `limits`
    before the corner was disqualified."""
    path = _sim_report_with_statuses(
        tmp_path,
        [
            (1.0, "pass", "pass"),
            (2.0, "inconclusive", "pass"),
            (3.0, "pass", "pass"),
        ],
        limits={"min": 0.5, "max": 3.5},
    )
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["samples"] == [1.0, 3.0]
    assert measurements[0]["inconclusive"] == 1


def test_sim_report_inconclusive_is_zero_when_no_status_is_graded(tmp_path):
    """A report predating the status grading (or a hand-authored one) carries
    no `status` on its corner measurements at all -- absent is not
    `"inconclusive"`, so nothing is excluded."""
    path = _sim_report(tmp_path, [1.0, 2.0, 3.0], limits={"min": 0.5, "max": 3.5})
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["samples"] == [1.0, 2.0, 3.0]
    assert measurements[0]["inconclusive"] == 0


def test_sim_report_an_inconclusive_sample_is_counted_once_not_twice(tmp_path):
    """A draw disqualified at *both* levels is one excluded sample, not two --
    `n + errored + inconclusive` must still account for the full draw."""
    path = _sim_report_with_statuses(
        tmp_path,
        [
            (1.0, "pass", "pass"),
            (2.0, "inconclusive", "inconclusive"),
            (None, "pass", "error"),
            (3.0, "pass", "pass"),
        ],
        limits={"min": 0.5, "max": 3.5},
    )
    _kind, measurements, _source = _read_samples(path)
    m = measurements[0]
    assert m["samples"] == [1.0, 3.0]
    assert m["errored"] == 1
    assert m["inconclusive"] == 1
    # The full draw is accounted for: 2 usable + 1 errored + 1 inconclusive.
    assert len(m["samples"]) + m["errored"] + m["inconclusive"] == 4


def test_sim_report_an_inconclusive_sample_with_a_null_value_is_not_errored(tmp_path):
    """An inconclusive draw that also produced no value is inconclusive, not
    errored -- the distrust verdict outranks the missing-value bookkeeping,
    exactly as `sim.py`'s `_stats` sets the sample aside before its own
    `value is not None` filter runs."""
    path = _sim_report_with_statuses(
        tmp_path,
        [
            (1.0, "pass", "pass"),
            (None, "pass", "inconclusive"),
            (3.0, "pass", "pass"),
        ],
        limits={"min": 0.5, "max": 3.5},
    )
    m = _read_samples(path)[1][0]
    assert m["samples"] == [1.0, 3.0]
    assert m["errored"] == 0
    assert m["inconclusive"] == 1


def test_sim_report_an_inconclusive_sample_is_never_validated_as_numeric(tmp_path):
    """A distrusted value is set aside *before* the numeric-type check -- it
    is not part of the population, so its shape is not this reader's
    business (and a non-numeric sentinel must not become a hard error)."""
    path = _sim_report_with_statuses(
        tmp_path,
        [
            (1.0, "pass", "pass"),
            ("no-solution", "pass", "inconclusive"),
            (3.0, "pass", "pass"),
        ],
        limits={"min": 0.5, "max": 3.5},
    )
    m = _read_samples(path)[1][0]
    assert m["samples"] == [1.0, 3.0]
    assert m["inconclusive"] == 1


def test_inconclusive_is_parsed_from_a_sample_set(tmp_path):
    """A plain sample-set document has no per-sample status channel, so the
    count is caller-supplied -- the same shape `errored`/
    `failed_unmeasurable`/`censored` already use."""
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "errored": 2,
        "inconclusive": 4,
        "limits": {"max": 5.0},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["errored"] == 2
    assert measurements[0]["inconclusive"] == 4


def test_inconclusive_defaults_to_zero_on_a_sample_set(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0], limits={"max": 5.0})
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["inconclusive"] == 0


def test_sample_set_inconclusive_accounts_for_the_full_draw(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, None, 3.0],
        "errored": 1,
        "inconclusive": 5,
        "limits": {"max": 5.0},
    }
    path = _sample_set_doc(tmp_path, entry)
    m = _read_samples(path)[1][0]
    # 3 declared samples (one null) + 1 extra errored + 5 inconclusive = 9.
    assert len(m["samples"]) + m["errored"] + m["inconclusive"] == 9


def test_a_negative_inconclusive_is_an_error(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "inconclusive": -1,
        },
    )
    with pytest.raises(YieldError, match="inconclusive must be a"):
        _read_samples(path)


def test_a_non_integer_inconclusive_is_an_error(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "inconclusive": "lots",
        },
    )
    with pytest.raises(YieldError, match="inconclusive must be a"):
        _read_samples(path)


def test_analytic_cross_check_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "analytic_cross_check": {"kind": "mismatch_offset", "sigma": 0.01, "mean": 0.0},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    check = measurements[0]["analytic_cross_check"]
    assert check == {"kind": "mismatch_offset", "sigma": 0.01, "mean": 0.0}


def test_analytic_cross_check_rejects_an_unknown_kind(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "analytic_cross_check": {"kind": "made_up"},
        },
    )
    with pytest.raises(YieldError, match="kind must be one of"):
        _read_samples(path)


def test_analytic_cross_check_rejects_a_field_not_valid_for_its_kind(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "analytic_cross_check": {"kind": "mismatch_offset", "capacitance_f": 1e-12},
        },
    )
    with pytest.raises(YieldError, match="is not valid for kind"):
        _read_samples(path)


# --------------------------------------------------------------------------- #
# sampling parsing (issue #907, Phase 2b of epic #710 -- no native extension
# needed, these are input-reader-tier checks)
# --------------------------------------------------------------------------- #


def test_a_measurement_without_sampling_reads_as_none(tmp_path):
    path = _sample_set_doc(
        tmp_path, {"name": "m", "samples": [1.0, 2.0], "limits": {"max": 5.0}}
    )
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["sampling"] is None


def test_plain_random_sampling_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "sampling": {"strategy": "plain_random"},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["sampling"] == {"strategy": "plain_random"}


def test_latin_hypercube_sampling_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0, 4.0],
        "limits": {"max": 5.0},
        "sampling": {"strategy": "latin_hypercube", "replicates": 2},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["sampling"] == {
        "strategy": "latin_hypercube",
        "replicates": 2,
    }


def test_importance_sampling_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0],
        "limits": {"max": 5.0},
        "sampling": {"strategy": "importance", "weights": [1.5, 2.5]},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["sampling"] == {
        "strategy": "importance",
        "weights": [1.5, 2.5],
    }


def test_sampling_must_be_an_object(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {"name": "m", "samples": [1.0, 2.0], "limits": {"max": 5.0}, "sampling": 9},
    )
    with pytest.raises(YieldError, match="non-object sampling"):
        _read_samples(path)


def test_sampling_rejects_an_unknown_strategy(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "sampling": {"strategy": "made_up"},
        },
    )
    with pytest.raises(YieldError, match="strategy must be one of"):
        _read_samples(path)


def test_sampling_rejects_a_field_not_valid_for_its_strategy(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "sampling": {"strategy": "plain_random", "replicates": 2},
        },
    )
    with pytest.raises(YieldError, match="is not valid for strategy"):
        _read_samples(path)


def test_latin_hypercube_sampling_requires_replicates_at_least_two(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "sampling": {"strategy": "latin_hypercube", "replicates": 1},
        },
    )
    with pytest.raises(YieldError, match="replicates must be an integer >= 2"):
        _read_samples(path)


def test_importance_sampling_requires_a_non_empty_weights_array(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "sampling": {"strategy": "importance", "weights": []},
        },
    )
    with pytest.raises(YieldError, match="weights must be a non-empty array"):
        _read_samples(path)


# --------------------------------------------------------------------------- #
# Statistics (native extension required)
# --------------------------------------------------------------------------- #


@requires_native
def test_yield_matches_the_closed_form_two_sigma_case(tmp_path):
    """A standard normal against symmetric +/-2 sigma limits has an analytic
    yield of `erf(sqrt(2)) = 0.9544997...` -- the closed-form case issue
    #816's fourth acceptance criterion asks for, checked here through the
    full CLI-facing path rather than only inside the crate."""
    analytic = math.erf(math.sqrt(2.0))
    path = _sample_set(
        tmp_path, _normal_grid(4000, 0.0, 1.0), limits={"min": -2.0, "max": 2.0}
    )
    report = run_yield(path)
    m = report["measurements"][0]

    assert m["yield"]["normal"]["estimate"] == pytest.approx(analytic, abs=5e-4)
    assert m["yield"]["empirical"]["estimate"] == pytest.approx(analytic, abs=2e-3)
    ci = m["yield"]["empirical"]["confidence_interval"]
    assert ci["low"] <= analytic <= ci["high"]
    # Cpk for a symmetric +/-2 sigma window is 2/3; sigma-to-spec is 2.
    assert m["capability"]["cpk"] == pytest.approx(2.0 / 3.0, abs=5e-3)
    assert m["capability"]["sigma_to_spec"] == pytest.approx(2.0, abs=1.5e-2)
    assert m["distribution"]["normality"]["verdict"] == "consistent"


@requires_native
def test_every_yield_number_carries_an_interval_and_a_sample_count(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(64, 0.0, 1.0), limits={"min": -3.0, "max": 3.0}
    )
    report = run_yield(path)
    for m in report["measurements"]:
        for key in ("empirical", "normal"):
            estimate = m["yield"][key]
            assert estimate is not None
            assert estimate["n"] == 64
            assert set(estimate["confidence_interval"]) == {"low", "high"}
            assert math.isfinite(estimate["confidence_interval"]["low"])
            assert math.isfinite(estimate["confidence_interval"]["high"])
            assert estimate["confidence"] == 0.95


@requires_native
def test_a_single_sample_is_an_error_not_a_bare_point_estimate(tmp_path):
    path = _sample_set(tmp_path, [1.2], limits={"min": 1.0, "max": 1.5})
    with pytest.raises(YieldError, match="cannot carry a confidence interval"):
        run_yield(path)


@requires_native
def test_a_confidence_of_one_is_an_error(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(50, 0.0, 1.0), limits={"min": -3.0, "max": 3.0}
    )
    with pytest.raises(YieldError, match="no finite interval"):
        run_yield(path, confidence=1.0)


@requires_native
def test_min_samples_below_the_hard_floor_is_an_error(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(50, 0.0, 1.0), limits={"min": -3.0, "max": 3.0}
    )
    with pytest.raises(YieldError, match="min_samples must be at least 2"):
        run_yield(path, min_samples=1)


@requires_native
def test_limits_file_wins_over_the_documents_own_limits(tmp_path):
    """The spec-limits file is the caller's explicit statement of the spec."""
    path = _sample_set(
        tmp_path,
        _normal_grid(200, 0.0, 1.0),
        limits={"min": -10.0, "max": 10.0},
        name="a",
    )
    limits = tmp_path / "limits.json"
    limits.write_text(json.dumps({"measurements": {"a": {"min": -1.0, "max": 1.0}}}))
    report = run_yield(path, limits_path=str(limits))
    m = report["measurements"][0]
    assert m["limits"] == {"min": -1.0, "max": 1.0}
    # Phi(1) - Phi(-1) = 0.6827.
    assert m["yield"]["empirical"]["estimate"] == pytest.approx(0.6827, abs=0.01)


@requires_native
def test_a_declared_target_yield_fails_when_the_lower_bound_misses_it(tmp_path):
    """The point estimate is 1.0, but 100 clean samples cannot *claim* 99% at
    95% confidence -- the whole point of the epic's discipline."""
    path = _sample_set(
        tmp_path,
        _normal_grid(100, 0.0, 1.0),
        limits={"min": -10.0, "max": 10.0, "target_yield": 0.99},
    )
    report = run_yield(path)
    m = report["measurements"][0]
    assert report["status"] == "fail"
    assert m["status"] == "fail"
    assert m["yield"]["empirical"]["estimate"] == 1.0
    assert m["yield"]["empirical"]["confidence_interval"]["low"] < 0.99
    # ln(0.025)/ln(0.99) = 367.03 -> 368.
    assert m["sample_size"]["required_n_for_target"] == 368
    assert m["sample_size"]["verdict"] == "insufficient"


@requires_native
def test_a_target_yield_below_the_observed_rate_passes(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(400, 0.0, 1.0),
        limits={"min": -10.0, "max": 10.0, "target_yield": 0.98},
    )
    report = run_yield(path)
    assert report["status"] == "pass"
    assert report["measurements"][0]["status"] == "pass"


@requires_native
def test_an_exclusive_min_spec_row_excludes_a_boundary_sample(tmp_path):
    """Issue #1083: a sample exactly at `min` fails an `exclusive_min` spec
    row, dropping the empirical yield below what a `target_yield` of 1.0
    would require -- the end-to-end version of the Rust `within()` unit
    tests, exercised through the CLI's JSON report."""
    samples = [2.0] * 19 + [0.0]  # the 20th sample sits exactly on `min`.
    path = _sample_set(
        tmp_path,
        samples,
        limits={"min": 0.0, "exclusive_min": True, "target_yield": 1.0},
    )
    report = run_yield(path)
    m = report["measurements"][0]
    assert m["limits"]["exclusive_min"] is True
    assert m["yield"]["empirical"]["estimate"] == pytest.approx(19 / 20)
    assert m["status"] == "fail"
    assert report["status"] == "fail"

    # The regression guard: the identical sample set with the default
    # (inclusive) comparison passes every sample, including the boundary one.
    inclusive_path = _sample_set(
        tmp_path,
        samples,
        limits={"min": 0.0, "target_yield": 1.0},
        name="m2",
    )
    inclusive_report = run_yield(inclusive_path)
    m2 = inclusive_report["measurements"][0]
    assert not m2["limits"].get("exclusive_min")
    assert m2["yield"]["empirical"]["estimate"] == 1.0


@requires_native
def test_exclusive_min_with_no_min_value_raises_the_native_validation_error(tmp_path):
    """Declaring `exclusive_min` on a side with no `min` value is a
    validation error (mirrors the existing 'max <= min' check), not silently
    ignored."""
    path = _sample_set(
        tmp_path,
        _normal_grid(50, 0.0, 1.0),
        limits={"max": 3.0, "exclusive_min": True},
    )
    with pytest.raises(YieldError, match="exclusive_min"):
        run_yield(path)


@requires_native
def test_exclusive_max_with_no_max_value_raises_the_native_validation_error(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(50, 0.0, 1.0),
        limits={"min": -3.0, "exclusive_max": True},
    )
    with pytest.raises(YieldError, match="exclusive_max"):
        run_yield(path)


@requires_native
def test_sim_report_and_sample_set_agree_on_the_same_draw():
    """The two input shapes over the identical draw must produce identical
    statistics -- the guarantee that consuming the canary MC record format
    directly costs nothing."""
    from_samples = run_yield(
        os.path.join(EXAMPLES, "mc-samples.json"),
        limits_path=os.path.join(EXAMPLES, "spec-limits.json"),
    )
    from_sim = run_yield(
        os.path.join(EXAMPLES, "sim-report.json"),
        limits_path=os.path.join(EXAMPLES, "spec-limits.json"),
    )
    assert from_samples["measurements"] == [
        {**m, "source_corners": []} for m in from_sim["measurements"]
    ]


# --------------------------------------------------------------------------- #
# Errored samples and the conditional yield (issue #1082)
# --------------------------------------------------------------------------- #


@requires_native
def test_errored_samples_warn_that_the_empirical_yield_is_conditional(tmp_path):
    """The acute case issue #1082 reports: every sample that produced a value
    is inside the limits, so the empirical yield is 1.0 -- but most of the
    draw produced no value at all, and nothing used to say so."""
    samples = [1.0 + 0.001 * i for i in range(20)] + [None] * 60
    path = _sample_set(tmp_path, samples, limits={"min": 0.0, "max": 2.0})
    report = run_yield(path)
    m = report["measurements"][0]

    assert m["n"] == 20
    assert m["errored"] == 60
    assert m["yield"]["empirical"]["estimate"] == 1.0

    # The pre-existing exclusion warning, and the new conditional-yield one
    # alongside it -- two distinct messages, not a reworded single one.
    assert any("excluded from every statistic below" in w for w in m["warnings"])
    conditional = [w for w in m["warnings"] if "conditional on the" in w]
    assert len(conditional) == 1, m["warnings"]
    # 20 passing of 80 drawn = 0.25 if every errored sample were a failure.
    assert "60 of the 80" in conditional[0]
    assert "0.250000" in conditional[0]
    assert "#errored-samples-and-conditional-yield" in conditional[0]

    # And the run level points a reader at it.
    assert any("excluded errored sample" in w for w in report["warnings"])


@requires_native
def test_a_draw_with_no_errored_samples_gets_no_conditional_warning(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(200, 0.0, 1.0), limits={"min": -2.0, "max": 2.0}
    )
    report = run_yield(path)
    assert report["measurements"][0]["errored"] == 0
    assert not [
        w for w in report["measurements"][0]["warnings"] if "conditional on the" in w
    ]
    assert not [w for w in report["warnings"] if "excluded errored sample" in w]


@requires_native
def test_an_entirely_errored_draw_is_an_error_that_names_the_errored_count(tmp_path):
    """`errored == n + errored`: no usable sample survives, so there is no
    report to warn in -- the sample-floor error has to say the draw failed to
    *measure* rather than reporting it as merely small."""
    path = _sample_set(tmp_path, [None] * 40, limits={"min": 0.0, "max": 2.0})
    with pytest.raises(YieldError, match=r"40 of the 40 sample\(s\)"):
        run_yield(path)


@requires_native
def test_cli_text_output_surfaces_the_conditional_yield_warning(tmp_path, capsys):
    samples = [1.0 + 0.001 * i for i in range(20)] + [None] * 60
    path = _sample_set(tmp_path, samples, limits={"min": 0.0, "max": 2.0})
    assert main(["yield", path]) == 0
    out = capsys.readouterr().out
    assert "conditional on the 20 sample(s) that produced one" in out
    assert "excluded errored samples from its denominator" in out


# --------------------------------------------------------------------------- #
# `failed_unmeasurable` -- a design failure with no value (issue #1095)
# --------------------------------------------------------------------------- #


@requires_native
def test_failed_unmeasurable_draws_count_as_failures_in_the_empirical_yield(tmp_path):
    """Unlike `errored`, `failed_unmeasurable` draws are *not* excluded from
    the empirical yield -- they enter its numerator's complement and
    denominator as failures."""
    entry = {
        "name": "m",
        "samples": [1.0] * 20,
        "failed_unmeasurable": 5,
        "limits": {"min": 0.0, "max": 2.0},
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]

    assert m["n"] == 20
    assert m["failed_unmeasurable"] == 5
    # The population backing distribution/capability is still 20 -- the
    # failed_unmeasurable draws never touch it.
    assert m["distribution"]["mean"] == 1.0
    # But the empirical yield's own `n` is 25 (20 + 5), and the estimate is
    # 20/25, not 20/20.
    assert m["yield"]["empirical"]["n"] == 25
    assert abs(m["yield"]["empirical"]["estimate"] - 20 / 25) < 1e-12

    assert any(
        "failed_unmeasurable" in w and "counted as failing" in w for w in m["warnings"]
    )
    assert any("counted failed_unmeasurable draws" in w for w in report["warnings"])


@requires_native
def test_a_draw_with_no_failed_unmeasurable_gets_no_extra_warning(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(200, 0.0, 1.0), limits={"min": -2.0, "max": 2.0}
    )
    report = run_yield(path)
    assert report["measurements"][0]["failed_unmeasurable"] == 0
    assert not [
        w for w in report["measurements"][0]["warnings"] if "failed_unmeasurable" in w
    ]
    assert not [
        w for w in report["warnings"] if "counted failed_unmeasurable draws" in w
    ]


@requires_native
def test_an_entirely_failed_unmeasurable_draw_does_not_crash_and_names_the_count(
    tmp_path,
):
    """100% failed_unmeasurable, no numeric samples: the distribution fit has
    nothing to fit, so this still errors at the sample floor -- the point is
    a clean error, not a crash."""
    entry = {
        "name": "m",
        "samples": [],
        "failed_unmeasurable": 40,
        "limits": {"min": 0.0, "max": 2.0},
    }
    path = _sample_set_doc(tmp_path, entry)
    with pytest.raises(YieldError, match="40 failed_unmeasurable"):
        run_yield(path)


# --------------------------------------------------------------------------- #
# `censored` -- a draw whose measurement precondition was never met
# (issue #2468)
# --------------------------------------------------------------------------- #


@requires_native
def test_censored_draws_are_excluded_from_the_empirical_yield(tmp_path):
    """Unlike `failed_unmeasurable`, `censored` draws get `errored`'s
    denominator treatment -- excluded from both the numerator and
    denominator of the empirical yield."""
    entry = {
        "name": "m",
        "samples": [1.0] * 20,
        "censored": 5,
        "limits": {"min": 0.0, "max": 2.0},
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]

    assert m["n"] == 20
    assert m["censored"] == 5
    assert m["distribution"]["mean"] == 1.0
    # The empirical yield's own `n` stays 20 -- unlike failed_unmeasurable,
    # the censored draws never enter the denominator.
    assert m["yield"]["empirical"]["n"] == 20
    assert m["yield"]["empirical"]["estimate"] == 1.0

    assert any("censored" in w and "precondition" in w for w in m["warnings"])
    assert any(
        "excluded censored draws" in w and "precondition" in w
        for w in report["warnings"]
    )
    # The censored warning must not be mistaken for a tooling-failure one.
    assert not any("censored" in w and "tooling failure" in w for w in m["warnings"])


@requires_native
def test_a_draw_with_no_censored_gets_no_extra_warning(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(200, 0.0, 1.0), limits={"min": -2.0, "max": 2.0}
    )
    report = run_yield(path)
    assert report["measurements"][0]["censored"] == 0
    assert not [w for w in report["measurements"][0]["warnings"] if "censored" in w]
    assert not [w for w in report["warnings"] if "excluded censored draws" in w]


@requires_native
def test_an_entirely_censored_draw_does_not_crash_and_names_the_count(tmp_path):
    """100% censored, no numeric samples: like the equivalent
    failed_unmeasurable case, this still errors at the sample floor -- a
    clean error naming the count, not a crash."""
    entry = {
        "name": "m",
        "samples": [],
        "censored": 40,
        "limits": {"min": 0.0, "max": 2.0},
    }
    path = _sample_set_doc(tmp_path, entry)
    with pytest.raises(YieldError, match="40 censored"):
        run_yield(path)


# --------------------------------------------------------------------------- #
# negative_control / analytic_cross_check (issue #817)
# --------------------------------------------------------------------------- #


@requires_native
def test_a_seeded_known_bad_negative_control_is_detected(tmp_path):
    """The self-check issue #817 requires: a deliberately-degraded variant
    must show up as a *statistically distinguishable* worse yield, not just
    a lower point estimate."""
    entry = {
        "name": "vos",
        "samples": _normal_grid(300, 0.0, 0.05),
        "limits": {"min": -0.5, "max": 0.5},
        "negative_control": {
            "samples": _normal_grid(300, 0.6, 0.05),
            "description": "vos forced to 0.6 (12x the 0.05 spec sigma)",
        },
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    nc = m["negative_control"]
    assert nc["verdict"] == "detected"
    assert nc["description"] == "vos forced to 0.6 (12x the 0.05 spec sigma)"
    assert nc["yield"]["empirical"]["estimate"] < m["yield"]["empirical"]["estimate"]
    assert not any("not statistically" in w for w in report["warnings"])
    assert not any(
        "no measurement declared a negative_control" in w for w in report["warnings"]
    )


@requires_native
def test_a_negative_control_seeded_entirely_with_failed_unmeasurable_is_detected(
    tmp_path,
):
    """Issue #1095's core scenario: a deliberate defect that drives *every*
    negative-control draw out of the measurable regime, with no numeric
    failing samples at all. Before this issue, every such draw vanished
    into `errored` and the self-check reported `not_detected` even though
    the defect was working exactly as intended."""
    entry = {
        "name": "vos",
        "samples": _normal_grid(300, 0.0, 0.05),
        "limits": {"min": -0.5, "max": 0.5},
        "negative_control": {
            "samples": [],
            "failed_unmeasurable": 20,
            "description": "the deliberate defect drives every draw out of the "
            "measurable regime",
        },
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    nc = m["negative_control"]
    assert nc["n"] == 0
    assert nc["failed_unmeasurable"] == 20
    assert nc["yield"]["empirical"]["n"] == 20
    assert nc["yield"]["empirical"]["estimate"] == 0.0
    assert nc["yield"]["normal"] is None
    assert nc["verdict"] == "detected"
    assert not any("not statistically" in w for w in report["warnings"])


@requires_native
def test_a_negative_control_seeded_entirely_with_censored_errors_at_the_floor(
    tmp_path,
):
    """Issue #2468's opposite-polarity case from #1095's above: a deliberate
    defect that only ever drives negative-control draws into `censored`
    carries no information about a failure either way, so there is no
    `"detected"` verdict to report -- `censored` is deliberately excluded
    from the floor check, so this falls through to the same
    below-minimum-samples error the nominal measurement would raise."""
    entry = {
        "name": "vos",
        "samples": _normal_grid(300, 0.0, 0.05),
        "limits": {"min": -0.5, "max": 0.5},
        "negative_control": {
            "samples": [],
            "censored": 20,
            "description": "the deliberate defect only ever fails its own "
            "precondition, never a real failure",
        },
    }
    path = _sample_set_doc(tmp_path, entry)
    with pytest.raises(YieldError, match="20 censored"):
        run_yield(path)


@requires_native
def test_a_negative_control_that_does_not_degrade_is_flagged(tmp_path):
    """A "negative control" drawn from the same distribution as the nominal
    measurement is a broken self-check (the defect was never injected) --
    `klt yield` must flag this rather than accept it silently."""
    samples = _normal_grid(300, 0.0, 0.05)
    entry = {
        "name": "vos",
        "samples": samples,
        "limits": {"min": -0.5, "max": 0.5},
        "negative_control": {"samples": list(samples)},
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    assert m["negative_control"]["verdict"] == "not_detected"
    assert any("not statistically distinguishable" in w for w in m["warnings"])
    assert any("did not show the expected degradation" in w for w in report["warnings"])


@requires_native
def test_a_campaign_without_any_negative_control_is_flagged(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(200, 0.0, 1.0), limits={"min": -3.0, "max": 3.0}
    )
    report = run_yield(path)
    assert report["measurements"][0]["negative_control"] is None
    assert any(
        "no measurement declared a negative_control" in w for w in report["warnings"]
    )


@requires_native
def test_ktc_noise_analytic_cross_check_matches_a_synthetic_draw(tmp_path):
    """`sigma = sqrt(kB * T / C)` for a 1 pF cap at 300 K -- the closed-form
    case issue #817's second acceptance criterion asks for."""
    boltzmann = 1.380_649e-23
    capacitance_f = 1.0e-12
    temperature_k = 300.0
    sigma = math.sqrt(boltzmann * temperature_k / capacitance_f)
    entry = {
        "name": "vn",
        "samples": _normal_grid(2000, 0.0, sigma),
        "limits": {"min": -5 * sigma, "max": 5 * sigma},
        "analytic_cross_check": {
            "kind": "ktc_noise",
            "capacitance_f": capacitance_f,
            "temperature_k": temperature_k,
        },
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    check = report["measurements"][0]["analytic_cross_check"]
    assert check["kind"] == "ktc_noise"
    assert check["verdict"] == "consistent"
    assert check["analytic_stddev"] == pytest.approx(sigma, rel=1e-9)
    assert abs(check["stddev_relative_delta"]) < 0.01


@requires_native
def test_mismatch_offset_analytic_cross_check_flags_a_disagreement(tmp_path):
    """A draw whose actual spread is nowhere near the claimed sigma is the
    discrepancy an analytic cross-check exists to catch."""
    entry = {
        "name": "vos",
        "samples": _normal_grid(2000, 0.0, 0.001),
        "limits": {"min": -0.05, "max": 0.05},
        "analytic_cross_check": {"kind": "mismatch_offset", "sigma": 0.010},
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    check = m["analytic_cross_check"]
    assert check["verdict"] == "inconsistent"
    assert any("not consistent with the mismatch_offset" in w for w in m["warnings"])


# --------------------------------------------------------------------------- #
# Variance-reduction sampling strategies (issue #907, Phase 2b of epic #710)
# --------------------------------------------------------------------------- #


@requires_native
def test_omitting_sampling_reports_plain_random_with_no_variance_reduced_estimate(
    tmp_path,
):
    path = _sample_set(
        tmp_path, _normal_grid(300, 0.0, 1.0), limits={"min": -3.0, "max": 3.0}
    )
    report = run_yield(path)
    m = report["measurements"][0]
    assert m["sampling"] == {
        "strategy": "plain_random",
        "replicates": None,
        "effective_sample_size": None,
    }
    assert m["yield"]["variance_reduced"] is None
    assert m["sample_size"]["variance_reduced"] is None


@requires_native
def test_latin_hypercube_sampling_strategy_and_verdict_are_surfaced_in_the_report(
    tmp_path,
):
    """Issue #907's JSON-contract requirement: the sampling strategy and its
    effect on the CI/sample-size verdict must be visible in the payload, not
    just the point estimate."""
    entry = {
        "name": "vref",
        # 4 replicates of an exact quantile grid -- deterministic, so this
        # test only checks the JSON shape end to end; the Rust crate's own
        # tests (native/yield/src/estimate.rs) carry the statistical
        # variance-reduction proof against a genuinely random draw.
        "samples": _normal_grid(400, 0.0, 1.0),
        "limits": {"min": -2.0, "max": 2.0},
        "sampling": {"strategy": "latin_hypercube", "replicates": 4},
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    assert m["sampling"]["strategy"] == "latin_hypercube"
    assert m["sampling"]["replicates"] == 4
    assert m["yield"]["variance_reduced"]["method"] == "lhs-replicated"
    assert m["yield"]["variance_reduced"]["n"] == 400
    assert m["sample_size"]["variance_reduced"] is not None
    assert m["sample_size"]["variance_reduced"]["verdict"] in (
        "sufficient",
        "insufficient",
    )


@requires_native
def test_importance_sampling_with_equal_weights_matches_the_plain_empirical_estimate(
    tmp_path,
):
    samples = _normal_grid(300, 0.0, 1.0)
    entry = {
        "name": "vref",
        "samples": samples,
        "limits": {"min": -2.0, "max": 2.0},
        "sampling": {"strategy": "importance", "weights": [2.0] * len(samples)},
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    assert m["sampling"]["strategy"] == "importance"
    assert m["sampling"]["effective_sample_size"] == pytest.approx(300.0, rel=1e-6)
    assert m["yield"]["variance_reduced"]["method"] == "importance-weighted"
    assert m["yield"]["variance_reduced"]["estimate"] == pytest.approx(
        m["yield"]["empirical"]["estimate"], rel=1e-9
    )


@requires_native
def test_latin_hypercube_replicates_below_two_is_an_error(tmp_path):
    # Python's own input-reader rejects this before it ever reaches the
    # native core (see test_latin_hypercube_sampling_requires_replicates_at_least_two
    # above); this test is the end-to-end confirmation via `run_yield`.
    entry = {
        "name": "vref",
        "samples": [1.0, 2.0],
        "limits": {"max": 5.0},
        "sampling": {"strategy": "latin_hypercube", "replicates": 1},
    }
    path = _sample_set_doc(tmp_path, entry)
    with pytest.raises(YieldError, match="replicates must be an integer >= 2"):
        run_yield(path)


@requires_native
def test_importance_weights_length_mismatch_is_an_error(tmp_path):
    entry = {
        "name": "vref",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "sampling": {"strategy": "importance", "weights": [1.0, 1.0]},
    }
    path = _sample_set_doc(tmp_path, entry)
    with pytest.raises(YieldError, match="sampling.weights has 2 entries"):
        run_yield(path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@requires_native
def test_cli_json_envelope_and_exit_code(tmp_path, capsys):
    path = _sample_set(
        tmp_path,
        _normal_grid(400, 0.0, 1.0),
        limits={"min": -10.0, "max": 10.0, "target_yield": 0.98},
    )
    assert main(["yield", path, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["status"] == "pass"
    assert payload["measurement_count"] == 1


@requires_native
def test_cli_exits_3_when_a_yield_claim_is_not_supported(tmp_path, capsys):
    path = _sample_set(
        tmp_path,
        _normal_grid(100, 0.0, 1.0),
        limits={"min": -10.0, "max": 10.0, "target_yield": 0.99},
    )
    assert main(["yield", path, "--format", "json"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "fail"


@requires_native
def test_cli_text_output_always_shows_the_interval(tmp_path, capsys):
    path = _sample_set(
        tmp_path, _normal_grid(200, 0.0, 1.0), limits={"min": -2.0, "max": 2.0}
    )
    assert main(["yield", path]) == 0
    out = capsys.readouterr().out
    assert "yield (empirical, clopper-pearson):" in out
    assert "at 95%, N=200" in out
    assert "sample size:" in out
    assert "negative control: none declared" in out
    assert "no measurement declared a negative_control" in out


@requires_native
def test_cli_text_output_shows_the_negative_control_and_analytic_cross_check(
    tmp_path, capsys
):
    entry = {
        "name": "vos",
        "samples": _normal_grid(300, 0.0, 0.05),
        "limits": {"min": -0.5, "max": 0.5},
        "negative_control": {
            "samples": _normal_grid(300, 0.6, 0.05),
            "description": "forced offset",
        },
        "analytic_cross_check": {"kind": "mismatch_offset", "sigma": 0.05},
    }
    path = _sample_set_doc(tmp_path, entry)
    assert main(["yield", path]) == 0
    out = capsys.readouterr().out
    assert "negative control (forced offset): detected" in out
    assert "analytic cross-check (mismatch_offset): consistent" in out


def test_cli_error_uses_the_json_error_envelope(tmp_path, capsys):
    assert main(["yield", str(tmp_path / "nope.json"), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "yield"
    assert "not found" in error["error"]["message"]


@requires_native
def test_cli_measurement_filter_accepts_repeats_and_commas(tmp_path, capsys):
    path = tmp_path / "samples.json"
    path.write_text(
        json.dumps(
            {
                "measurements": [
                    {
                        "name": name,
                        "samples": _normal_grid(50, 0.0, 1.0),
                        "limits": {"min": -3.0, "max": 3.0},
                    }
                    for name in ("a", "b", "c")
                ]
            }
        )
    )
    assert main(["yield", str(path), "--measurement", "a,c", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in payload["measurements"]] == ["a", "c"]


# --------------------------------------------------------------------------- #
# Worked example
# --------------------------------------------------------------------------- #


def test_examples_regenerate_byte_identically(tmp_path):
    """`examples/yield/generate.py` is seeded, so the committed fixtures must
    round-trip exactly -- otherwise the numbers quoted in docs/cli/yield.md
    would drift silently."""
    before = {
        name: open(os.path.join(EXAMPLES, name)).read()
        for name in ("mc-samples.json", "spec-limits.json", "sim-report.json")
    }
    subprocess.run(
        [sys.executable, os.path.join(EXAMPLES, "generate.py")],
        check=True,
        capture_output=True,
    )
    after = {name: open(os.path.join(EXAMPLES, name)).read() for name in before}
    assert after == before


@requires_native
def test_worked_example_matches_the_documented_numbers():
    """The exact figures docs/cli/yield.md's worked example quotes."""
    report = run_yield(
        os.path.join(EXAMPLES, "mc-samples.json"),
        limits_path=os.path.join(EXAMPLES, "spec-limits.json"),
    )
    assert report["status"] == "fail"
    vref = next(m for m in report["measurements"] if m["name"] == "vref")
    assert vref["n"] == 300
    assert vref["yield"]["empirical"]["estimate"] == 1.0
    assert vref["yield"]["empirical"]["confidence_interval"]["low"] == pytest.approx(
        0.987779, abs=1e-6
    )
    assert vref["yield"]["normal"]["estimate"] == pytest.approx(0.998172, abs=1e-6)
    assert vref["capability"]["cpk"] == pytest.approx(1.0122, abs=1e-4)
    assert vref["capability"]["sigma_to_spec"] == pytest.approx(3.0365, abs=1e-4)
    assert vref["sample_size"]["required_n_for_target"] == 368
    assert vref["distribution"]["normality"]["verdict"] == "consistent"

    iq = next(m for m in report["measurements"] if m["name"] == "iq_ua")
    assert iq["limits"] == {"max": 10.0, "target_yield": 0.99}
    assert iq["capability"]["cp"] is None
    assert iq["capability"]["limiting_side"] == "upper"
    assert iq["sample_size"]["required_n_for_target"] == 874


# --------------------------------------------------------------------------- #
# `klt yield-samples`: derive a sample set from two sim reports (issue #2563)
#
# Input-tier tests (no native extension) pin the derived document against a
# hand-authored equivalent; the `requires_native` tests then prove the
# statistics are identical to analysing the nominal report directly.
# --------------------------------------------------------------------------- #

_DET_CORNER = "tt/1.800V/27C"


def _mc_report_doc(rollup, draws, *, origin, deterministic=None):
    """A `klt sim` MC report: ``draws`` is a list of
    ``(corner_status, {name: (value, measurement_status)})`` -- one sampled
    corner each -- and ``deterministic`` an optional ``{name: value}`` for a
    plain (non-sampled) PVT corner that must never enter a population."""
    corners = []
    if deterministic is not None:
        corners.append(
            {
                "corner_id": _DET_CORNER,
                "status": "pass",
                "measurements": [
                    {"name": n, "value": v, "status": "pass"}
                    for n, v in deterministic.items()
                ],
                "monte_carlo": None,
            }
        )
    for i, (corner_status, values) in enumerate(draws):
        corners.append(
            {
                "corner_id": f"{origin}/mc{i}",
                "status": corner_status,
                "measurements": [
                    {"name": n, "value": v, "status": s} for n, (v, s) in values.items()
                ],
                "monte_carlo": {"sample_index": i, "seed": 7000 + i},
            }
        )
    return {
        "schema_version": 3,
        "netlist": {"path": "tb.spice", "scope": "repo"},
        "status": "pass",
        "environment": {"monte_carlo": {"n": len(draws), "seed": 7000}},
        "measurements": rollup,
        "corners": corners,
    }


_NOMINAL_VOS = _normal_grid(200, 0.0, 0.05)
_CONTROL_VOS = _normal_grid(200, 0.6, 0.05)


def _nominal_doc():
    """Nominal campaign: mixed deterministic + sampled corners, a null draw,
    measurement-level and corner-level distrust (including both on one
    draw, and distrust on a null value), plus rollup-supplied
    failed_unmeasurable/censored counts and an analytic cross-check."""
    draws = []
    for i, vos in enumerate(_NOMINAL_VOS):
        corner_status, vos_value, vos_status = "pass", vos, "pass"
        if i == 0:
            vos_value = None  # errored
        elif i == 1:
            vos_value, vos_status = 9.0e9, "inconclusive"  # measurement level
        elif i == 2:
            corner_status = "inconclusive"  # corner level
        elif i == 3:
            corner_status, vos_status = "inconclusive", "inconclusive"  # both
        elif i == 4:
            vos_value, vos_status = None, "inconclusive"  # distrusted null
        draws.append(
            (
                corner_status,
                {"vos": (vos_value, vos_status), "iq": (5.0 + 0.01 * i, "pass")},
            )
        )
    rollup = [
        {
            "name": "vos",
            "unit": "V",
            "limits": {"min": -0.5, "max": 0.5},
            "failed_unmeasurable": 2,
            "censored": 1,
            "analytic_cross_check": {"kind": "mismatch_offset", "sigma": 0.05},
        },
        {"name": "iq", "unit": "uA", "limits": {"max": 10.0}},
    ]
    return _mc_report_doc(
        rollup,
        draws,
        origin=_DET_CORNER,
        deterministic={"vos": 99.0, "iq": 99.0},
    )


def _control_doc():
    """Known-bad variant: its own (wider) limits that must be ignored, a
    reordered rollup with an extra measurement, and its own exclusions."""
    draws = []
    for i, vos in enumerate(_CONTROL_VOS):
        corner_status, vos_value, vos_status = "pass", vos, "pass"
        if i == 0:
            vos_value = None
        elif i == 1:
            vos_value, vos_status = 9.0e9, "inconclusive"
        elif i == 2:
            corner_status = "inconclusive"
        draws.append(
            (
                corner_status,
                {
                    "vos": (vos_value, vos_status),
                    "iq": (15.0 + 0.01 * i, "pass"),
                    "extra": (1.0, "pass"),
                },
            )
        )
    rollup = [
        {"name": "extra", "unit": "s"},
        {"name": "iq", "unit": "uA", "limits": {"max": 1000.0}},
        {
            "name": "vos",
            "unit": "V",
            "limits": {"min": -100.0, "max": 100.0},
            "failed_unmeasurable": 3,
            "censored": 2,
        },
    ]
    return _mc_report_doc(
        rollup, draws, origin="ss/1.620V/125C", deterministic={"vos": -99.0}
    )


def _write_json(path, doc):
    path.write_text(json.dumps(doc))
    return str(path)


def _derivation_inputs(tmp_path, nominal=None, control=None):
    return (
        _write_json(tmp_path / "nominal.json", nominal or _nominal_doc()),
        _write_json(tmp_path / "control.json", control or _control_doc()),
    )


def _hand_authored_measurements(description=None):
    """What a careful caller would have had to extract by hand -- the
    sample-set equivalent of the two fixture reports above."""
    iq_nominal = [5.0 + 0.01 * i for i in range(200) if i not in (2, 3)]
    iq_control = [15.0 + 0.01 * i for i in range(200) if i != 2]
    return [
        {
            "name": "vos",
            "unit": "V",
            "samples": _NOMINAL_VOS[5:],
            "errored": 1,
            "inconclusive": 4,
            "failed_unmeasurable": 2,
            "censored": 1,
            "limits": {"min": -0.5, "max": 0.5},
            "source_corners": [_DET_CORNER],
            "negative_control": {
                "samples": _CONTROL_VOS[3:],
                "errored": 1 + 2,
                "failed_unmeasurable": 3,
                "censored": 2,
                "description": description,
            },
            "analytic_cross_check": {"kind": "mismatch_offset", "sigma": 0.05},
            "sampling": None,
        },
        {
            "name": "iq",
            "unit": "uA",
            "samples": iq_nominal,
            "errored": 0,
            "inconclusive": 2,
            "failed_unmeasurable": 0,
            "censored": 0,
            "limits": {"max": 10.0},
            "source_corners": [_DET_CORNER],
            "negative_control": {
                "samples": iq_control,
                "errored": 0 + 1,
                "failed_unmeasurable": 0,
                "censored": 0,
                "description": description,
            },
            "analytic_cross_check": None,
            "sampling": None,
        },
    ]


def _sha256(path):
    with open(path, "rb") as f:
        return "sha256:" + hashlib.sha256(f.read()).hexdigest()


def test_derived_sample_set_matches_a_hand_authored_equivalent(tmp_path):
    nominal, control = _derivation_inputs(tmp_path)
    doc = derive_sample_set(nominal, control, description="vos forced to 0.6 V")
    assert doc["schema_version"] == 1
    assert doc["measurements"] == _hand_authored_measurements("vos forced to 0.6 V")


def test_derived_sample_set_excludes_deterministic_corners_from_both_populations(
    tmp_path,
):
    nominal, control = _derivation_inputs(tmp_path)
    doc = derive_sample_set(nominal, control)
    vos = doc["measurements"][0]
    assert 99.0 not in vos["samples"]
    assert -99.0 not in vos["negative_control"]["samples"]
    assert len(vos["samples"]) == 195
    assert len(vos["negative_control"]["samples"]) == 197


def test_derivation_metadata_records_source_hashes_and_control_disclosures(tmp_path):
    nominal, control = _derivation_inputs(tmp_path)
    doc = derive_sample_set(nominal, control, description="seeded defect")
    derivation = doc["derivation"]
    assert derivation["nominal"] == {"path": nominal, "content_hash": _sha256(nominal)}
    assert derivation["negative_control"] == {
        "path": control,
        "content_hash": _sha256(control),
        "description": "seeded defect",
    }
    assert derivation["measurements"] == [
        {
            "name": "vos",
            "negative_control": {
                "source_corners": ["ss/1.620V/125C"],
                "errored": 1,
                "inconclusive": 2,
                "errored_reported": 3,
            },
        },
        {
            "name": "iq",
            "negative_control": {
                "source_corners": ["ss/1.620V/125C"],
                "errored": 0,
                "inconclusive": 1,
                "errored_reported": 1,
            },
        },
    ]


def test_derivation_is_deterministic_and_never_modifies_its_inputs(tmp_path):
    nominal, control = _derivation_inputs(tmp_path)
    before = (open(nominal, "rb").read(), open(control, "rb").read())
    first = json.dumps(derive_sample_set(nominal, control, description="d"))
    second = json.dumps(derive_sample_set(nominal, control, description="d"))
    assert first == second
    assert (open(nominal, "rb").read(), open(control, "rb").read()) == before


def test_derived_document_reads_back_as_a_sample_set(tmp_path):
    """The flat payload *is* the sample-set document: `klt yield`'s reader
    takes it unchanged, ignoring `schema_version`/`derivation`."""
    nominal, control = _derivation_inputs(tmp_path)
    derived = _write_json(
        tmp_path / "samples.json", derive_sample_set(nominal, control)
    )
    kind, entries, _ = _read_samples(derived)
    assert kind == "sample-set"
    direct = {e["name"]: e for e in _read_samples(nominal)[1]}
    for entry in entries:
        expected = {**direct[entry["name"]]}
        assert entry["negative_control"] is not None
        assert {k: v for k, v in entry.items() if k != "negative_control"} == {
            k: v for k, v in expected.items() if k != "negative_control"
        }


def test_measurement_selection_follows_the_comma_list_convention(tmp_path, capsys):
    nominal, control = _derivation_inputs(tmp_path)
    doc = derive_sample_set(nominal, control, measurements=["iq"])
    assert [m["name"] for m in doc["measurements"]] == ["iq"]
    assert [m["name"] for m in doc["derivation"]["measurements"]] == ["iq"]

    assert (
        main(
            [
                "yield-samples",
                nominal,
                "--negative-control",
                control,
                "--measurement",
                "iq,vos",
                "--measurement",
                "iq",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in payload["measurements"]] == ["iq", "vos"]


def test_selection_of_an_unknown_measurement_is_an_error(tmp_path):
    nominal, control = _derivation_inputs(tmp_path)
    with pytest.raises(YieldError, match="no such measurement in nominal.*nope"):
        derive_sample_set(nominal, control, measurements=["nope"])


def test_a_measurement_missing_from_the_control_is_an_error_unless_deselected(
    tmp_path,
):
    control_doc = _control_doc()
    control_doc["measurements"] = [
        m for m in control_doc["measurements"] if m["name"] != "iq"
    ]
    nominal, control = _derivation_inputs(tmp_path, control=control_doc)
    with pytest.raises(YieldError, match="no measurement named 'iq'.*--measurement"):
        derive_sample_set(nominal, control)
    doc = derive_sample_set(nominal, control, measurements=["vos"])
    assert [m["name"] for m in doc["measurements"]] == ["vos"]


def test_a_duplicate_companion_measurement_is_an_error(tmp_path):
    control_doc = _control_doc()
    control_doc["measurements"].append({"name": "vos", "unit": "V"})
    nominal, control = _derivation_inputs(tmp_path, control=control_doc)
    with pytest.raises(YieldError, match="'vos' 2 times.*must be unique"):
        derive_sample_set(nominal, control)


def test_a_duplicate_nominal_measurement_is_an_error(tmp_path):
    nominal_doc = _nominal_doc()
    nominal_doc["measurements"].append({"name": "iq", "unit": "uA"})
    nominal, control = _derivation_inputs(tmp_path, nominal=nominal_doc)
    with pytest.raises(YieldError, match="'iq' 2 times"):
        derive_sample_set(nominal, control)


def test_a_unit_mismatch_is_an_error(tmp_path):
    control_doc = _control_doc()
    for m in control_doc["measurements"]:
        if m["name"] == "vos":
            m["unit"] = "mV"
    nominal, control = _derivation_inputs(tmp_path, control=control_doc)
    with pytest.raises(YieldError, match="unit mismatch.*'V'.*'mV'"):
        derive_sample_set(nominal, control)


def test_absent_units_on_both_sides_match(tmp_path):
    nominal_doc, control_doc = _nominal_doc(), _control_doc()
    for doc in (nominal_doc, control_doc):
        for m in doc["measurements"]:
            m.pop("unit", None)
    nominal, control = _derivation_inputs(tmp_path, nominal_doc, control_doc)
    doc = derive_sample_set(nominal, control)
    assert [m["unit"] for m in doc["measurements"]] == [None, None]


def test_an_absent_unit_does_not_match_a_present_one(tmp_path):
    control_doc = _control_doc()
    for m in control_doc["measurements"]:
        m.pop("unit", None)
    nominal, control = _derivation_inputs(tmp_path, control=control_doc)
    with pytest.raises(YieldError, match="unit mismatch"):
        derive_sample_set(nominal, control)


def test_an_existing_negative_control_is_never_silently_replaced(tmp_path):
    nominal_doc = _nominal_doc()
    nominal_doc["measurements"][0]["negative_control"] = {"samples": [1.0, 2.0]}
    nominal, control = _derivation_inputs(tmp_path, nominal=nominal_doc)
    with pytest.raises(YieldError, match="already declares a negative_control"):
        derive_sample_set(nominal, control)
    # ...but excluding that measurement lets the rest derive.
    doc = derive_sample_set(nominal, control, measurements=["iq"])
    assert [m["name"] for m in doc["measurements"]] == ["iq"]


def test_invalid_json_is_an_error_naming_the_file(tmp_path):
    nominal, _ = _derivation_inputs(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(YieldError, match="could not read negative-control"):
        derive_sample_set(nominal, str(bad))


def test_a_missing_input_is_an_error(tmp_path):
    nominal, _ = _derivation_inputs(tmp_path)
    with pytest.raises(YieldError, match="negative-control sim report not found"):
        derive_sample_set(nominal, str(tmp_path / "nope.json"))


def test_a_sample_set_document_is_not_accepted_as_an_input(tmp_path):
    _, control = _derivation_inputs(tmp_path)
    sample_set = _sample_set(tmp_path, [1.0, 2.0], limits={"max": 3.0})
    with pytest.raises(YieldError, match="nominal .* is not a `klt sim"):
        derive_sample_set(sample_set, control)


def test_a_report_without_monte_carlo_corners_is_an_error(tmp_path):
    control_doc = _control_doc()
    control_doc["corners"] = [c for c in control_doc["corners"] if not c["monte_carlo"]]
    nominal, control = _derivation_inputs(tmp_path, control=control_doc)
    with pytest.raises(
        YieldError, match="negative-control .*declares no Monte Carlo samples"
    ):
        derive_sample_set(nominal, control)


def test_an_empty_control_population_is_an_error(tmp_path):
    """A companion that names the measurement in its rollup but whose
    sampled corners never report it has no draw at all -- not a valid,
    empty negative control."""
    control_doc = _control_doc()
    for corner in control_doc["corners"]:
        corner["measurements"] = [
            m for m in corner["measurements"] if m["name"] != "iq"
        ]
    nominal, control = _derivation_inputs(tmp_path, control=control_doc)
    with pytest.raises(YieldError, match="no Monte Carlo draws for measurement 'iq'"):
        derive_sample_set(nominal, control)


def test_an_empty_nominal_population_is_an_error(tmp_path):
    nominal_doc = _nominal_doc()
    nominal_doc["measurements"].append({"name": "extra", "unit": "s"})
    nominal, control = _derivation_inputs(tmp_path, nominal=nominal_doc)
    with pytest.raises(YieldError, match="nominal .*no Monte Carlo draws.*'extra'"):
        derive_sample_set(nominal, control)


def test_a_control_population_of_only_rollup_counts_is_kept(tmp_path):
    """A defect so effective every draw is `failed_unmeasurable` is a valid
    control (docs/cli/yield.md's Negative control); the derivation must not
    mistake 'no numeric samples' for 'no draws'."""
    control_doc = _control_doc()
    for corner in control_doc["corners"]:
        corner["measurements"] = [
            m for m in corner["measurements"] if m["name"] != "iq"
        ]
    for m in control_doc["measurements"]:
        if m["name"] == "iq":
            m["failed_unmeasurable"] = 50
    nominal, control = _derivation_inputs(tmp_path, control=control_doc)
    doc = derive_sample_set(nominal, control)
    nc = doc["measurements"][1]["negative_control"]
    assert nc["samples"] == [] and nc["failed_unmeasurable"] == 50


def test_cli_json_success_is_the_flat_sample_set_payload(tmp_path, capsys):
    nominal, control = _derivation_inputs(tmp_path)
    before = (open(nominal, "rb").read(), open(control, "rb").read())
    argv = [
        "yield-samples",
        nominal,
        "--negative-control",
        control,
        "--description",
        "seeded defect",
        "--format",
        "json",
    ]
    assert main(argv) == 0
    first = capsys.readouterr()
    assert first.err == ""
    payload = json.loads(first.out)
    assert sorted(payload) == ["derivation", "measurements", "schema_version"]
    assert payload["schema_version"] == 1
    assert payload["measurements"] == _hand_authored_measurements("seeded defect")
    assert payload["derivation"]["negative_control"]["description"] == "seeded defect"
    # Stable bytes across runs, and the inputs are untouched.
    assert main(argv) == 0
    assert capsys.readouterr().out == first.out
    assert (open(nominal, "rb").read(), open(control, "rb").read()) == before


def test_cli_json_error_goes_to_stderr_with_empty_stdout(tmp_path, capsys):
    nominal, _ = _derivation_inputs(tmp_path)
    argv = [
        "yield-samples",
        nominal,
        "--negative-control",
        str(tmp_path / "missing.json"),
        "--format",
        "json",
    ]
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "yield-samples"
    assert "not found" in error["error"]["message"]


def test_cli_text_error_is_the_plain_stderr_line(tmp_path, capsys):
    nominal, _ = _derivation_inputs(tmp_path)
    assert (
        main(["yield-samples", nominal, "--negative-control", str(tmp_path / "x")]) == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("klt yield-samples: ")


def test_cli_negative_control_flag_is_required(tmp_path):
    nominal, _ = _derivation_inputs(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["yield-samples", nominal])
    assert exc.value.code == 2


def test_cli_text_rendering_shows_description_hashes_and_exclusions(tmp_path, capsys):
    nominal, control = _derivation_inputs(tmp_path)
    assert (
        main(
            [
                "yield-samples",
                nominal,
                "--negative-control",
                control,
                "--description",
                "vos forced to 0.6 V",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "description: vos forced to 0.6 V" in out
    assert _sha256(nominal) in out
    assert _sha256(control) in out
    assert "errored=3 (= 1 errored + 2 inconclusive)" in out


@requires_native
def test_derived_nominal_statistics_equal_direct_analysis_of_the_nominal_report(
    tmp_path,
):
    nominal, control = _derivation_inputs(tmp_path)
    derived = _write_json(
        tmp_path / "samples.json", derive_sample_set(nominal, control)
    )
    direct = run_yield(nominal)
    via_derived = run_yield(derived)
    assert direct["source"]["sample_count"] == via_derived["source"]["sample_count"]
    assert direct["status"] == via_derived["status"]
    for d, v in zip(direct["measurements"], via_derived["measurements"], strict=True):
        assert d["negative_control"] is None
        assert v["negative_control"] is not None
        assert {k: x for k, x in d.items() if k != "negative_control"} == {
            k: x for k, x in v.items() if k != "negative_control"
        }


@requires_native
def test_derived_control_grades_like_a_hand_authored_control_against_nominal_limits(
    tmp_path,
):
    nominal, control = _derivation_inputs(tmp_path)
    derived = _write_json(
        tmp_path / "samples.json",
        derive_sample_set(nominal, control, description="seeded"),
    )
    hand = _write_json(
        tmp_path / "hand.json",
        {"measurements": _hand_authored_measurements("seeded")},
    )
    via_derived = run_yield(derived)
    via_hand = run_yield(hand)
    for d, h in zip(via_derived["measurements"], via_hand["measurements"], strict=True):
        assert d["negative_control"] == h["negative_control"]
    vos_nc = via_derived["measurements"][0]["negative_control"]
    assert vos_nc["verdict"] == "detected"
    # Graded against the nominal +/-0.5 V limits, never the control's own
    # +/-100 V ones (which every control draw would pass).
    assert vos_nc["yield"]["empirical"]["estimate"] < 0.5
    # Inconclusive control draws are excluded from the denominator (folded
    # into errored), never counted as design failures.
    assert vos_nc["errored"] == 3
    assert vos_nc["failed_unmeasurable"] == 3
    assert vos_nc["censored"] == 2


@requires_native
def test_documented_two_command_pipeline_grades_a_negative_control(tmp_path, capsys):
    nominal, control = _derivation_inputs(tmp_path)
    before = (open(nominal, "rb").read(), open(control, "rb").read())
    assert (
        main(
            [
                "yield-samples",
                nominal,
                "--negative-control",
                control,
                "--description",
                "seeded defect",
                "--format",
                "json",
            ]
        )
        == 0
    )
    samples = tmp_path / "samples.json"
    samples.write_text(capsys.readouterr().out)
    assert main(["yield", str(samples), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    for m in report["measurements"]:
        assert m["negative_control"]["verdict"] == "detected"
        assert m["negative_control"]["description"] == "seeded defect"
    assert not any(
        "no measurement declared a negative_control" in w for w in report["warnings"]
    )
    assert (open(nominal, "rb").read(), open(control, "rb").read()) == before
