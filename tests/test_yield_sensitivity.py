"""Tests for `klt yield-sensitivity` and the `run_sensitivity` library
function (issue #923, Phase 3 of the statistical/yield epic #710).

Same two-tier split as `tests/test_yield.py`:

- The **input-reader** tier (sensitivity sample document parsing, every
  error the CLI can raise before any statistics run) needs no Rust and
  always runs.
- The **statistics** tier needs the `klt_yield_native` extension to be built
  and importable (`maturin develop --release` in `native/yield/`, or `uv
  sync --group yield`); those tests are skipped with a clear reason -- never
  silently passed -- when it is not. CI's `native-yield` job (which this
  file's tests are folded into) is what makes sure that skip is never the
  only thing the pipeline ever sees.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import subprocess
import sys

import pytest

from klayout_tools.cli import main
from klayout_tools.yield_sensitivity import (
    SensitivityError,
    _measurements_from_doc,
    run_sensitivity,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(REPO_ROOT, "examples", "yield-sensitivity")

requires_native = pytest.mark.skipif(
    importlib.util.find_spec("klt_yield_native") is None,
    reason=(
        "klt_yield_native is not built -- run `maturin develop --release` in "
        "native/yield/ (or `uv sync --group yield`); see "
        "docs/cli/yield-sensitivity.md#building-the-native-extension"
    ),
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _doc(tmp_path, samples, name="m", unit="V"):
    entry = {"name": name, "unit": unit, "samples": samples}
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"measurements": [entry]}))
    return str(path)


def _linear_campaign(
    n: int, coeffs: dict[str, float], noise_stddev: float = 0.0, seed: int = 7
):
    """`n` samples of independent unit-normal parameter draws named by
    `coeffs`' keys, with `output = sum(coef * param) + noise`."""
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        parameters = {name: rng.gauss(0.0, 1.0) for name in coeffs}
        output = sum(coef * parameters[name] for name, coef in coeffs.items())
        output += rng.gauss(0.0, noise_stddev)
        samples.append({"parameters": parameters, "output": output})
    return samples


# --------------------------------------------------------------------------- #
# Input readers (no native extension needed)
# --------------------------------------------------------------------------- #


def test_sample_document_is_read(tmp_path):
    samples = [
        {"parameters": {"a": 0.1, "b": 0.2}, "output": 1.0},
        {"parameters": {"a": 0.3, "b": 0.4}, "output": 2.0},
    ]
    path = _doc(tmp_path, samples, name="m")
    measurements = _measurements_from_doc(json.loads(open(path).read()))
    assert measurements[0]["name"] == "m"
    assert measurements[0]["output"] == [1.0, 2.0]
    assert measurements[0]["parameters"] == [{"a": 0.1, "b": 0.2}, {"a": 0.3, "b": 0.4}]


def test_missing_file_is_a_clean_error(tmp_path):
    with pytest.raises(SensitivityError, match="samples file not found"):
        run_sensitivity(str(tmp_path / "nope.json"))


def test_malformed_json_is_a_clean_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(SensitivityError, match="could not read samples"):
        run_sensitivity(str(path))


def test_document_without_measurements_is_an_error(tmp_path):
    path = tmp_path / "junk.json"
    path.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(SensitivityError, match="no non-empty 'measurements'"):
        run_sensitivity(str(path))


def test_measurement_without_samples_is_an_error(tmp_path):
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"measurements": [{"name": "m", "samples": []}]}))
    with pytest.raises(SensitivityError, match="no non-empty 'samples'"):
        run_sensitivity(str(path))


def test_sample_with_non_numeric_output_is_an_error(tmp_path):
    samples = [{"parameters": {"a": 0.1}, "output": "oops"}]
    path = _doc(tmp_path, samples)
    with pytest.raises(SensitivityError, match="no numeric 'output' value"):
        run_sensitivity(path)


def test_sample_with_no_parameters_is_an_error(tmp_path):
    samples = [{"parameters": {}, "output": 1.0}]
    path = _doc(tmp_path, samples)
    with pytest.raises(SensitivityError, match="no non-empty 'parameters'"):
        run_sensitivity(path)


def test_sample_with_non_numeric_parameter_is_an_error(tmp_path):
    samples = [{"parameters": {"a": "oops"}, "output": 1.0}]
    path = _doc(tmp_path, samples)
    with pytest.raises(SensitivityError, match="is not a number"):
        run_sensitivity(path)


