"""Per-measurement Monte Carlo dispersion bounds and CLI grading."""

import json

import pytest

from klayout_tools import sim
from klayout_tools.cli import main
from test_sim import (
    _B_ALONE_FAILS,
    _KNOWN_SAMPLES,
    _POOLED_ONLY_FAILS,
    _VOUT_MEAS,
    _mc_request,
    _sampled_corner,
    _scope_corners,
    _stub_subprocess_values,
    _write_body,
    _write_request,
)

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


# --------------------------------------------------------------------------- #
# `population_limits` -- a bound on the dispersion itself (#6967)
# --------------------------------------------------------------------------- #


def _population_rollup(
    per_corner,
    population_limits,
    *,
    k_sigma=3.0,
    limits=None,
    meas_k_sigma=None,
    window_scope="pooled",
):
    spec: dict = {"name": "vref", "population_limits": population_limits}
    if limits is not None:
        spec["limits"] = limits
    if meas_k_sigma is not None:
        spec["k_sigma"] = meas_k_sigma
    (entry,) = sim._rollup_measurements(
        [spec],
        _scope_corners(per_corner),
        {
            "quantiles": sim.DEFAULT_MC_QUANTILES,
            "k_sigma": k_sigma,
            "window_scope": window_scope,
        },
    )
    return entry


# Corner "b" alone disperses past a tight bound ("a" is tight: k*sigma 0,
# "b" k*sigma 3*sqrt(2) ~ 4.243); no `limits` involved at all.
_POP_B_ALONE_FAILS = {"a": [5.0] * 4, "b": [4.0, 6.0]}


def test_population_limits_fails_the_wide_corner_independent_of_mean():
    entry = _population_rollup(_POP_B_ALONE_FAILS, {"max_k_sigma": 1.0})

    pop = entry["monte_carlo"]["population_limits"]
    assert pop["statistic"] == "k_sigma"
    assert pop["k"] == 3.0
    assert pop["max"] == 1.0
    assert "quantization_variance" not in pop
    assert pop["status"] == "fail"
    assert pop["worst_corner"] == "b"
    assert pop["margin"] == pytest.approx(1.0 - 3.0 * (2.0**0.5))
    assert pop["margin"] < 0
    by = {c["corner_id"]: c for c in pop["by_corner"]}
    assert by["a"]["value"] == pytest.approx(0.0)
    assert by["a"]["status"] == "pass"
    assert by["b"]["status"] == "fail"
    # Both corners' values are listed, in corner order.
    assert [c["corner_id"] for c in pop["by_corner"]] == ["a", "b"]
    # Independent of the mean and of per-sample `limits` (none declared):
    # every sample passes, yet the dispersion alone fails the row.
    assert entry["status"] == "fail"


def test_population_limits_all_pass_names_the_smallest_margin_corner():
    entry = _population_rollup(_POP_B_ALONE_FAILS, {"max_k_sigma": 5.0})

    pop = entry["monte_carlo"]["population_limits"]
    assert pop["status"] == "pass"
    assert pop["worst_corner"] == "b"  # smallest margin, not first corner
    assert pop["margin"] == pytest.approx(5.0 - 3.0 * (2.0**0.5))
    assert entry["status"] == "pass"


def test_population_limits_margin_tie_names_the_first_corner_in_order():
    per_corner = {"a": [4.0, 6.0], "b": [9.0, 11.0]}  # equal sigma
    entry = _population_rollup(per_corner, {"max_k_sigma": 5.0})

    pop = entry["monte_carlo"]["population_limits"]
    assert pop["status"] == "pass"
    assert pop["worst_corner"] == "a"


