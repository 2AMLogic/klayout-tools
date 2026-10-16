"""Tests for `klt sim`'s numeric design-parameter axis (`corners.params`, issue #3035).

A parameter-dependent resistor, bias value or trim code varies across PVT
inside one request: the axis zips equal-length arrays of numbers by index,
sits innermost in the expansion, and is applied in the generated deck with
`alterparam` + one `reset`. Split out of `tests/test_sim.py` (already ~12k
lines) as a self-contained sibling, following `tests/test_sim_steps.py`:
unit tests need no simulator, integration tests run the real `ngspice -b`
and skip without it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim, sim_remote

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

#: The issue's fixture, as an included body: a top-level `.param` default, a
#: dependent `.param`, a DC source and the resistor it loads.
_BODY = ".param rload=1000\nV1 in 0 1\nR1 in 0 {rload}\n"


def _expand(corners: dict, exclude: list | None = None) -> list[sim.CornerPoint]:
    return sim._expand_corners(corners, exclude or [])


def _write_body(tmp_path: Path, text: str = _BODY) -> Path:
    path = tmp_path / "body.spice"
    path.write_text(text)
    return path


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


# --------------------------------------------------------------------------- #
# Expansion, ordering, identity
# --------------------------------------------------------------------------- #


def test_issue_example_expands_in_process_supply_temperature_param_order():
    points = _expand(
        {"temperature_c": [27, 85], "params": {"rload": [1000, 2000], "code": [0, 1]}}
    )
    assert [(p.temperature_c, p.params) for p in points] == [
        (27, {"code": 0, "rload": 1000}),
        (27, {"code": 1, "rload": 2000}),
        (85, {"code": 0, "rload": 1000}),
        (85, {"code": 1, "rload": 2000}),
    ]


def test_parameter_axis_is_innermost_below_every_other_axis():
    points = _expand(
        {
            "process": ["tt", "ss"],
            "supply_v": {"vdd": [1.0, 1.2]},
            "temperature_c": [27],
            "params": {"r": [1, 2]},
        }
    )
    assert [p.corner_id for p in points[:4]] == [
        "tt/1.000V/27C/params:r=1",
        "tt/1.000V/27C/params:r=2",
        "tt/1.200V/27C/params:r=1",
        "tt/1.200V/27C/params:r=2",
    ]
    assert len(points) == 8


def test_canonical_id_matches_the_documented_example():
    (point,) = _expand({"params": {"RLOAD": [2000.0], "Code": [1]}})
    assert point.corner_id == "default/novdd/27C/params:code=1,rload=2000"
    assert point.params == {"code": 1, "rload": 2000}


def test_supply_label_precedes_params_in_the_documented_id():
    (point,) = _expand({"supply_v": {"vdd": [1.8]}, "params": {"x": [1]}})
    assert point.corner_id == "default/1.800V/27C/params:x=1"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (2000, "2000"),
        (2000.0, "2000"),
        (-0.0, "0"),
        (0.1, "0.1"),
        (1e-05, "1e-05"),
        (1.0000000000000002, "1.0000000000000002"),
        (-3.5, "-3.5"),
    ],
)
def test_value_spelling_is_shortest_round_trip(value, text):
    assert sim._format_param_value(value) == text
    assert float(text) == value


def test_close_values_keep_distinct_ids_and_slugs():
    a, b = _expand({"params": {"r": [1.0, 1.0000000000000002]}})
    assert a.corner_id != b.corner_id
    assert a.slug != b.slug


def test_omitted_or_empty_params_preserve_legacy_ids_and_slugs():
    for corners in ({"process": ["ss"], "supply_v": {"vdd": [1.62]}}, {"params": {}}):
        (point,) = _expand({"temperature_c": [125], **corners})
        assert "params" not in point.corner_id
        assert point.params == {}
    (legacy,) = _expand(
        {"process": ["ss"], "supply_v": {"vdd": [1.62]}, "temperature_c": [125]}
    )
    assert legacy.corner_id == "ss/1.620V/125C"
    assert legacy.slug == "ss_1p620V_125C"


def test_slug_is_pvt_slug_plus_params_and_full_sha256_of_the_id():
    (point,) = _expand({"supply_v": {"vdd": [1.62]}, "params": {"r": [2000]}})
    digest = hashlib.sha256(point.corner_id.encode("utf-8")).hexdigest()
    assert point.slug == f"default_1p620V_27C_params_{digest}"
    assert len(digest) == 64
    # The report-side slugger (waveform plots) reaches the same directory.
    assert sim._slugify_corner_id(point.corner_id) == point.slug


def test_mc_suffix_follows_params_and_is_hashed_into_the_slug():
    (base,) = _expand({"params": {"r": [2000]}})
    sampled, _ = sim._expand_monte_carlo([base], {"n": 2, "seed": 7, "vary": "both"})
    assert [p.corner_id for p in sampled] == [
        "default/novdd/27C/params:r=2000/mc0",
        "default/novdd/27C/params:r=2000/mc1",
    ]
    assert all(p.params == {"r": 2000} for p in sampled)
    assert sampled[0].slug != sampled[1].slug
    assert sampled[0].slug == (
        "default_novdd_27C_params_"
        + hashlib.sha256(sampled[0].corner_id.encode()).hexdigest()
    )
    # Statistics group by the corner the sample was drawn from.
    assert (
        sim._base_corner_id(
            {
                "corner_id": sampled[1].corner_id,
                "monte_carlo": {"sample_index": sampled[1].sample_index},
            }
        )
        == base.corner_id
    )


def test_seeded_reproducibility_with_a_param_axis_shifting_point_indices():
    corners = {"temperature_c": [27, 85], "params": {"r": [1, 2]}}
    mc = {"n": 2, "seed": 11, "vary": "both"}
    first, _ = sim._expand_monte_carlo(_expand(corners), mc)
    again, _ = sim._expand_monte_carlo(_expand(corners), mc)
    assert [p.mc_seed for p in first] == [p.mc_seed for p in again]
    # Every (corner, sample) pair draws from its own index: the param rows
    # occupy distinct corner indices, so no two points share a seed.
    seeds = {p.mc_seed["rndseed"] for p in first}
    assert len(seeds) == len(first) == 8
    # The legacy request (no axis) is untouched by the feature.
    legacy, _ = sim._expand_monte_carlo(_expand({"temperature_c": [27, 85]}), mc)
    assert [p.corner_id for p in legacy][:2] == [
        "default/novdd/27C/mc0",
        "default/novdd/27C/mc1",
    ]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"r": None}, "nonempty array"),
        ({"r": []}, "nonempty array"),
        ({"r": 5}, "nonempty array"),
        ({"r": [1], "c": [1, 2]}, "same length"),
        ({"r": ["{rload*2}"]}, "finite number"),
        ({"r": [True]}, "finite number"),
        ({"r": [None]}, "finite number"),
        ({"r": [float("nan")]}, "finite number"),
        ({"r": [float("inf")]}, "finite number"),
        ({"1r": [1]}, "must match"),
        ({"r-x": [1]}, "must match"),
        ({"r x": [1]}, "must match"),
        ({"": [1]}, "must match"),
        ({"R": [1], "r": [2]}, "more than once"),
        ({"mc_sample_index": [1]}, "reserved"),
        ({"MC_foo": [1]}, "reserved"),
        ({"r": [1, 1.0]}, "twice"),
        ({"r": [1, 2], "c": [3, 3]}, None),
    ],
)
def test_malformed_params_are_rejected(params, message):
    if message is None:
        # Distinct rows are fine even when one column repeats.
        assert len(_expand({"params": params})) == 2
        return
    with pytest.raises(sim.SimError, match=message):
        _expand({"params": params})


def test_params_must_be_an_object():
    with pytest.raises(sim.SimError, match="must be an object"):
        _expand({"params": [1, 2]})


def test_name_in_both_params_and_supply_v_is_rejected_case_insensitively():
    with pytest.raises(sim.SimError, match="supply_v"):
        _expand({"supply_v": {"V1": [1.0]}, "params": {"v1": [1.0]}})


def test_run_sim_rejects_the_conflict_before_any_dispatch(tmp_path, monkeypatch):
    _write_body(tmp_path)

    def boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("engine launched")

    monkeypatch.setattr(sim.subprocess, "run", boom)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "op", "args": ""},
            "corners": {"supply_v": {"v1": [1.0]}, "params": {"V1": [1.0]}},
        },
    )
    with pytest.raises(sim.SimError, match="supply_v"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Exclusion
# --------------------------------------------------------------------------- #


def test_exclude_params_is_an_exact_subset_match_anded_with_selector_keys():
    corners = {"temperature_c": [27, 85], "params": {"r": [1, 2], "c": [10, 20]}}
    points = _expand(
        corners,
        [{"temperature_c": 85, "params": {"R": 2}}, {"params": {"c": 10, "r": 2}}],
    )
    assert [(p.temperature_c, p.params) for p in points] == [
        (27, {"c": 10, "r": 1}),
        (27, {"c": 20, "r": 2}),
        (85, {"c": 10, "r": 1}),
    ]


def test_exclude_params_value_matches_numerically_not_textually():
    points = _expand({"params": {"r": [1000, 2000]}}, [{"params": {"r": 2000.0}}])
    assert [p.params for p in points] == [{"r": 1000}]


def test_exclude_unknown_parameter_name_is_rejected():
    with pytest.raises(sim.SimError, match="not a corners.params parameter"):
        _expand({"params": {"r": [1]}}, [{"params": {"zz": 1}}])
    with pytest.raises(sim.SimError, match="not a corners.params parameter"):
        _expand({}, [{"params": {"r": 1}}])


# --------------------------------------------------------------------------- #
# Wire format
# --------------------------------------------------------------------------- #


def test_wire_round_trip_preserves_params_and_mc_seed():
    points, _ = sim._expand_monte_carlo(
        _expand({"params": {"r": [1, 2.5]}}), {"n": 2, "seed": 3, "vary": "both"}
    )
    back = sim._corner_points_from_wire(
        json.loads(json.dumps(sim._corner_points_to_wire(points)))
    )
    assert [p.corner_id for p in back] == [p.corner_id for p in points]
    assert [p.params for p in back] == [p.params for p in points]
    assert [p.mc_seed for p in back] == [p.mc_seed for p in points]


def test_wire_omits_the_field_for_a_point_without_params():
    (point,) = _expand({"temperature_c": [27]})
    (entry,) = sim._corner_points_to_wire([point])
    assert "params" not in entry


@pytest.mark.parametrize(
    "bad",
    [
        [1, 2],
        {"r": "{x}"},
        {"r": True},
        {"mc_x": 1},
        {"1r": 1},
        {"R": 1, "r": 2},
        {"v1": 1},
    ],
)
def test_wire_params_are_revalidated_at_the_receiving_worker(bad):
    entry = {
        "process": None,
        "supply_v": {"v1": 1.0},
        "temperature_c": 27,
        "params": bad,
    }
    with pytest.raises(sim.SimError):
        sim._corner_points_from_wire([entry])


def test_remote_request_carries_params_in_explicit_points_only():
    points = _expand({"params": {"r": [1, 2]}})
    request = {
        "netlist": "body.spice",
        "corners": {"params": {"r": [1, 2]}},
        "analysis": {"kind": "op", "args": ""},
    }
    shard = sim_remote._build_remote_request(
        request,
        timeout_s=10.0,
        keep_artifacts=False,
        want_waveforms=False,
        explicit_points=points[1:],
    )
    assert "corners" not in shard
    assert shard["_explicit_points"][0]["params"] == {"r": 2}
    whole = sim_remote._build_remote_request(
        request, timeout_s=10.0, keep_artifacts=False, want_waveforms=False
    )
    assert whole["corners"]["params"] == {"r": [1, 2]}


# --------------------------------------------------------------------------- #
# Deck
# --------------------------------------------------------------------------- #


def _control(point: sim.CornerPoint, **overrides) -> list[str]:
    kwargs = {
        "point": point,
        "analysis": {"kind": "op", "args": ""},
        "measurements_spec": [{"name": "i", "expr": "i(v1)"}],
        "raw_path": None,
        "osdi_preload": (),
    }
    kwargs.update(overrides)
    return sim._corner_deck_control_lines(**kwargs)


def test_alterparam_then_one_reset_precede_save_alter_and_analysis():
    point = sim.CornerPoint("tt", {"v1": 2.0}, 27, params={"rload": 2000, "code": 1})
    lines = _control(point, osdi_preload=("/o/a.osdi",))
    assert lines[:8] == [
        ".control",
        "pre_osdi /o/a.osdi",
        "alterparam code=1",
        "alterparam rload=2000",
        "reset",
        "save all",
        "alter v1=2.0",
        "op ",
    ]
    assert lines.count("reset") == 1


def test_save_mode_netlist_still_orders_reset_before_alter():
    point = sim.CornerPoint(None, {"v1": 2.0}, 27, params={"r": 5})
    lines = _control(point, save_mode="netlist")
    assert "save all" not in lines
    assert lines.index("reset") < lines.index("alter v1=2.0") < lines.index("op ")


def test_deck_without_params_is_unchanged():
    point = sim.CornerPoint(None, {"v1": 2.0}, 27)
    lines = _control(point)
    assert not any(line.startswith("alterparam") or line == "reset" for line in lines)
    assert lines == [
        ".control",
        "save all",
        "alter v1=2.0",
        "op ",
        "let i = i(v1)",
        "print i",
        "quit",
        ".endc",
    ]


def test_temperature_and_mc_seed_cards_stay_at_file_scope(tmp_path):
    point = sim.CornerPoint(
        None,
        {},
        85,
        params={"r": 3},
        sample_index=0,
        mc_seed={"process_seed": 1, "mismatch_seed": 2, "rndseed": 3},
    )
    deck = tmp_path / "deck.spice"
    sim._write_corner_deck(
        deck_path=str(deck),
        netlist_path="/n.spice",
        models_lib=None,
        point=point,
        analysis={"kind": "op", "args": ""},
        measurements_spec=[],
        raw_path=None,
    )
    text = deck.read_text().splitlines()
    control = text.index(".control")
    assert text.index(".temp 85") < control
    assert text.index(".options seed=3") < control
    assert text.index("alterparam r=3") > control


def test_xyce_refuses_a_parameter_axis():
    with pytest.raises(sim.SimError, match="corners.params"):
        sim._enforce_xyce_support_boundary(
            engine="xyce",
            backend="local",
            corners_spec={"params": {"r": [1]}},
            monte_carlo_declared=False,
            fail_fast_probe=False,
            osdi_preload_declared=False,
            expr_measurements_declared=False,
            save_mode_declared=False,
            analysis_steps_declared=False,
            crossings_declared=False,
        )


# --------------------------------------------------------------------------- #
# Target validation (static) and engine refusal (runtime)
# --------------------------------------------------------------------------- #


def _points(*names: str) -> list[sim.CornerPoint]:
    return [sim.CornerPoint(None, {}, 27, params={name: 1 for name in names})]


def test_unknown_target_fails_before_dispatch(tmp_path):
    body = _write_body(tmp_path)
    with pytest.raises(sim.SimError, match="'bogus'.*not declared"):
        sim._validate_param_targets(str(body), _points("rload", "bogus"))
    sim._validate_param_targets(str(body), _points("rload"))


def test_target_declared_in_an_included_body_is_accepted(tmp_path):
    (tmp_path / "inner.spice").write_text(".PARAM Rload = 1000\n")
    top = tmp_path / "top.spice"
    top.write_text(".include inner.spice\nV1 in 0 1\nR1 in 0 {rload}\n")
    sim._validate_param_targets(str(top), _points("rload"))
    with pytest.raises(sim.SimError, match="'other'"):
        sim._validate_param_targets(str(top), _points("other"))


def test_subcircuit_local_parameter_is_not_a_target(tmp_path):
    top = tmp_path / "top.spice"
    top.write_text(".subckt cell a b\n.param inner=1\nR1 a b {inner}\n.ends\n")
    with pytest.raises(sim.SimError, match="'inner'"):
        sim._validate_param_targets(str(top), _points("inner"))


def test_unresolvable_include_defers_to_the_engine(tmp_path):
    top = tmp_path / "top.spice"
    top.write_text(".include missing.spice\nV1 in 0 1\n")
    sim._validate_param_targets(str(top), _points("anything"))


def test_ngspice_alterparam_refusal_is_an_error_diagnostic():
    log = "Error: parameter 'bogus' not found,\n   command 'alterparam' skipped\n"
    point = sim.CornerPoint(None, {}, 27, params={"bogus": 3})
    (diagnostic,) = sim._alterparam_failures(point, log)
    assert diagnostic["severity"] == "error"
    assert diagnostic["code"] == "netlist"
    assert "bogus" in diagnostic["message"]
    assert sim._alterparam_failures(point, "i(v1) = -5e-4\n") == []


# --------------------------------------------------------------------------- #
# Reports, checkpoints, workers
# --------------------------------------------------------------------------- #


def test_checkpoint_fingerprint_separates_close_parameter_values(tmp_path):
    body = _write_body(tmp_path)

    def fingerprint(points):
        return sim._checkpoint_fingerprint(
            netlist_path=str(body),
            models_lib=None,
            analysis={"kind": "op", "args": ""},
            measurements_spec=[],
            corner_points=points,
            timeout_s=10.0,
            engine="ngspice",
            osdi_preload=(),
            corner_section_libs=None,
            save_mode="all",
        )

    a = fingerprint(_expand({"params": {"r": [1.0]}}))
    b = fingerprint(_expand({"params": {"r": [1.0000000000000002]}}))
    c = fingerprint(_expand({}))
    assert len({json.dumps(x, sort_keys=True, default=str) for x in (a, b, c)}) == 3


def _legacy_corner(point: sim.CornerPoint) -> dict:
    """What a worker built before the parameter axis reports: the legacy ID,
    no `params` field."""
    return {
        "corner_id": point.corner_id.split("/params:")[0],
        "status": "pass",
        "diagnostics": [],
    }


def _honest_corner(point: sim.CornerPoint) -> dict:
    return {
        "corner_id": point.corner_id,
        "params": dict(point.params),
        "status": "pass",
        "diagnostics": [],
    }


def test_updated_worker_echo_is_accepted():
    points = _expand({"temperature_c": [27, 85], "params": {"r": [1, 2]}})
    sim._require_params_echo(points, [_honest_corner(p) for p in points])


def test_worker_that_drops_the_axis_cannot_return_a_compatible_pass():
    points = _expand({"params": {"r": [1, 2]}})
    with pytest.raises(sim.SimError) as raised:
        sim._require_params_echo(points, [_legacy_corner(p) for p in points])
    assert raised.value.code == sim.PARAMS_UNSUPPORTED_WORKER_CODE


def test_worker_that_collapses_rows_is_refused():
    points = _expand({"params": {"r": [1, 2]}})
    with pytest.raises(sim.SimError, match="1 corner report"):
        sim._require_params_echo(points, [_legacy_corner(points[0])])


def test_requests_without_params_are_never_gated():
    points = _expand({"temperature_c": [27, 85]})
    sim._require_params_echo(points, [_legacy_corner(p) for p in points])


@pytest.mark.parametrize("backend", ["remote", "batch"])
def test_run_sim_refuses_an_old_offhost_worker_end_to_end(
    tmp_path, monkeypatch, backend
):
    _write_body(tmp_path)

    def old_worker(**kwargs):
        points = kwargs["corner_points"]
        # Ignores the axis: one legacy-ID report per distinct PVT coordinate.
        return [_legacy_corner(points[0])], "ngspice-46", None

    monkeypatch.setitem(sim._BACKENDS, backend, old_worker)
    monkeypatch.setattr(sim, "_preflight_staged_model_inputs", lambda *a, **k: None)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": backend,
            "analysis": {"kind": "op", "args": ""},
            "corners": {"params": {"rload": [1000, 2000]}},
        },
    )
    with pytest.raises(sim.SimError) as raised:
        sim.run_sim(str(request))
    assert raised.value.code == sim.PARAMS_UNSUPPORTED_WORKER_CODE


def test_unrun_corner_report_carries_params():
    (point,) = _expand({"params": {"r": [2000]}})
    report = sim_remote._unrun_corner_report(point, [], "lost_shard")
    assert report["params"] == {"r": 2000}
    assert report["corner_id"] == point.corner_id
    (legacy,) = _expand({})
    assert "params" not in sim_remote._unrun_corner_report(legacy, [], "lost_shard")


# --------------------------------------------------------------------------- #
# Integration: the real ngspice binary
# --------------------------------------------------------------------------- #


def _engine_request(tmp_path: Path, **overrides) -> Path:
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "op", "args": ""},
        "measurements": [{"name": "i1", "expr": "i(v1)", "unit": "A"}],
        "backend": "local",
        "options": {"keep_artifacts": True},
        **overrides,
    }
    return _write_request(tmp_path, request)


def _values(report: dict, name: str) -> list[float]:
    return [
        next(m["value"] for m in c["measurements"] if m["name"] == name)
        for c in report["corners"]
    ]


@_SKIP_NO_NGSPICE
def test_real_ngspice_param_row_overrides_a_body_declared_default(tmp_path):
    """The issue's numerical oracle: -1 mA at the declared 1 kΩ, then -0.5 mA
    once `alterparam` + `reset` move the body's `.param` to 2 kΩ."""
    _write_body(tmp_path)
    request = _engine_request(tmp_path, corners={"params": {"rload": [1000, 2000]}})
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))

    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    assert report["environment"]["engine_version"]
    assert [c["corner_id"] for c in report["corners"]] == [
        "default/novdd/27C/params:rload=1000",
        "default/novdd/27C/params:rload=2000",
    ]
    assert [c["params"] for c in report["corners"]] == [
        {"rload": 1000},
        {"rload": 2000},
    ]
    currents = _values(report, "i1")
    assert currents[0] == pytest.approx(-1e-3, abs=1e-9)
    assert currents[1] == pytest.approx(-0.5e-3, abs=1e-9)
    # Distinct artifact directories, named by the hashed slug.
    decks = {c["artifacts"]["deck"] for c in report["corners"]}
    assert len(decks) == 2
    assert all("_params_" in d for d in decks)