def test_an_unknown_measurement_name_is_an_error(tmp_path):
    samples = _linear_campaign(10, {"a": 1.0, "b": 2.0})
    path = _doc(tmp_path, samples, name="m")
    with pytest.raises(SensitivityError, match="no such measurement"):
        run_sensitivity(path, measurements=["nope"])


# --------------------------------------------------------------------------- #
# Statistics (native extension required)
# --------------------------------------------------------------------------- #


@requires_native
def test_a_sample_set_below_the_minimum_is_an_error(tmp_path):
    samples = _linear_campaign(3, {"a": 1.0})
    path = _doc(tmp_path, samples)
    with pytest.raises(SensitivityError, match="at least"):
        run_sensitivity(path)


@requires_native
def test_a_constant_output_is_an_error(tmp_path):
    samples = _linear_campaign(10, {"a": 1.0, "b": 1.0})
    for s in samples:
        s["output"] = 42.0
    path = _doc(tmp_path, samples)
    with pytest.raises(SensitivityError, match="no variance to attribute"):
        run_sensitivity(path)


@requires_native
def test_a_constant_parameter_is_excluded_with_a_warning(tmp_path):
    samples = _linear_campaign(20, {"a": 1.0, "b": 3.0})
    for s in samples:
        s["parameters"]["frozen"] = 1.0
    path = _doc(tmp_path, samples)
    report = run_sensitivity(path)
    m = report["measurements"][0]
    assert all(e["parameter"] != "frozen" for e in m["ranking"])
    assert any("'frozen'" in w for w in m["warnings"])


@requires_native
def test_when_every_parameter_is_constant_it_is_an_error(tmp_path):
    samples = [
        {"parameters": {"a": 1.0, "b": 2.0}, "output": float(i)} for i in range(10)
    ]
    path = _doc(tmp_path, samples)
    with pytest.raises(SensitivityError, match="nothing to rank"):
        run_sensitivity(path)


@requires_native
def test_a_dominant_linear_term_is_ranked_first_by_standardized_coefficient(tmp_path):
    """The exact acceptance criterion issue #923 states: a campaign where
    one injected mismatch term is scaled 10x the others must surface first,
    with plenty of samples for the regression to be solvable."""
    samples = _linear_campaign(
        200, {"x1": 10.0, "x2": 1.0, "x3": -1.0}, noise_stddev=0.05, seed=42
    )
    path = _doc(tmp_path, samples, name="offset_mv", unit="mV")
    report = run_sensitivity(path)
    m = report["measurements"][0]
    assert m["method"]["primary"] == "standardized_regression_coefficient"
    assert m["method"]["regression_solvable"] is True
    assert m["ranking"][0]["parameter"] == "x1"
    assert m["ranking"][0]["rank"] == 1
    assert m["r_squared"] > 0.95
    # An order of magnitude above the next-ranked contribution.
    assert abs(m["ranking"][0]["contribution"]) > 5 * abs(
        m["ranking"][1]["contribution"]
    )


@requires_native
def test_falls_back_to_pearson_correlation_when_the_regression_is_unsolvable(tmp_path):
    """Too few samples relative to the parameter count -- 5 samples, 4
    parameters -- so the multiple regression cannot be fit; the ranking
    still surfaces the dominant term via the Pearson fallback."""
    samples = _linear_campaign(
        5, {"x1": 10.0, "x2": 1.0, "x3": 1.0, "x4": -1.0}, seed=3
    )
    path = _doc(tmp_path, samples)
    report = run_sensitivity(path)
    m = report["measurements"][0]
    assert m["method"]["primary"] == "pearson_correlation"
    assert m["method"]["regression_solvable"] is False
    assert m["r_squared"] is None
    assert all(e["standardized_coefficient"] is None for e in m["ranking"])
    assert any("not solvable" in w for w in m["warnings"])


@requires_native
def test_ranking_contribution_matches_standardized_coefficient_when_solvable(tmp_path):
    samples = _linear_campaign(50, {"a": 3.0, "b": 1.0}, noise_stddev=0.1, seed=9)
    path = _doc(tmp_path, samples)
    report = run_sensitivity(path)
    for entry in report["measurements"][0]["ranking"]:
        assert entry["contribution"] == entry["standardized_coefficient"]