def test_population_limits_quantization_variance_nets_out_sigma():
    # Raw k*sigma = 3*sqrt(2) ~ 4.243 exceeds the 4.0 bound...
    entry = _population_rollup({"b": [4.0, 6.0]}, {"max_k_sigma": 4.0})
    assert entry["monte_carlo"]["population_limits"]["status"] == "fail"

    # ...but net of a declared quantization variance it does not:
    # sigma_net = sqrt(max(2 - 1, 0)) = 1 -> value 3.0 <= 4.0.
    entry = _population_rollup(
        {"b": [4.0, 6.0]}, {"max_k_sigma": 4.0, "quantization_variance": 1.0}
    )
    pop = entry["monte_carlo"]["population_limits"]
    assert pop["quantization_variance"] == 1.0
    assert pop["by_corner"][0]["value"] == pytest.approx(3.0)
    assert pop["status"] == "pass"
    assert entry["status"] == "pass"


def test_population_limits_variance_above_sigma_squares_to_zero():
    entry = _population_rollup(
        {"b": [4.0, 6.0]}, {"max_k_sigma": 0.5, "quantization_variance": 9.0}
    )

    pop = entry["monte_carlo"]["population_limits"]
    assert pop["by_corner"][0]["value"] == 0.0
    assert pop["status"] == "pass"


def test_population_limits_skips_corners_without_a_sigma():
    # "a" has a single trusted sample (stddev null, n < 2): not graded, and
    # it does not fail the measurement; "b" still is.
    entry = _population_rollup({"a": [5.0], "b": [4.0, 6.0]}, {"max_k_sigma": 1.0})

    pop = entry["monte_carlo"]["population_limits"]
    by = {c["corner_id"]: c for c in pop["by_corner"]}
    assert by["a"] == {
        "corner_id": "a",
        "value": None,
        "margin": None,
        "status": None,
    }
    assert pop["status"] == "fail"
    assert pop["worst_corner"] == "b"


def test_population_limits_without_any_gradable_corner_is_null():
    entry = _population_rollup({"a": [5.0], "b": [7.0]}, {"max_k_sigma": 1.0})

    pop = entry["monte_carlo"]["population_limits"]
    assert pop["status"] is None
    assert pop["margin"] is None
    assert pop["worst_corner"] is None
    assert all(c["status"] is None for c in pop["by_corner"])
    assert entry["status"] == "pass"  # the row fails nothing


def test_population_limits_all_samples_errored_corner_is_ungraded():
    entry = _population_rollup(
        {"a": [5.0] * 4, "b": [None, None]}, {"max_k_sigma": 1.0}
    )

    pop = entry["monte_carlo"]["population_limits"]
    by = {c["corner_id"]: c for c in pop["by_corner"]}
    assert by["b"]["status"] is None
    assert pop["worst_corner"] == "a"
    assert pop["status"] == "pass"
    assert entry["status"] == "error"  # the errored samples still error the run


def test_population_limits_error_outranks_the_population_fail():
    entry = _population_rollup(
        {"a": [5.0] * 4, "b": [4.0, 6.0, None]}, {"max_k_sigma": 1.0}
    )

    assert entry["monte_carlo"]["population_limits"]["status"] == "fail"
    assert entry["status"] == "error"


def test_population_limits_inconclusive_outranks_the_population_fail():
    corners = [
        _sampled_corner("a", 0, 5.0),
        _sampled_corner("a", 1, 5.0),
        _sampled_corner("b", 0, 4.0),
        _sampled_corner("b", 1, 6.0),
    ]
    # A distrusted (plausibility-disqualified) sample grades the measurement
    # `inconclusive`; the dispersion row must not overwrite that stronger
    # claim with a confident `fail` -- corner "b" is still graded (its own
    # samples are trusted) and still fails the bound.
    corners[1]["measurements"][0]["status"] = "inconclusive"
    (entry,) = sim._rollup_measurements(
        [{"name": "vref", "population_limits": {"max_k_sigma": 1.0}}],
        corners,
        {
            "quantiles": sim.DEFAULT_MC_QUANTILES,
            "k_sigma": 3.0,
            "window_scope": "pooled",
        },
    )

    assert entry["monte_carlo"]["population_limits"]["status"] == "fail"
    assert entry["monte_carlo"]["population_limits"]["worst_corner"] == "b"
    assert entry["status"] == "inconclusive"