@_SKIP_NO_NGSPICE
def test_real_ngspice_dependent_parameter_is_re_evaluated(tmp_path):
    _write_body(
        tmp_path,
        ".param rload=1000\n.param rdep={2*rload}\nV1 in 0 1\n"
        "R1 in x {rdep}\nR2 x 0 1k\n",
    )
    request = _engine_request(tmp_path, corners={"params": {"rload": [1000, 3000]}})
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass"
    assert _values(report, "i1") == [
        pytest.approx(-1 / 3000, abs=1e-9),
        pytest.approx(-1 / 7000, abs=1e-9),
    ]


@_SKIP_NO_NGSPICE
def test_real_ngspice_sin_source_amplitude_and_frequency_follow_the_row(tmp_path):
    """A parameterized SIN source: amplitude and frequency both change with
    the row, with no source-card rewriting. `v(s)` at t = 0.25 ms is
    amp * sin(2*pi*f*0.25ms): 1.0 at (1, 1 kHz) and 3 * sin(pi/4) at
    (3, 500 Hz)."""
    _write_body(
        tmp_path,
        ".param amp=1\n.param freq=1k\nV2 s 0 SIN(0 {amp} {freq})\nR4 s 0 1k\n",
    )
    request = _engine_request(
        tmp_path,
        analysis={"kind": "tran", "args": "1u 0.5m"},
        measurements=[
            {"name": "vs", "spice": ".meas tran vs FIND v(s) AT=0.25m", "unit": "V"}
        ],
        corners={"params": {"amp": [1, 3], "freq": [1000, 500]}},
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    first, second = _values(report, "vs")
    assert first == pytest.approx(1.0, abs=2e-3)
    assert second == pytest.approx(3 * 0.70710678, abs=2e-3)


@_SKIP_NO_NGSPICE
def test_real_ngspice_supply_override_wins_over_a_parameter_default(tmp_path):
    """A parameter sets a source's default; the explicit `supply_v` `alter`
    applied after the reset wins that source's value."""
    _write_body(
        tmp_path,
        ".param vsrc=1\n.param rload=1000\nV1 in 0 DC {vsrc}\nR1 in 0 {rload}\n",
    )
    request = _engine_request(
        tmp_path,
        corners={"supply_v": {"v1": [2.0]}, "params": {"vsrc": [5], "rload": [2000]}},
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    assert _values(report, "i1") == [pytest.approx(-2.0 / 2000, abs=1e-9)]


@_SKIP_NO_NGSPICE
def test_real_ngspice_reset_keeps_temperature_and_the_saved_vector_policy(tmp_path):
    """`reset` reloads the circuit: the corner's `.temp` and the generated
    `save all` (which supersedes the body's restrictive `.save`) must both
    survive it."""
    _write_body(
        tmp_path,
        ".param rload=1000\nV1 in 0 1\nR1 in mid {rload}\nR2 mid 0 1k\n.save v(in)\n",
    )
    request = _engine_request(
        tmp_path,
        corners={"temperature_c": [85], "params": {"rload": [3000]}},
        measurements=[
            {"name": "vmid", "expr": "v(mid)", "unit": "V"},
            {"name": "temp", "expr": "@r1[temp]", "unit": "C"},
        ],
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    assert _values(report, "vmid") == [pytest.approx(0.25, abs=1e-6)]
    assert _values(report, "temp") == [pytest.approx(85.0)]


@_SKIP_NO_NGSPICE
def test_real_ngspice_engine_refusal_of_a_target_fails_the_corner(tmp_path):
    """The runtime backstop: when the static scan cannot see the declaration
    (here the point is dispatched without the static check), the engine's own
    `alterparam ... not found` still grades the corner an error rather than
    letting it pass at the netlist default."""
    _write_body(tmp_path, ".param rload=1000\nV1 in 0 1\nR1 in 0 {rload}\n")
    points = [sim.CornerPoint(None, {}, 27, params={"bogus": 3})]
    corners, _, _ = sim._run_local(
        corner_points=points,
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        analysis={"kind": "op", "args": ""},
        measurements_spec=[{"name": "i1", "expr": "i(v1)", "unit": "A"}],
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "a"),
        max_workers=1,
        request={},
        deadline=None,
        initial_ppid=os.getppid(),
        checkpoint=None,
        probe_abort=None,
    )
    (corner,) = corners
    assert corner["status"] == "error"
    assert [d["code"] for d in corner["diagnostics"]] == ["netlist"]


@_SKIP_NO_NGSPICE
def test_real_ngspice_parameter_axis_through_local_parallel_and_monte_carlo(tmp_path):
    _write_body(tmp_path)
    request = _engine_request(
        tmp_path,
        backend="local-parallel",
        corners={"params": {"rload": [1000, 2000]}},
        monte_carlo={"n": 2, "seed": 5, "vary": "both"},
        options={"keep_artifacts": True, "max_workers": 2},
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    assert [c["corner_id"] for c in report["corners"]] == [
        "default/novdd/27C/params:rload=1000/mc0",
        "default/novdd/27C/params:rload=1000/mc1",
        "default/novdd/27C/params:rload=2000/mc0",
        "default/novdd/27C/params:rload=2000/mc1",
    ]
    assert [c["params"] for c in report["corners"]] == [
        {"rload": 1000},
        {"rload": 1000},
        {"rload": 2000},
        {"rload": 2000},
    ]
    assert _values(report, "i1")[2] == pytest.approx(-0.5e-3, abs=1e-9)


@_SKIP_NO_NGSPICE
def test_real_ngspice_parameter_axis_reaches_analysis_steps(tmp_path):
    _write_body(
        tmp_path, ".param rload=1000\nVIN in 0 3\nR1 in out {rload}\nR2 out 0 1k\n"
    )
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local",
            "analysis_steps": [
                {
                    "name": "lo",
                    "analysis": {"kind": "op", "args": ""},
                    "alter": {"VIN": 3.0},
                    "measurements": [{"name": "vout", "expr": "v(out)", "unit": "V"}],
                }
            ],
            "corners": {"params": {"rload": [1000, 2000]}},
        },
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    assert _values(report, "lo.vout") == [
        pytest.approx(1.5, abs=1e-6),
        pytest.approx(1.0, abs=1e-6),
    ]


@_SKIP_NO_NGSPICE
def test_real_ngspice_unknown_target_in_a_request_fails_visibly(tmp_path):
    _write_body(tmp_path)
    request = _engine_request(tmp_path, corners={"params": {"nope": [1]}})
    with pytest.raises(sim.SimError, match="'nope'"):
        sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