@requires_native
def test_ranking_is_sorted_descending_by_absolute_contribution(tmp_path):
    samples = _linear_campaign(80, {"a": 5.0, "b": -3.0, "c": 0.5}, seed=11)
    path = _doc(tmp_path, samples)
    report = run_sensitivity(path)
    ranking = report["measurements"][0]["ranking"]
    contributions = [abs(e["contribution"]) for e in ranking]
    assert contributions == sorted(contributions, reverse=True)
    ranks = [e["rank"] for e in ranking]
    assert ranks == [1, 2, 3]


@requires_native
def test_measurement_filter_accepts_repeats_and_commas(tmp_path):
    doc = {
        "measurements": [
            {
                "name": name,
                "samples": _linear_campaign(20, {"a": 1.0, "b": 2.0}, seed=i),
            }
            for i, name in enumerate(("p", "q", "r"))
        ]
    }
    path = tmp_path / "samples.json"
    path.write_text(json.dumps(doc))
    report = run_sensitivity(str(path), measurements=["p", "r"])
    assert [m["name"] for m in report["measurements"]] == ["p", "r"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@requires_native
def test_cli_json_envelope_and_exit_code(tmp_path, capsys):
    samples = _linear_campaign(50, {"a": 4.0, "b": 1.0}, noise_stddev=0.1, seed=5)
    path = _doc(tmp_path, samples)
    assert main(["yield-sensitivity", path, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["measurement_count"] == 1
    assert payload["measurements"][0]["ranking"][0]["parameter"] == "a"


def test_cli_error_uses_the_json_error_envelope(tmp_path, capsys):
    args = ["yield-sensitivity", str(tmp_path / "nope.json"), "--format", "json"]
    assert main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "yield-sensitivity"
    assert "not found" in error["error"]["message"]


@requires_native
def test_cli_text_output_shows_the_ranking_and_its_limitations(tmp_path, capsys):
    samples = _linear_campaign(50, {"a": 4.0, "b": 1.0}, noise_stddev=0.1, seed=5)
    path = _doc(tmp_path, samples)
    assert main(["yield-sensitivity", path]) == 0
    out = capsys.readouterr().out
    assert "method: standardized_regression_coefficient" in out
    assert "1. a" in out
    assert "limitation:" in out


def test_cli_empty_measurement_filter_is_an_error(tmp_path, capsys):
    path = _doc(tmp_path, [{"parameters": {"a": 1.0}, "output": 1.0}] * 5)
    args = ["yield-sensitivity", path, "--measurement", " , ", "--format", "json"]
    assert main(args) == 1
    error = json.loads(capsys.readouterr().err)
    assert "empty" in error["error"]["message"]


# --------------------------------------------------------------------------- #
# Worked example (issue #923's own acceptance-criterion validation case)
# --------------------------------------------------------------------------- #


def test_example_regenerates_byte_identically():
    """`examples/yield-sensitivity/generate.py` is seeded, so the committed
    fixture must round-trip exactly -- otherwise the numbers quoted in
    docs/cli/yield-sensitivity.md would drift silently."""
    name = "dominant-mismatch-samples.json"
    before = open(os.path.join(EXAMPLES, name)).read()
    subprocess.run(
        [sys.executable, os.path.join(EXAMPLES, "generate.py")],
        check=True,
        capture_output=True,
    )
    after = open(os.path.join(EXAMPLES, name)).read()
    assert after == before


@requires_native
def test_worked_example_surfaces_the_known_dominant_parameter_first():
    """The exact acceptance criterion issue #923 states: "a campaign where
    one injected mismatch term is scaled 10x the others -- the ranking must
    correctly surface it first"."""
    report = run_sensitivity(os.path.join(EXAMPLES, "dominant-mismatch-samples.json"))
    m = report["measurements"][0]
    assert m["name"] == "offset_mv"
    assert m["n"] == 200
    assert m["parameter_count"] == 4
    assert m["method"]["regression_solvable"] is True

    ranking = m["ranking"]
    assert ranking[0]["parameter"] == "vth_mismatch_m1"
    assert ranking[0]["rank"] == 1
    # An order of magnitude above every other parameter's contribution.
    for other in ranking[1:]:
        assert abs(ranking[0]["contribution"]) > 5 * abs(other["contribution"])
    # Every corroborating metric agrees it dominates.
    assert ranking[0]["pearson_r"] > 0.9
    assert ranking[0]["spearman_rho"] > 0.9
    assert m["r_squared"] > 0.99
    assert not math.isnan(ranking[0]["standardized_coefficient"])