def test_population_limits_k_zero_always_passes():
    entry = _population_rollup({"b": [4.0, 6.0]}, {"max_k_sigma": 0.0}, k_sigma=0.0)

    pop = entry["monte_carlo"]["population_limits"]
    assert pop["k"] == 0.0
    assert pop["by_corner"][0]["value"] == 0.0
    assert pop["status"] == "pass"


def test_population_limits_per_measurement_k_sigma_overrides_run_wide():
    entry = _population_rollup(
        {"b": [4.0, 6.0]}, {"max_k_sigma": 2.0}, k_sigma=3.0, meas_k_sigma=1.0
    )

    pop = entry["monte_carlo"]["population_limits"]
    assert pop["k"] == 1.0
    assert pop["by_corner"][0]["value"] == pytest.approx(2.0**0.5)
    assert pop["status"] == "pass"  # would fail at the run-wide k=3


def test_population_limits_single_corner_is_the_whole_population():
    entry = _population_rollup({"only": [4.0, 6.0]}, {"max_k_sigma": 1.0})

    pop = entry["monte_carlo"]["population_limits"]
    assert [c["corner_id"] for c in pop["by_corner"]] == ["only"]
    assert pop["worst_corner"] == "only"
    assert entry["status"] == "fail"


def test_population_limits_omitted_leaves_the_rollup_unchanged():
    (entry,) = sim._rollup_measurements(
        [{"name": "vref", "limits": {"min": 0.0, "max": 10.0}}],
        _scope_corners(_POP_B_ALONE_FAILS),
        {
            "quantiles": sim.DEFAULT_MC_QUANTILES,
            "k_sigma": 3.0,
            "window_scope": "pooled",
        },
    )

    assert "population_limits" not in entry["monte_carlo"]


def test_population_limits_leaves_window_and_limits_semantics_alone():
    # Declared alongside `limits` + `window_scope: "per_corner"`: the window
    # still gates exactly as #2960 shipped it, the dispersion row adds its
    # own verdict, and neither dilutes the other.
    entry = _population_rollup(
        _B_ALONE_FAILS,
        {"max_k_sigma": 100.0},
        limits={"min": 0.0, "max": 10.0},
        window_scope="per_corner",
    )
    mc = entry["monte_carlo"]
    assert mc["failing_corners"] == ["b"]
    assert mc["population_limits"]["status"] == "pass"
    assert entry["status"] == "fail"

    # And the converse: the pooled window fails on between-corner spread
    # while every corner's dispersion row passes -- the row neither rescues
    # nor contradicts the window verdict.
    entry = _population_rollup(
        _POOLED_ONLY_FAILS,
        {"max_k_sigma": 5.0},
        limits={"min": 0.0, "max": 10.0},
    )
    mc = entry["monte_carlo"]
    assert mc["sigma_window"]["status"] == "fail"
    assert mc["population_limits"]["status"] == "pass"
    assert entry["status"] == "fail"


def test_run_sim_population_limits_two_corner_worst_corner_fails(tmp_path, monkeypatch):
    """Corner ss alone disperses past the bound: measurement fail, envelope
    fail -- while every individual sample passes its limits."""
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        two_corners=True,
        measurements=[
            {
                **_VOUT_MEAS,
                "limits": {"min": 0.0, "max": 10.0},
                "population_limits": {"max_k_sigma": 1.0},
            }
        ],
    )
    # tt draws [1.0, 1.0] (sigma 0), ss draws [1.0, 3.0] (sigma sqrt(2)).
    _stub_subprocess_values(monkeypatch, "vout", [1.0, 1.0, 1.0, 3.0])

    report = sim.run_sim(str(request))

    assert all(c["status"] == "pass" for c in report["corners"])
    pop = report["measurements"][0]["monte_carlo"]["population_limits"]
    assert pop["status"] == "fail"
    assert pop["worst_corner"] == "ss/novdd/27C"
    assert pop["margin"] < 0
    by = {c["corner_id"]: c for c in pop["by_corner"]}
    assert by["tt/novdd/27C"]["value"] == pytest.approx(0.0)
    assert by["ss/novdd/27C"]["value"] == pytest.approx(3.0 * (2.0**0.5))
    assert report["measurements"][0]["status"] == "fail"
    assert report["status"] == "fail"


