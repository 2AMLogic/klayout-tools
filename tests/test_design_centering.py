"""Tests for `klt design-centering` and the `build_recentering_proposal`
library function (issue #924, Phase 3 of the statistical/yield epic #710).

Two tiers, mirroring `tests/test_yield_sensitivity.py`:

- **Contract/mapping tests** (this module's own logic: request parsing,
  measurement selection, the mismatch-parameter -> instance-name bridge, the
  Pelgrom-scaling heuristic) need no native extension and always run --
  they build synthetic ranking/sized-device dicts directly, independent of
  `klt yield-sensitivity`'s own statistics engine.
- **The worked-example round-trip** (issue #924's own acceptance criterion:
  "a synthetic case matching #923's known-dominant-parameter validation case
  round-trips through the contract") additionally runs `klt
  yield-sensitivity` for real, so it needs the `klt_yield_native` extension
  built (`maturin develop --release` in `native/yield/`, or `uv sync
  --group yield`) and is skipped with a clear reason when it is not.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from klayout_tools.cli import main
from klayout_tools.design_centering import (
    DesignCenteringError,
    build_recentering_proposal,
    load_request,
    run_design_centering,
)
from klayout_tools.yield_sensitivity import run_sensitivity

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(REPO_ROOT, "examples", "design-centering")
YIELD_SENSITIVITY_EXAMPLES = os.path.join(REPO_ROOT, "examples", "yield-sensitivity")

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


def _ranking_measurement(
    name: str, parameters_and_contributions: list[tuple[str, float]], unit="mV"
) -> dict:
    """A bare `klt yield-sensitivity` measurement object -- pre-sorted
    descending by |contribution|, as the real producer's own contract
    guarantees."""
    ranking = [
        {
            "rank": i + 1,
            "parameter": param,
            "contribution": contribution,
            "standardized_coefficient": contribution,
            "pearson_r": contribution,
            "spearman_rho": contribution,
        }
        for i, (param, contribution) in enumerate(
            sorted(parameters_and_contributions, key=lambda pc: -abs(pc[1]))
        )
    ]
    return {
        "name": name,
        "unit": unit,
        "n": 100,
        "parameter_count": len(ranking),
        "method": {
            "primary": "standardized_regression_coefficient",
            "description": "...",
            "limitations": ["..."],
            "regression_solvable": True,
        },
        "r_squared": 0.99,
        "ranking": ranking,
        "warnings": [],
    }


def _single_device_sized(w_um=5.0, l_um=0.5, nf=1, mult=1) -> dict:
    return {
        "schema_version": 1,
        "status": "pass",
        "device": {"kind": "nmos", "model": "demo", "l_um": l_um},
        "operating_point": {
            "w_um": w_um,
            "l_um": l_um,
            "nf": nf,
            "mult": mult,
            "id_a": 1e-5,
            "gm_s": 1e-4,
            "gm_id": 10.0,
            "vgs_v": 0.7,
            "vth_v": 0.5,
            "vov_v": 0.2,
            "vdsat_v": 0.19,
            "inversion_level": "strong",
        },
    }


def _topology_sized(instances: dict[str, dict | None]) -> dict:
    """A minimal topology-mode `klt size` response: `instances` maps
    instance name -> operating_point dict (or `None` for an errored
    instance)."""
    devices = {}
    for name, op in instances.items():
        devices[name] = {
            "role": name,
            "status": "pass" if op else "error",
            "operating_point": op,
        }
    return {
        "schema_version": 1,
        "status": "pass",
        "topology": "diff_pair_mirror_tail",
        "devices": devices,
    }


def _request(sensitivity, sized_device, parameter_map, measurement=None) -> dict:
    req = {
        "sensitivity": sensitivity,
        "sized_device": sized_device,
        "parameter_map": parameter_map,
    }
    if measurement is not None:
        req["measurement"] = measurement
    return req


# --------------------------------------------------------------------------- #
# Request loading
# --------------------------------------------------------------------------- #


def test_missing_request_file_is_a_clean_error(tmp_path):
    with pytest.raises(DesignCenteringError, match="file not found"):
        load_request(str(tmp_path / "nope.json"))


def test_malformed_json_is_a_clean_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(DesignCenteringError, match="not valid JSON"):
        load_request(str(path))


def test_missing_required_field_is_an_error(tmp_path):
    path = tmp_path / "req.json"
    path.write_text(json.dumps({"sensitivity": {}, "sized_device": {}}))
    with pytest.raises(DesignCenteringError, match="missing required field"):
        load_request(str(path))


# --------------------------------------------------------------------------- #
# Measurement selection
# --------------------------------------------------------------------------- #


def test_single_measurement_report_is_auto_selected():
    sensitivity = {
        "measurements": [_ranking_measurement("m", [("a", 0.9), ("b", 0.1)])]
    }
    sized_device = _single_device_sized()
    request = _request(sensitivity, sized_device, {"a": "device", "b": "device"})
    report = build_recentering_proposal(request)
    assert report["measurement"] == "m"


def test_multiple_measurements_without_a_selection_is_an_error():
    sensitivity = {
        "measurements": [
            _ranking_measurement("p", [("a", 0.9)]),
            _ranking_measurement("q", [("a", 0.9)]),
        ]
    }
    request = _request(sensitivity, _single_device_sized(), {"a": "device"})
    with pytest.raises(DesignCenteringError, match="more than one measurement"):
        build_recentering_proposal(request)


def test_measurement_field_selects_among_several():
    sensitivity = {
        "measurements": [
            _ranking_measurement("p", [("a", 0.9)]),
            _ranking_measurement("q", [("b", 0.5)]),
        ]
    }
    request = _request(
        sensitivity, _single_device_sized(), {"b": "device"}, measurement="q"
    )
    report = build_recentering_proposal(request)
    assert report["measurement"] == "q"


def test_unknown_measurement_name_is_an_error():
    sensitivity = {"measurements": [_ranking_measurement("p", [("a", 0.9)])]}
    request = _request(
        sensitivity, _single_device_sized(), {"a": "device"}, measurement="nope"
    )
    with pytest.raises(DesignCenteringError, match="no such measurement"):
        build_recentering_proposal(request)


def test_bare_measurement_object_is_accepted_directly():
    sensitivity = _ranking_measurement("bare", [("a", 0.9), ("b", 0.1)])
    request = _request(
        sensitivity, _single_device_sized(), {"a": "device", "b": "device"}
    )
    report = build_recentering_proposal(request)
    assert report["measurement"] == "bare"


def test_bare_measurement_name_mismatch_is_an_error():
    sensitivity = _ranking_measurement("bare", [("a", 0.9)])
    request = _request(
        sensitivity, _single_device_sized(), {"a": "device"}, measurement="other"
    )
    with pytest.raises(DesignCenteringError, match="own name is 'bare'"):
        build_recentering_proposal(request)


def test_cli_measurement_argument_overrides_request_field():
    sensitivity = {
        "measurements": [
            _ranking_measurement("p", [("a", 0.9)]),
            _ranking_measurement("q", [("b", 0.5)]),
        ]
    }
    request = _request(
        sensitivity,
        _single_device_sized(),
        {"a": "device", "b": "device"},
        measurement="p",
    )
    report = build_recentering_proposal(request, measurement="q")
    assert report["measurement"] == "q"


# --------------------------------------------------------------------------- #
# parameter_map validation
# --------------------------------------------------------------------------- #


def test_empty_parameter_map_is_an_error():
    sensitivity = _ranking_measurement("m", [("a", 0.9)])
    request = _request(sensitivity, _single_device_sized(), {})
    with pytest.raises(DesignCenteringError, match="non-empty JSON object"):
        build_recentering_proposal(request)


def test_non_string_parameter_map_value_is_an_error():
    sensitivity = _ranking_measurement("m", [("a", 0.9)])
    request = _request(sensitivity, _single_device_sized(), {"a": 42})
    with pytest.raises(DesignCenteringError, match="must be a non-empty instance"):
        build_recentering_proposal(request)


# --------------------------------------------------------------------------- #
# Instance resolution / geometry
# --------------------------------------------------------------------------- #


def test_topology_mode_unknown_instance_is_an_error():
    sensitivity = _ranking_measurement("m", [("a", 0.9)])
    sized_device = _topology_sized(
        {"input_a": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1}}
    )
    request = _request(sensitivity, sized_device, {"a": "nope"})
    with pytest.raises(DesignCenteringError, match="not one of request.sized_device"):
        build_recentering_proposal(request)


def test_topology_mode_errored_instance_yields_no_geometry_but_still_a_candidate():
    sensitivity = _ranking_measurement("m", [("a", 0.9), ("b", 0.1)])
    sized_device = _topology_sized(
        {"input_a": None, "input_b": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1}}
    )
    request = _request(sensitivity, sized_device, {"a": "input_a", "b": "input_b"})
    report = build_recentering_proposal(request)
    assert report["candidates"][0]["geometry"] is None
    assert (
        "no usable operating-point geometry"
        in report["candidates"][0]["recommendation"]
    )


def test_single_device_mode_uses_top_level_operating_point_regardless_of_name():
    sensitivity = _ranking_measurement("m", [("a", 0.9)])
    sized_device = _single_device_sized(w_um=7.0)
    request = _request(sensitivity, sized_device, {"a": "whatever-label"})
    report = build_recentering_proposal(request)
    assert report["candidates"][0]["geometry"]["w_um"] == 7.0
    assert report["candidates"][0]["instance"] == "whatever-label"


# --------------------------------------------------------------------------- #
# Ranking / heuristic
# --------------------------------------------------------------------------- #


def test_candidates_are_ordered_by_the_producers_own_ranking_order():
    sensitivity = _ranking_measurement(
        "m", [("a", 0.9), ("b", 0.5), ("c", -0.4), ("d", 0.1)]
    )
    sized_device = _topology_sized(
        {n: {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1} for n in ("a", "b", "c", "d")}
    )
    parameter_map = {n: n for n in ("a", "b", "c", "d")}
    report = build_recentering_proposal(
        _request(sensitivity, sized_device, parameter_map)
    )
    assert [c["parameter"] for c in report["candidates"]] == ["a", "b", "c", "d"]
    assert [c["rank"] for c in report["candidates"]] == [1, 2, 3, 4]


def test_area_multiplier_is_the_squared_ratio_to_the_next_mapped_contribution():
    sensitivity = _ranking_measurement("m", [("a", 0.8), ("b", 0.2)])
    sized_device = _topology_sized(
        {
            "a": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1},
            "b": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1},
        }
    )
    report = build_recentering_proposal(
        _request(sensitivity, sized_device, {"a": "a", "b": "b"})
    )
    first, second = report["candidates"]
    assert first["suggested_area_multiplier"] == pytest.approx((0.8 / 0.2) ** 2)
    assert second["suggested_area_multiplier"] == 1.0
    assert "already the smallest-contributing" in second["recommendation"]


def test_zero_contribution_next_parameter_yields_no_finite_multiplier():
    sensitivity = _ranking_measurement("m", [("a", 0.8), ("b", 0.0)])
    sized_device = _topology_sized(
        {
            "a": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1},
            "b": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1},
        }
    )
    report = build_recentering_proposal(
        _request(sensitivity, sized_device, {"a": "a", "b": "b"})
    )
    assert report["candidates"][0]["suggested_area_multiplier"] is None
    assert "no finite area" in report["candidates"][0]["recommendation"]


def test_unmapped_parameters_are_reported_separately_with_a_warning():
    sensitivity = _ranking_measurement("m", [("a", 0.9), ("process_only", 0.3)])
    sized_device = _topology_sized({"a": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1}})
    report = build_recentering_proposal(_request(sensitivity, sized_device, {"a": "a"}))
    assert report["candidate_count"] == 1
    assert report["unmapped_parameters"] == [
        {"rank": 2, "parameter": "process_only", "contribution": 0.3}
    ]
    assert any("process_only" in w for w in report["warnings"])


def test_malformed_ranking_entry_is_an_error():
    sensitivity = {"name": "m", "ranking": [{"parameter": "a"}]}
    request = _request(sensitivity, _single_device_sized(), {"a": "device"})
    with pytest.raises(DesignCenteringError, match="malformed entry"):
        build_recentering_proposal(request)


def test_empty_ranking_is_an_error():
    sensitivity = {"name": "m", "ranking": []}
    request = _request(sensitivity, _single_device_sized(), {"a": "device"})
    with pytest.raises(DesignCenteringError, match="no non-empty 'ranking'"):
        build_recentering_proposal(request)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_json_envelope_and_exit_code(tmp_path, capsys):
    sensitivity = _ranking_measurement("m", [("a", 0.9), ("b", 0.1)])
    sized_device = _topology_sized(
        {
            "a": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1},
            "b": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1},
        }
    )
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(_request(sensitivity, sized_device, {"a": "a", "b": "b"}))
    )

    assert main(["design-centering", str(path), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["candidate_count"] == 2
    assert payload["candidates"][0]["parameter"] == "a"


def test_cli_error_uses_the_json_error_envelope(tmp_path, capsys):
    args = ["design-centering", str(tmp_path / "nope.json"), "--format", "json"]
    assert main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "design-centering"
    assert "not found" in error["error"]["message"]


def test_cli_text_output_shows_candidates_and_recommendations(tmp_path, capsys):
    sensitivity = _ranking_measurement("offset_mv", [("a", 0.9), ("b", 0.1)])
    sized_device = _topology_sized(
        {
            "a": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1},
            "b": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1},
        }
    )
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(_request(sensitivity, sized_device, {"a": "a", "b": "b"}))
    )

    assert main(["design-centering", str(path)]) == 0
    out = capsys.readouterr().out
    assert "measurement: offset_mv" in out
    assert "1. a" in out
    assert "suggested_area_multiplier" in out


def test_cli_measurement_flag(tmp_path, capsys):
    sensitivity = {
        "measurements": [
            _ranking_measurement("p", [("a", 0.9)]),
            _ranking_measurement("q", [("b", 0.5)]),
        ]
    }
    sized_device = _topology_sized(
        {
            "a": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1},
            "b": {"w_um": 5, "l_um": 0.5, "nf": 1, "mult": 1},
        }
    )
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(_request(sensitivity, sized_device, {"a": "a", "b": "b"}))
    )

    args = ["design-centering", str(path), "--measurement", "q", "--format", "json"]
    assert main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["measurement"] == "q"


# --------------------------------------------------------------------------- #
# Worked example (issue #924's own round-trip acceptance criterion)
# --------------------------------------------------------------------------- #


def test_example_request_is_valid_and_loadable():
    """Even without the native extension, the committed `request.json` must
    parse and load -- only *regenerating* the sensitivity block needs
    native."""
    path = os.path.join(EXAMPLES, "request.json")
    request = load_request(path)
    assert "sensitivity" in request
    assert "sized_device" in request
    assert "parameter_map" in request


@requires_native
def test_worked_example_round_trips_and_flags_the_dominant_parameter():
    """Issue #924's acceptance criterion: "a synthetic case (matching #923's
    known-dominant-parameter validation case) round-trips through the
    contract and correctly flags the dominant parameter as a re-centering
    candidate."

    Runs the *real* `klt yield-sensitivity` statistics (not a canned
    fixture) on #923's own known-dominant-parameter sample document, feeds
    that ranking through `build_recentering_proposal` with this directory's
    parameter map and synthetic sized device, and asserts the injected
    10x-dominant term (`vth_mismatch_m1`) surfaces as candidate #1, mapped
    to the correct sized-device instance (`input_a`).
    """
    samples_path = os.path.join(
        YIELD_SENSITIVITY_EXAMPLES, "dominant-mismatch-samples.json"
    )
    sensitivity = run_sensitivity(samples_path)

    with open(os.path.join(EXAMPLES, "sized-device.json"), encoding="utf-8") as f:
        sized_device = json.load(f)
    with open(os.path.join(EXAMPLES, "parameter-map.json"), encoding="utf-8") as f:
        parameter_map = json.load(f)

    report = build_recentering_proposal(
        {
            "sensitivity": sensitivity,
            "sized_device": sized_device,
            "parameter_map": parameter_map,
            "measurement": "offset_mv",
        }
    )

    assert report["measurement"] == "offset_mv"
    assert report["candidate_count"] == 4
    top = report["candidates"][0]
    assert top["parameter"] == "vth_mismatch_m1"
    assert top["rank"] == 1
    assert top["instance"] == "input_a"
    assert top["geometry"]["area_um2"] > 0
    # An order of magnitude above the next-ranked candidate's contribution,
    # same threshold `tests/test_yield_sensitivity.py` asserts against the
    # same fixture.
    assert abs(top["contribution"]) > 5 * abs(report["candidates"][1]["contribution"])
    # A large suggested area multiplier is exactly the outsized-headroom
    # signal a design-centering pass should notice for the dominant term.
    assert top["suggested_area_multiplier"] > 10
    assert not report["unmapped_parameters"]


@requires_native
def test_worked_example_via_the_committed_request_file():
    """Same assertion as the round-trip test above, but driven through
    `run_design_centering` against the committed `request.json` -- the
    exact path the `klt design-centering` CLI itself takes."""
    path = os.path.join(EXAMPLES, "request.json")
    report = run_design_centering(path)
    assert report["candidates"][0]["parameter"] == "vth_mismatch_m1"
    assert report["candidates"][0]["instance"] == "input_a"


@requires_native
def test_example_regenerates_byte_identically():
    """`examples/design-centering/generate.py` re-runs `klt
    yield-sensitivity` for real -- with the native extension built, the
    committed `request.json` must round-trip exactly, mirroring
    `tests/test_yield_sensitivity.py::test_example_regenerates_byte_identically`'s
    own precedent for its sibling fixture."""
    path = os.path.join(EXAMPLES, "request.json")
    before = open(path).read()
    subprocess.run(
        [sys.executable, os.path.join(EXAMPLES, "generate.py")],
        check=True,
        capture_output=True,
        cwd=REPO_ROOT,
    )
    after = open(path).read()
    assert after == before