def test_run_sim_population_limits_quantization_flips_to_pass(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        two_corners=True,
        measurements=[
            {
                **_VOUT_MEAS,
                "population_limits": {
                    "max_k_sigma": 1.0,
                    "quantization_variance": 2.0,
                },
            }
        ],
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0, 1.0, 1.0, 3.0])

    report = sim.run_sim(str(request))

    pop = report["measurements"][0]["monte_carlo"]["population_limits"]
    assert pop["quantization_variance"] == 2.0
    # sigma_net = sqrt(max(sigma^2 - 2, 0)): the raw k*sigma ~ 4.243 fails
    # the 1.0 bound, the net value (0 up to the sqrt round-trip's ULP)
    # passes it.
    assert pop["by_corner"][1]["value"] == pytest.approx(0.0, abs=1e-6)
    assert pop["status"] == "pass"
    assert report["status"] == "pass"


def test_run_sim_population_limits_omitted_keeps_response_shape(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    assert "population_limits" not in report["measurements"][0]["monte_carlo"]


@pytest.mark.parametrize(
    "population_limits",
    [
        3,
        [],
        {},
        {"max_k_sigma": -1},
        {"max_k_sigma": "3"},
        {"max_k_sigma": True},
        {"quantization_variance": 1.0},
        {"max_k_sigma": 1.0, "quantization_variance": -1.0},
        {"max_k_sigma": 1.0, "quantization_variance": "1"},
        {"max_k_sigma": 1.0, "min_k_sigma": 0.0},
    ],
)
def test_run_sim_population_limits_bad_declaration_raises(tmp_path, population_limits):
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        measurements=[{**_VOUT_MEAS, "population_limits": population_limits}],
    )

    with pytest.raises(sim.SimError, match="population_limits"):
        sim.run_sim(str(request))


def test_run_sim_population_limits_requires_monte_carlo(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [{**_VOUT_MEAS, "population_limits": {"max_k_sigma": 1.0}}],
        },
    )

    with pytest.raises(sim.SimError, match="population_limits.*monte_carlo"):
        sim.run_sim(str(request))


def test_run_sim_population_limits_requires_a_resolvable_k_sigma(tmp_path):
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch"},
        measurements=[{**_VOUT_MEAS, "population_limits": {"max_k_sigma": 1.0}}],
    )

    with pytest.raises(sim.SimError, match="population_limits.*k_sigma"):
        sim.run_sim(str(request))


@pytest.mark.parametrize(
    ("malformed", "type_name"), [("typo", "str"), ([1], "list"), (False, "bool")]
)
@pytest.mark.parametrize("with_population_limit", [False, True])
def test_cli_preserves_malformed_monte_carlo_error(
    tmp_path, capsys, malformed, type_name, with_population_limit
):
    measurement = dict(_VOUT_MEAS)
    if with_population_limit:
        measurement["population_limits"] = {"max_k_sigma": 1}
    request = _mc_request(tmp_path, malformed, measurements=[measurement])

    assert main(["sim", str(request), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["command"] == "sim"
    expected = f"request.monte_carlo must be an object (got {type_name})"
    assert error["message"] == expected


def test_cli_sim_population_limits_failure_exits_3(tmp_path, monkeypatch, capsys):
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        two_corners=True,
        measurements=[
            {
                **_VOUT_MEAS,
                "population_limits": {"max_k_sigma": 1.0},
            }
        ],
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0, 1.0, 1.0, 3.0])

    assert main(["sim", str(request), "--format", "json"]) == 3

    payload = json.loads(capsys.readouterr().out)
    pop = payload["measurements"][0]["monte_carlo"]["population_limits"]
    assert pop["status"] == "fail"
    assert pop["worst_corner"] == "ss/novdd/27C"
