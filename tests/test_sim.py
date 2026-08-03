"""Tests for `klt sim` and the `klayout_tools.sim` library.

Two tiers, per the issue's testing requirement (#91):

- **Unit tests** (the majority) exercise the corner-matrix expansion, log
  classification, `.meas` parsing, limit evaluation, rawfile parsing, and
  model-library resolution as pure functions -- either directly, or by
  stubbing `subprocess.run` so `run_sim`'s full per-corner pipeline is
  exercised without ever invoking the real `ngspice` binary. These always
  run, everywhere, and are the ones a classification/parsing regression
  actually gets caught by.
- **Integration tests** (`@pytest.mark.skipif(not HAVE_NGSPICE, ...)`) run
  the real `ngspice -b` subprocess end to end -- corner expansion, `.lib`
  process-corner selection, `alter`-based supply sweep, `.temp`, `.meas`
  extraction, and the optional waveform artifact -- against tiny, synthetic,
  non-PDK fixtures (see `examples/sim/generate.py`'s docstring for why no
  real PDK data is vendored here). CI installs `ngspice` via the package
  manager (`.github/workflows/ci.yml`) so these always run there; they skip
  with a clear reason on a dev machine without it, rather than silently
  passing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from klayout_tools import pdk, remote_transport, sim
from klayout_tools import remote_launcher as rl
from klayout_tools.cli import main

HAVE_NGSPICE = shutil.which("ngspice") is not None
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "sim"


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #


def _write_body(tmp_path: Path, name: str = "body.spice") -> Path:
    path = tmp_path / name
    path.write_text(".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n")
    return path


def _write_corner_lib(tmp_path: Path, name: str = "corner.lib") -> Path:
    path = tmp_path / name
    path.write_text(
        ".lib tt\n.param corner_scale=1.0\n.endl tt\n\n"
        ".lib ss\n.param corner_scale=0.9\n.endl ss\n"
    )
    return path


def _write_request(tmp_path: Path, request: dict, name: str = "request.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(request))
    return path


# --------------------------------------------------------------------------- #
# load_request
# --------------------------------------------------------------------------- #


def test_load_request_missing_file(tmp_path):
    with pytest.raises(sim.SimError, match="not found"):
        sim.load_request(str(tmp_path / "nope.json"))


def test_load_request_is_a_directory(tmp_path):
    with pytest.raises(sim.SimError, match="not a file"):
        sim.load_request(str(tmp_path))


def test_load_request_not_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("this is not json {{{")
    with pytest.raises(sim.SimError, match="not valid JSON"):
        sim.load_request(str(path))


def test_load_request_non_object(tmp_path):
    path = tmp_path / "array.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(sim.SimError, match="JSON object"):
        sim.load_request(str(path))


@pytest.mark.parametrize("missing_field", ["netlist", "analysis"])
def test_load_request_missing_required_field(tmp_path, missing_field):
    request = {"netlist": "x.spice", "analysis": {"kind": "tran", "args": "1n 1u"}}
    del request[missing_field]
    path = _write_request(tmp_path, request)
    with pytest.raises(sim.SimError, match=missing_field):
        sim.load_request(str(path))


def test_load_request_does_not_require_models_or_schema(tmp_path):
    # `models` is validated in run_sim (only when corners.process is set);
    # a bare "schema" field (the spike's shape) is never required.
    request = {"netlist": "x.spice", "analysis": {"kind": "tran", "args": "1n 1u"}}
    path = _write_request(tmp_path, request)
    assert sim.load_request(str(path)) == request


# --------------------------------------------------------------------------- #
# run_sim: request-level validation (raised before any corner runs)
# --------------------------------------------------------------------------- #


def test_run_sim_unsupported_engine_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported engine"):
        sim.run_sim(str(request))


def test_run_sim_unsupported_backend_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "bogus",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported backend"):
        sim.run_sim(str(request))


def test_run_sim_unsupported_backend_via_cli_flag_raises(tmp_path):
    # The --backend flag overrides the request field and is validated the
    # same way (unknown name -> SimError, not a silent fallback to local).
    # "remote" is a real, implemented backend as of #265 -- use a genuinely
    # unsupported name here instead.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported backend"):
        sim.run_sim(str(request), backend="quantum")


def test_run_sim_netlist_not_found_raises(tmp_path):
    request = _write_request(
        tmp_path,
        {"netlist": "missing.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    with pytest.raises(sim.SimError, match="netlist not found"):
        sim.run_sim(str(request))


def test_run_sim_process_corner_without_models_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {"process": ["tt"]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="models.lib"):
        sim.run_sim(str(request))


def test_run_sim_analysis_missing_fields_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran"}},
    )
    with pytest.raises(sim.SimError, match="analysis"):
        sim.run_sim(str(request))


def test_run_sim_measurement_missing_fields_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [{"name": "vout"}],
        },
    )
    with pytest.raises(sim.SimError, match="measurements"):
        sim.run_sim(str(request))


def test_run_sim_netlist_source_invalid_value_raises(tmp_path):
    # Per the JSON contract's error shape (docs/json-contract.md), an
    # unrecognized netlist_source is a loud application error (SimError,
    # exit 1) -- never silently ignored or coerced.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "netlist_source": "post-layout",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported netlist_source"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# `.meas op` rejection -- ngspice has no `.MEASURE OP` analysis type (#205)
# --------------------------------------------------------------------------- #


def test_validate_meas_card_rejects_op():
    with pytest.raises(sim.SimError, match=r"\.MEASURE OP"):
        sim._validate_meas_card("vout_meas", ".meas op vout_meas find v(out)")


def test_validate_meas_card_accepts_supported_types():
    # Every ngspice-implemented `.meas` type is accepted -- no SimError.
    for card in (
        ".meas dc vout find v(out) at=1.0",
        ".meas ac vout find v(out) at=1k",
        ".meas tran vout find v(out) at=1n",
        ".meas sp vout find v(out) at=1k",
        ".measure tran vout find v(out) at=1n",  # long-form spelling
    ):
        sim._validate_meas_card("vout", card)  # must not raise


def test_run_sim_analysis_kind_op_with_meas_op_card_raises_clear_error(tmp_path):
    # The exact pairing this issue reports: `analysis.kind: "op"` paired with
    # a `.meas op` card must fail with a clear, actionable SimError *before*
    # ngspice ever runs -- never a raw
    # "Error: unrecognized analysis type 'op'" ngspice parse failure.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "op", "args": ""},
            "measurements": [
                {"name": "vout_meas", "spice": ".meas op vout_meas find v(out)"}
            ],
        },
    )
    with pytest.raises(sim.SimError, match=r"\.MEASURE OP"):
        sim.run_sim(str(request))


def test_run_sim_meas_op_card_rejected_even_with_non_op_analysis_kind(tmp_path):
    # A `.meas op` card is invalid on ngspice's own terms regardless of the
    # request's `analysis.kind` -- not just when the two literally match.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1n"},
            "measurements": [
                {"name": "vout_meas", "spice": ".meas op vout_meas find v(out)"}
            ],
        },
    )
    with pytest.raises(sim.SimError, match="unsupported .meas analysis type"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Model-library resolution (klt pdk integration, #45)
# --------------------------------------------------------------------------- #


def test_resolve_models_lib_via_pdk_variant(tmp_path):
    install_root = tmp_path / "install"
    variant_dir = install_root / "sky130A" / "libs.tech" / "ngspice"
    variant_dir.mkdir(parents=True)
    lib_path = variant_dir / "sky130.lib.spice"
    lib_path.write_text(".lib tt\n.endl tt\n")
    # A minimal libs.tech marker so pdk.find_pdk recognises the variant.
    (install_root / "sky130A" / "libs.tech" / "klayout").mkdir()

    resolved = sim._resolve_models_lib(
        {
            "pdk": "sky130A",
            "pdk_root": str(install_root),
            "lib": "libs.tech/ngspice/sky130.lib.spice",
        },
        request_dir=str(tmp_path),
    )

    assert resolved == str(lib_path)


def test_resolve_models_lib_via_pdk_missing_install_raises(tmp_path):
    with pytest.raises(sim.SimError):
        sim._resolve_models_lib(
            {
                "pdk": "sky130A",
                "pdk_root": str(tmp_path / "nope"),
                "lib": "sky130.lib.spice",
            },
            request_dir=str(tmp_path),
        )


def test_resolve_models_lib_env_var_expansion(tmp_path, monkeypatch):
    lib_path = _write_corner_lib(tmp_path)
    monkeypatch.setenv("PDK_ROOT", str(tmp_path))

    resolved = sim._resolve_models_lib(
        {"lib": "$PDK_ROOT/corner.lib"}, request_dir=str(tmp_path)
    )

    assert resolved == str(lib_path)


def test_resolve_models_lib_relative_to_request_dir(tmp_path):
    _write_corner_lib(tmp_path)

    resolved = sim._resolve_models_lib({"lib": "corner.lib"}, request_dir=str(tmp_path))

    assert resolved == str(tmp_path / "corner.lib")


def test_resolve_models_lib_missing_lib_field_raises(tmp_path):
    with pytest.raises(sim.SimError, match="models.lib"):
        sim._resolve_models_lib({}, request_dir=str(tmp_path))


def test_resolve_models_lib_missing_file_raises(tmp_path):
    with pytest.raises(sim.SimError, match="not found"):
        sim._resolve_models_lib({"lib": "nope.lib"}, request_dir=str(tmp_path))


# --------------------------------------------------------------------------- #
# Corner-matrix expansion
# --------------------------------------------------------------------------- #


def test_expand_corners_defaults_to_single_point():
    points = sim._expand_corners({}, [])

    assert len(points) == 1
    (point,) = points
    assert point.process is None
    assert point.supply_v == {}
    assert point.temperature_c == 27
    assert point.corner_id == "default/novdd/27C"


def test_expand_corners_cross_product_order():
    points = sim._expand_corners(
        {
            "process": ["tt", "ss"],
            "supply_v": {"vdd": [1.62, 1.98]},
            "temperature_c": [-40, 125],
        },
        [],
    )

    # process outermost, temperature innermost -- odometer-style.
    ids = [p.corner_id for p in points]
    assert ids == [
        "tt/1.620V/-40C",
        "tt/1.620V/125C",
        "tt/1.980V/-40C",
        "tt/1.980V/125C",
        "ss/1.620V/-40C",
        "ss/1.620V/125C",
        "ss/1.980V/-40C",
        "ss/1.980V/125C",
    ]


def test_expand_corners_exclude_partial_match():
    points = sim._expand_corners(
        {"process": ["tt", "ss"], "temperature_c": [-40, 125]},
        [{"process": "ss", "temperature_c": -40}],
    )

    ids = [p.corner_id for p in points]
    assert "ss/novdd/-40C" not in ids
    assert len(ids) == 3


def test_expand_corners_supply_rails_move_together():
    points = sim._expand_corners(
        {"supply_v": {"vdd": [1.62, 1.98], "vdda": [1.7, 2.0]}}, []
    )

    assert len(points) == 2
    assert points[0].supply_v == {"vdd": 1.62, "vdda": 1.7}
    assert points[1].supply_v == {"vdd": 1.98, "vdda": 2.0}


def test_expand_corners_mismatched_supply_lengths_raises():
    with pytest.raises(sim.SimError, match="same length"):
        sim._expand_corners({"supply_v": {"vdd": [1.62, 1.98], "vdda": [1.7]}}, [])


# --------------------------------------------------------------------------- #
# corners.process bundle form: {"name": str, "sections": list[str]}
# --------------------------------------------------------------------------- #


def test_expand_corners_bare_string_process_is_unchanged():
    """Regression: a bare-string `corners.process` entry keeps producing a
    `CornerPoint` with `process_sections=None` -- the exact shape
    `_write_corner_deck` used before the bundle form existed."""
    points = sim._expand_corners({"process": ["tt", "ss"]}, [])

    assert [p.process for p in points] == ["tt", "ss"]
    assert [p.process_sections for p in points] == [None, None]
    assert [p.corner_id for p in points] == ["tt/novdd/27C", "ss/novdd/27C"]


def test_expand_corners_bundle_process_entry():
    points = sim._expand_corners(
        {
            "process": [
                "tt",
                {
                    "name": "ss",
                    "sections": [
                        "ss",
                        "bjt_ss",
                        "diode_ss",
                        "res_ss",
                        "moscap_ss",
                        "mimcap_ss",
                    ],
                },
            ]
        },
        [],
    )

    assert [p.process for p in points] == ["tt", "ss"]
    assert points[0].process_sections is None
    assert points[1].process_sections == [
        "ss",
        "bjt_ss",
        "diode_ss",
        "res_ss",
        "moscap_ss",
        "mimcap_ss",
    ]
    # corner_id/slug use the bundle's `name` -- report shape is unaffected.
    assert points[1].corner_id == "ss/novdd/27C"
    assert points[1].slug == "ss_novdd_27C"


def test_expand_corners_bundle_single_section_matches_bare_string_shape():
    """A single-section bundle is functionally equivalent to a bare string
    (see the issue's edge-case list) -- same `process`, same `corner_id`,
    just routed through the loop path in `_write_corner_deck` instead of the
    single-line path."""
    (bare,) = sim._expand_corners({"process": ["tt"]}, [])
    (bundle,) = sim._expand_corners(
        {"process": [{"name": "tt", "sections": ["tt"]}]}, []
    )

    assert bare.process == bundle.process == "tt"
    assert bare.corner_id == bundle.corner_id


@pytest.mark.parametrize(
    "entry,match",
    [
        ({"sections": ["tt"]}, "name"),
        ({"name": "", "sections": ["tt"]}, "name"),
        ({"name": 5, "sections": ["tt"]}, "name"),
        ({"name": "tt"}, "sections"),
        ({"name": "tt", "sections": []}, "sections"),
        ({"name": "tt", "sections": "tt"}, "sections"),
        ({"name": "tt", "sections": [1, 2]}, "sections"),
        ({"name": "tt", "sections": ["tt", ""]}, "sections"),
        (5, "string or an object"),
    ],
)
def test_expand_corners_bundle_process_entry_validation(entry, match):
    with pytest.raises(sim.SimError, match=match):
        sim._expand_corners({"process": [entry]}, [])


def test_expand_corners_exclude_matches_bundle_corner_by_name():
    """`corners.exclude[].process` compares against the bundle's `name`, not
    the raw `{"name", "sections"}` object -- an object `!=` a bare string
    would silently never match without this."""
    points = sim._expand_corners(
        {
            "process": [
                "tt",
                {"name": "ss", "sections": ["ss", "bjt_ss"]},
            ],
            "temperature_c": [-40, 125],
        },
        [{"process": "ss", "temperature_c": -40}],
    )

    ids = [p.corner_id for p in points]
    assert "ss/novdd/-40C" not in ids
    assert len(ids) == 3


# --------------------------------------------------------------------------- #
# _write_corner_deck: .lib card generation
# --------------------------------------------------------------------------- #


def _write_deck(tmp_path: Path, point: sim.CornerPoint, **overrides) -> list[str]:
    deck_path = tmp_path / "deck.spice"
    kwargs = {
        "deck_path": str(deck_path),
        "netlist_path": str(tmp_path / "body.spice"),
        "models_lib": str(tmp_path / "corner.lib"),
        "point": point,
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements_spec": [],
        "raw_path": None,
    }
    kwargs.update(overrides)
    sim._write_corner_deck(**kwargs)
    return deck_path.read_text().splitlines()


def test_write_corner_deck_bare_string_process_emits_one_lib_line(tmp_path):
    point = sim.CornerPoint("tt", {}, 27)
    lines = _write_deck(tmp_path, point)

    lib_lines = [line for line in lines if line.startswith(".lib")]
    assert lib_lines == [f".lib {tmp_path / 'corner.lib'} tt"]


def test_write_corner_deck_bundle_process_emits_one_lib_line_per_section_in_order(
    tmp_path,
):
    point = sim.CornerPoint(
        "ss",
        {},
        27,
        process_sections=[
            "ss",
            "bjt_ss",
            "diode_ss",
            "res_ss",
            "moscap_ss",
            "mimcap_ss",
        ],
    )
    lines = _write_deck(tmp_path, point)

    lib_lines = [line for line in lines if line.startswith(".lib")]
    models_lib = tmp_path / "corner.lib"
    assert lib_lines == [
        f".lib {models_lib} ss",
        f".lib {models_lib} bjt_ss",
        f".lib {models_lib} diode_ss",
        f".lib {models_lib} res_ss",
        f".lib {models_lib} moscap_ss",
        f".lib {models_lib} mimcap_ss",
    ]


def test_write_corner_deck_no_process_emits_no_lib_line(tmp_path):
    point = sim.CornerPoint(None, {}, 27)
    lines = _write_deck(tmp_path, point)

    assert not any(line.startswith(".lib") for line in lines)


def test_corner_id_multi_rail_format():
    point = sim.CornerPoint("tt", {"vdd": 1.8, "vdda": 1.7}, 27)
    assert point.corner_id == "tt/vdd=1.800_vdda=1.700V/27C"


def test_corner_slug_is_filesystem_safe():
    point = sim.CornerPoint("ss", {"vdd": 1.62}, -40)
    assert "/" not in point.slug
    assert point.slug == "ss_1p620V_n40C"


def test_corner_id_and_slug_get_sample_suffix():
    point = sim.CornerPoint(
        "tt",
        {"vdd": 1.8},
        27,
        sample_index=3,
        mc_seed={"process_seed": 1, "mismatch_seed": 2, "rndseed": 3},
    )
    assert point.corner_id == "tt/1.800V/27C/mc3"
    assert point.slug == "tt_1p800V_27C_mc3"


# --------------------------------------------------------------------------- #
# Monte Carlo sampling: request validation, seed contract, negative control
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "mc_spec,match",
    [
        ({"seed": 1, "vary": "mismatch"}, "monte_carlo.n"),
        ({"n": 0, "seed": 1, "vary": "mismatch"}, "monte_carlo.n"),
        ({"n": "5", "seed": 1, "vary": "mismatch"}, "monte_carlo.n"),
        ({"n": 5, "vary": "mismatch"}, "monte_carlo.seed"),
        ({"n": 5, "seed": "abc", "vary": "mismatch"}, "monte_carlo.seed"),
        ({"n": 5, "seed": 1}, "monte_carlo.vary"),
        ({"n": 5, "seed": 1, "vary": "bogus"}, "monte_carlo.vary"),
    ],
)
def test_validate_monte_carlo_spec_rejects_invalid(mc_spec, match):
    with pytest.raises(sim.SimError, match=match):
        sim._validate_monte_carlo_spec(mc_spec)


def test_validate_monte_carlo_spec_accepts_every_vary_value():
    for vary in sim.SUPPORTED_MC_VARY:
        assert sim._validate_monte_carlo_spec({"n": 3, "seed": 42, "vary": vary}) == (
            3,
            42,
            vary,
        )


def test_expand_monte_carlo_produces_n_samples_per_corner():
    corner_points = sim._expand_corners({"process": ["tt", "ss"]}, [])
    sampled, info = sim._expand_monte_carlo(
        corner_points, {"n": 3, "seed": 1, "vary": "both"}
    )

    assert info == {"n": 3, "seed": 1, "vary": "both"}
    assert len(sampled) == 6  # 2 corners x 3 samples
    ids = [p.corner_id for p in sampled]
    assert len(set(ids)) == 6  # every corner_id/artifact path is unique
    assert ids == [
        "tt/novdd/27C/mc0",
        "tt/novdd/27C/mc1",
        "tt/novdd/27C/mc2",
        "ss/novdd/27C/mc0",
        "ss/novdd/27C/mc1",
        "ss/novdd/27C/mc2",
    ]


def test_expand_monte_carlo_is_reproducible_given_the_same_seed():
    corner_points = sim._expand_corners({"process": ["tt"]}, [])
    mc_spec = {"n": 5, "seed": 20260801, "vary": "both"}

    sampled_a, _ = sim._expand_monte_carlo(corner_points, mc_spec)
    sampled_b, _ = sim._expand_monte_carlo(corner_points, mc_spec)

    seeds_a = [p.mc_seed for p in sampled_a]
    seeds_b = [p.mc_seed for p in sampled_b]
    assert seeds_a == seeds_b
    # Not a broken/no-op sampler: consecutive samples actually differ.
    assert len({tuple(s.values()) for s in seeds_a}) == 5


def test_expand_monte_carlo_different_seed_diverges():
    corner_points = sim._expand_corners({}, [])
    sampled_a, _ = sim._expand_monte_carlo(
        corner_points, {"n": 3, "seed": 1, "vary": "both"}
    )
    sampled_b, _ = sim._expand_monte_carlo(
        corner_points, {"n": 3, "seed": 2, "vary": "both"}
    )
    assert [p.mc_seed for p in sampled_a] != [p.mc_seed for p in sampled_b]


@pytest.mark.parametrize(
    "vary,varying_key,pinned_key",
    [
        ("process", "process_seed", "mismatch_seed"),
        ("mismatch", "mismatch_seed", "process_seed"),
    ],
)
def test_expand_monte_carlo_negative_control(vary, varying_key, pinned_key):
    """The deterministic negative control this issue requires: the axis
    `vary` does *not* request stays identical (sigma=0) across every sample
    of a corner, while the requested axis actually varies -- a broken/no-op
    sampler that always returns the same seeds for both axes (or that
    varies both regardless of `vary`) fails this test."""
    corner_points = sim._expand_corners({}, [])
    sampled, _ = sim._expand_monte_carlo(
        corner_points, {"n": 4, "seed": 7, "vary": vary}
    )

    pinned_values = {p.mc_seed[pinned_key] for p in sampled}
    assert len(pinned_values) == 1  # sigma=0: identical across every sample

    varying_values = {p.mc_seed[varying_key] for p in sampled}
    assert len(varying_values) == 4  # actually varies, not silently pinned too


def test_expand_monte_carlo_both_varies_every_axis():
    corner_points = sim._expand_corners({}, [])
    sampled, _ = sim._expand_monte_carlo(
        corner_points, {"n": 4, "seed": 7, "vary": "both"}
    )
    assert len({p.mc_seed["process_seed"] for p in sampled}) == 4
    assert len({p.mc_seed["mismatch_seed"] for p in sampled}) == 4


def test_expand_monte_carlo_negative_control_is_per_corner():
    # The pinned axis is constant *within* a corner's own sample set, but
    # different corners still derive independent (not globally identical)
    # pinned values -- the negative control isn't a single hard-coded
    # sentinel that would mask a broken corner_index derivation.
    corner_points = sim._expand_corners({"process": ["tt", "ss"]}, [])
    sampled, _ = sim._expand_monte_carlo(
        corner_points, {"n": 3, "seed": 7, "vary": "process"}
    )
    by_process = {"tt": [], "ss": []}
    for point in sampled:
        by_process[point.process].append(point.mc_seed["mismatch_seed"])

    assert len(set(by_process["tt"])) == 1
    assert len(set(by_process["ss"])) == 1
    assert by_process["tt"][0] != by_process["ss"][0]


# --------------------------------------------------------------------------- #
# .meas log parsing
# --------------------------------------------------------------------------- #


def test_parse_measurements_success():
    log = (
        "  Measurements for Transient Analysis\n\n"
        "vout_final          =  1.00000e+00\n"
        "vout_avg            =  9.50000e-01 from=  5.00000e-06 to=  1.00000e-05\n"
    )
    assert sim._parse_measurements(log) == {"vout_final": 1.0, "vout_avg": 0.95}


def test_parse_measurements_failed_is_absent():
    log = (
        "  Measurements for Transient Analysis\n\n\n"
        "Error: measure  vout_high  find(AT) : out of interval\n"
        " .meas tran vout_high find v(out) when v(out)=5 failed!\n"
    )
    assert sim._parse_measurements(log) == {}


def test_parse_measurements_ignores_unrelated_lines():
    log = (
        "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n"
        "Node                                   Voltage\n"
        "vdd                                          1\n"
        "No. of Data Rows : 10008\n"
    )
    assert sim._parse_measurements(log) == {}


# --------------------------------------------------------------------------- #
# Diagnostic classification (from log text, never exit code)
# --------------------------------------------------------------------------- #


def test_classify_diagnostics_singular_matrix():
    log = "Warning: singular matrix:  check node b\n"
    codes = [d["code"] for d in sim._classify_diagnostics(log)]
    assert codes == ["singular_matrix"]


def test_classify_diagnostics_nonconvergence():
    log = "Warning: Dynamic gmin stepping failed\n"
    codes = [d["code"] for d in sim._classify_diagnostics(log)]
    assert "nonconvergence" in codes


def test_classify_diagnostics_netlist_error():
    log = "Error: unknown subckt: foo\n"
    codes = [d["code"] for d in sim._classify_diagnostics(log)]
    assert "netlist" in codes


def test_classify_diagnostics_clean_log_is_empty():
    log = "Note: Transient op finished successfully\nngspice-46 done\n"
    assert sim._classify_diagnostics(log) == []


def test_classify_diagnostics_does_not_false_positive_on_title_text():
    # A netlist comment that happens to mention "singular matrix" in prose
    # (not an actual `Warning:` line) must not be misclassified.
    log = (
        "Circuit: * a singular matrix test circuit\n"
        "Note: Transient op finished successfully\n"
    )
    assert sim._classify_diagnostics(log) == []


# --------------------------------------------------------------------------- #
# Recovered singular_matrix/nonconvergence classification (#205)
# --------------------------------------------------------------------------- #


def test_recovered_from_stepping_true_when_measurements_all_resolved():
    log = (
        "Warning: singular matrix:  check node b\n"
        "Note: Transient op finished successfully\n"
    )
    measurements_spec = [{"name": "vout", "spice": ".meas tran vout find v(out) at=1n"}]
    measurement_results = [{"name": "vout", "value": 1.0, "status": "pass"}]
    assert sim._recovered_from_stepping(measurements_spec, measurement_results, log)


def test_recovered_from_stepping_false_with_no_measurements_declared():
    # No measurements[] at all -- nothing independently confirms the run
    # actually succeeded, so a singular-matrix classification stays fatal.
    log = "Warning: singular matrix:  check node b\n"
    assert sim._recovered_from_stepping([], [], log) is False


def test_recovered_from_stepping_false_when_a_measurement_errored():
    log = "Warning: singular matrix:  check node b\n"
    measurements_spec = [{"name": "vout", "spice": ".meas tran vout find v(out) at=1n"}]
    measurement_results = [{"name": "vout", "value": None, "status": "error"}]
    assert (
        sim._recovered_from_stepping(measurements_spec, measurement_results, log)
        is False
    )


def test_recovered_from_stepping_false_on_simulation_aborted_trailer():
    log = (
        "Warning: singular matrix:  check node b\n"
        "doAnalyses: TRAN:  Timestep too small\n"
        "tran simulation(s) aborted\n"
    )
    measurements_spec = [{"name": "vout", "spice": ".meas tran vout find v(out) at=1n"}]
    # Even if a stale measurement value somehow parsed, the abort trailer
    # alone is decisive.
    measurement_results = [{"name": "vout", "value": 1.0, "status": "pass"}]
    assert (
        sim._recovered_from_stepping(measurements_spec, measurement_results, log)
        is False
    )


# --------------------------------------------------------------------------- #
# Limit evaluation (margin sign convention)
# --------------------------------------------------------------------------- #


def test_evaluate_limits_no_limits_never_fails():
    status, margin = sim._evaluate_limits(1.0, None)
    assert status == "pass"
    assert margin is None


def test_evaluate_limits_pass_within_bounds():
    status, margin = sim._evaluate_limits(1.20, {"min": 1.19, "max": 1.21})
    assert status == "pass"
    assert margin == pytest.approx(0.01)  # nearest binding limit, positive


def test_evaluate_limits_fail_below_min():
    status, margin = sim._evaluate_limits(1.10, {"min": 1.19, "max": 1.21})
    assert status == "fail"
    assert margin < 0


def test_evaluate_limits_fail_above_max():
    status, margin = sim._evaluate_limits(5e-6, {"max": 4e-6})
    assert status == "fail"
    assert margin == pytest.approx(-1e-6)


def test_evaluate_limits_pass_min_only():
    status, margin = sim._evaluate_limits(2.0, {"min": 1.0})
    assert status == "pass"
    assert margin == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Measurement rollup (worst-case selection, aggregate status)
# --------------------------------------------------------------------------- #


def test_rollup_measurements_worst_case_is_most_negative_margin():
    corners = [
        {
            "corner_id": "a",
            "measurements": [
                {"name": "vref", "value": 1.20, "margin": 0.01, "status": "pass"}
            ],
        },
        {
            "corner_id": "b",
            "measurements": [
                {"name": "vref", "value": 1.25, "margin": -0.04, "status": "fail"}
            ],
        },
    ]
    rollup = sim._rollup_measurements(
        [{"name": "vref", "unit": "V", "limits": {"max": 1.21}}], corners
    )

    (entry,) = rollup
    assert entry["status"] == "fail"
    assert entry["worst_case"]["corner_id"] == "b"
    assert entry["worst_case"]["value"] == 1.25


def test_rollup_measurements_error_outranks_fail():
    corners = [
        {
            "corner_id": "a",
            "measurements": [
                {"name": "iq", "value": None, "margin": None, "status": "error"}
            ],
        },
        {
            "corner_id": "b",
            "measurements": [
                {"name": "iq", "value": 6e-6, "margin": -1e-6, "status": "fail"}
            ],
        },
    ]
    rollup = sim._rollup_measurements(
        [{"name": "iq", "unit": "A", "limits": {"max": 5e-6}}], corners
    )

    assert rollup[0]["status"] == "error"


# --------------------------------------------------------------------------- #
# ASCII rawfile -> waveform JSON
# --------------------------------------------------------------------------- #

_ASCII_RAWFILE = """Title: * rawfile test
Date: Fri Jul 31 12:00:00  2026
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 3
Variables:
\t0\ttime\ttime
\t1\tv(out)\tvoltage
Values:
 0\t0.000000000000000e+00
\t1.000000000000000e+00

 1\t1.000000000000000e-11
\t1.000000000000000e+00

 2\t2.000000000000000e-11
\t9.900000000000000e-01

"""


def test_parse_ascii_rawfile(tmp_path):
    path = tmp_path / "waveform.raw"
    path.write_text(_ASCII_RAWFILE)

    waveform = sim.parse_ascii_rawfile(str(path))

    assert waveform["plotname"] == "Transient Analysis"
    assert waveform["variables"] == [
        {"index": 0, "name": "time", "type": "time"},
        {"index": 1, "name": "v(out)", "type": "voltage"},
    ]
    assert waveform["points"] == [
        [0.0, 1.0],
        [1e-11, 1.0],
        [2e-11, 0.99],
    ]


def test_parse_ascii_rawfile_not_a_rawfile_raises(tmp_path):
    path = tmp_path / "notraw.txt"
    path.write_text("hello world\n")
    with pytest.raises(sim.SimError):
        sim.parse_ascii_rawfile(str(path))


def test_parse_ascii_rawfile_missing_file_raises(tmp_path):
    with pytest.raises(sim.SimError):
        sim.parse_ascii_rawfile(str(tmp_path / "nope.raw"))


# --------------------------------------------------------------------------- #
# run_sim with a stubbed ngspice subprocess (no binary required)
# --------------------------------------------------------------------------- #


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _stub_subprocess_run(
    monkeypatch,
    *,
    log_text: str = "",
    stdout: str = "** ngspice-99\n",
    side_effect=None,
):
    def fake_run(cmd, capture_output, text, timeout):
        log_path = cmd[cmd.index("-o") + 1]
        if side_effect is not None:
            raise side_effect
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(log_text)
        return _FakeCompleted(stdout)

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def test_run_sim_stubbed_missing_binary_is_corner_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["status"] == "error"
    codes = [d["code"] for d in corner["diagnostics"]]
    assert "unknown" in codes


def test_run_sim_stubbed_timeout_is_corner_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"timeout_s": 5},
        },
    )
    _stub_subprocess_run(
        monkeypatch, side_effect=subprocess.TimeoutExpired(cmd=["ngspice"], timeout=5)
    )

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["status"] == "error"
    codes = [d["code"] for d in corner["diagnostics"]]
    assert codes == ["timeout"]


def test_run_sim_stubbed_pass(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                    "limits": {"min": 0.9, "max": 1.1},
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    report = sim.run_sim(str(request))

    assert report["schema_version"] == 1
    assert report["status"] == "pass"
    assert report["passed"] == 1
    assert report["environment"]["engine_version"] == "99"
    (corner,) = report["corners"]
    assert corner["measurements"][0]["value"] == 1.0
    assert corner["measurements"][0]["status"] == "pass"


@pytest.mark.parametrize("netlist_source", ["schematic", "extracted"])
def test_run_sim_stubbed_netlist_source_echoed_in_environment(
    tmp_path, monkeypatch, netlist_source
):
    # request.netlist_source, when present and valid, is echoed verbatim
    # into environment.netlist_source (see docs/cli/sim.md's "Post-layout
    # verification" section) -- this is how a caller distinguishes a
    # pre-layout (S6) sim pass from a post-layout (S9) one.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "netlist_source": netlist_source,
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["environment"]["netlist_source"] == netlist_source


def test_run_sim_stubbed_netlist_source_absent_omits_environment_key(
    tmp_path, monkeypatch
):
    # Omitting netlist_source is unchanged, backward-compatible behavior:
    # no netlist_source key appears in environment at all (not null, not
    # an empty string) and every other field/behavior is unaffected.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert "netlist_source" not in report["environment"]
    assert report["status"] == "pass"


def test_run_sim_stubbed_provenance_pins_model_library(tmp_path, monkeypatch):
    """A process-axis sweep resolves a model library; the shared provenance
    block pins it as the run's `deck` with a `sha256:` content hash."""
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {"process": ["tt"]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request))

    prov = report["provenance"]
    assert set(prov.keys()) == {
        "klt_version",
        "klayout_version",
        "pdk",
        "deck",
        "input",
    }
    # No PDK variant declared in `models`, so no PDK is resolved.
    assert prov["pdk"] is None
    assert prov["deck"]["name"] == "corner.lib"
    assert prov["deck"]["content_hash"].startswith("sha256:")
    # Issue #331 added `provenance.input`, but `sim` wasn't in scope for it --
    # stays null here.
    assert prov["input"] is None


# --------------------------------------------------------------------------- #
# `monte_carlo` via `run_sim`, stubbed ngspice subprocess (#348)
# --------------------------------------------------------------------------- #


def _mc_request(tmp_path: Path, monte_carlo: dict, **overrides) -> Path:
    _write_body(tmp_path)
    request: dict[str, object] = {
        "netlist": "body.spice",
        "monte_carlo": monte_carlo,
        "analysis": {"kind": "tran", "args": "1n 1u"},
    }
    if overrides.pop("two_corners", False):
        _write_corner_lib(tmp_path)
        request["models"] = {"lib": "corner.lib"}
        request["corners"] = {"process": ["tt", "ss"]}
    request.update(overrides)
    return _write_request(tmp_path, request)


@pytest.mark.parametrize("missing_field", ["n", "seed", "vary"])
def test_run_sim_monte_carlo_missing_field_raises(tmp_path, missing_field):
    monte_carlo = {"n": 5, "seed": 1, "vary": "mismatch"}
    del monte_carlo[missing_field]
    request = _mc_request(tmp_path, monte_carlo)

    with pytest.raises(sim.SimError, match=f"monte_carlo.{missing_field}"):
        sim.run_sim(str(request))


def test_run_sim_monte_carlo_bad_vary_raises(tmp_path):
    request = _mc_request(tmp_path, {"n": 5, "seed": 1, "vary": "bogus"})

    with pytest.raises(sim.SimError, match="monte_carlo.vary"):
        sim.run_sim(str(request))


def test_run_sim_monte_carlo_expands_n_times_m_corners(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path, {"n": 3, "seed": 1, "vary": "both"}, two_corners=True
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["corner_count"] == 6  # 2 corners x 3 samples
    ids = [c["corner_id"] for c in report["corners"]]
    assert len(set(ids)) == 6
    assert report["environment"]["monte_carlo"] == {
        "n": 3,
        "seed": 1,
        "vary": "both",
    }
    for corner in report["corners"]:
        assert corner["monte_carlo"]["sample_index"] in (0, 1, 2)
        assert isinstance(corner["monte_carlo"]["seed"], int)


def test_run_sim_monte_carlo_omitted_leaves_corner_field_null(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert "monte_carlo" not in report["environment"]
    (corner,) = report["corners"]
    assert corner["monte_carlo"] is None


def test_run_sim_monte_carlo_reproducible_across_runs(tmp_path, monkeypatch):
    request = _mc_request(tmp_path, {"n": 4, "seed": 20260801, "vary": "both"})
    _stub_subprocess_run(monkeypatch)

    report_a = sim.run_sim(str(request))
    report_b = sim.run_sim(str(request))

    mc_a = [(c["corner_id"], c["monte_carlo"]) for c in report_a["corners"]]
    mc_b = [(c["corner_id"], c["monte_carlo"]) for c in report_b["corners"]]
    assert mc_a == mc_b


def test_run_sim_monte_carlo_negative_control_via_response(tmp_path, monkeypatch):
    request = _mc_request(tmp_path, {"n": 4, "seed": 20260801, "vary": "process"})
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    mismatch_seeds = {c["monte_carlo"]["mismatch_seed"] for c in report["corners"]}
    process_seeds = {c["monte_carlo"]["process_seed"] for c in report["corners"]}
    assert len(mismatch_seeds) == 1  # not requested -> pinned, sigma=0
    assert len(process_seeds) == 4  # requested -> actually varies


def test_run_sim_monte_carlo_unique_artifact_paths(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 3, "seed": 1, "vary": "mismatch"},
        options={"keep_artifacts": True},
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    log_paths = [c["artifacts"]["log"] for c in report["corners"]]
    assert len(log_paths) == 3
    assert len(set(log_paths)) == 3  # every sample got its own artifact dir
    for path in log_paths:
        assert os.path.isfile(path)


def test_run_sim_monte_carlo_deck_carries_seed_and_param_cards(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 1, "seed": 20260801, "vary": "mismatch"},
        options={"keep_artifacts": True},
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    deck_path = os.path.join(os.path.dirname(corner["artifacts"]["log"]), "corner.cir")
    deck_text = Path(deck_path).read_text()
    mc = corner["monte_carlo"]
    assert f".options seed={mc['seed']}" in deck_text
    assert f".param mc_process_seed={mc['process_seed']}" in deck_text
    assert f".param mc_mismatch_seed={mc['mismatch_seed']}" in deck_text
    # `.options seed=` must precede any `.lib`/`.include` card -- it seeds
    # AGAUSS/GAUSS calls evaluated while the netlist is parsed, before the
    # `.control` block ever runs.
    assert deck_text.index(".options seed=") < deck_text.index(".include")


def _stripped_report(report: dict) -> dict:
    """Drop the two fields the docs flag as legitimately varying between
    runs (`runtime_s`, `engine_version`) so the rest of the report can be
    compared for byte-identical equality (see docs/cli/sim.md's
    byte-exact-fixture caveat)."""
    clone = json.loads(json.dumps(report))
    clone["environment"].pop("engine_version", None)
    for corner in clone["corners"]:
        corner.pop("runtime_s", None)
    return clone


def test_run_sim_local_backend_is_byte_identical_to_default(tmp_path, monkeypatch):
    # The `local` backend (explicit, and via the --backend flag override)
    # must reproduce the pre-seam default behaviour exactly: same report
    # JSON, same corner ordering. Uses a multi-corner matrix so ordering is
    # actually exercised, not just a single-corner smoke test.
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    base = {
        "netlist": "body.spice",
        "models": {"lib": "corner.lib"},
        "corners": {
            "process": ["tt", "ss"],
            "temperature_c": [-40, 125],
        },
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
    }
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    default_req = _write_request(tmp_path, base, name="default.json")
    explicit_req = _write_request(
        tmp_path, {**base, "backend": "local"}, name="explicit.json"
    )

    default_report = sim.run_sim(str(default_req))
    explicit_report = sim.run_sim(str(explicit_req))
    flag_report = sim.run_sim(str(default_req), backend="local")

    # `netlist` echoes the request path field, identical across all three.
    assert _stripped_report(default_report) == _stripped_report(explicit_report)
    assert _stripped_report(default_report) == _stripped_report(flag_report)

    # Ordering is the odometer process x temperature expansion, unchanged.
    assert [c["corner_id"] for c in default_report["corners"]] == [
        "tt/novdd/-40C",
        "tt/novdd/125C",
        "ss/novdd/-40C",
        "ss/novdd/125C",
    ]


# --------------------------------------------------------------------------- #
# local-parallel backend (#255)
# --------------------------------------------------------------------------- #


def test_run_sim_local_parallel_backend_is_order_identical_to_local(
    tmp_path, monkeypatch
):
    # Corners share nothing, so `local-parallel` must reassemble results in
    # the same odometer order `local` produces, regardless of which corner's
    # subprocess happens to finish first. Sleep inversely with temperature
    # so corners complete in *reverse* submission order under the pool,
    # actually exercising reassembly rather than accidentally passing
    # because completion order matched submission order.
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "corners": {"temperature_c": [10, 20, 30, 40]},
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
        "options": {"max_workers": 4},
    }

    def fake_run(cmd, capture_output, text, timeout):
        deck_path = cmd[2]
        log_path = cmd[cmd.index("-o") + 1]
        deck_text = Path(deck_path).read_text()
        temp = float(re.search(r"\.temp\s+(-?[\d.]+)", deck_text).group(1))
        time.sleep(max(0.0, 40.0 - temp) * 0.005)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "  Measurements for Transient Analysis\n\n"
                "vout                =  1.00000e+00\n"
            )
        return _FakeCompleted("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    parallel_req = _write_request(
        tmp_path, {**base, "backend": "local-parallel"}, name="parallel.json"
    )
    local_req = _write_request(tmp_path, base, name="local.json")

    parallel_report = sim.run_sim(str(parallel_req))
    local_report = sim.run_sim(str(local_req))

    assert [c["corner_id"] for c in parallel_report["corners"]] == [
        "default/novdd/10C",
        "default/novdd/20C",
        "default/novdd/30C",
        "default/novdd/40C",
    ]
    assert [c["corner_id"] for c in parallel_report["corners"]] == [
        c["corner_id"] for c in local_report["corners"]
    ]
    assert _stripped_report(parallel_report) == _stripped_report(local_report)


def test_run_sim_local_parallel_failing_corner_does_not_abort_siblings(
    tmp_path, monkeypatch
):
    # A corner that times out (or otherwise errors) is reported exactly as
    # `local` reports it, and the other corners in the same pool still
    # complete and report their own real status.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "corners": {"temperature_c": [10, 20, 30]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
            "options": {"max_workers": 3},
        },
    )

    def fake_run(cmd, capture_output, text, timeout):
        deck_path = cmd[2]
        log_path = cmd[cmd.index("-o") + 1]
        deck_text = Path(deck_path).read_text()
        temp = float(re.search(r"\.temp\s+(-?[\d.]+)", deck_text).group(1))
        if temp == 20:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "  Measurements for Transient Analysis\n\n"
                "vout                =  1.00000e+00\n"
            )
        return _FakeCompleted("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    assert report["passed"] == 2
    assert report["errored"] == 1
    statuses = {c["corner_id"]: c["status"] for c in report["corners"]}
    assert statuses == {
        "default/novdd/10C": "pass",
        "default/novdd/20C": "error",
        "default/novdd/30C": "pass",
    }
    (errored_corner,) = [c for c in report["corners"] if c["status"] == "error"]
    assert any(d["code"] == "timeout" for d in errored_corner["diagnostics"])


def test_run_sim_local_parallel_default_max_workers_is_used(tmp_path, monkeypatch):
    # No explicit `max_workers` (request field or --flag) -> the backend
    # falls back to `_default_max_workers()`, never an unbounded pool.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_workers = {}
    real_pool = sim.ThreadPoolExecutor

    class _RecordingPool(real_pool):
        def __init__(self, max_workers=None, *args, **kwargs):
            seen_workers["max_workers"] = max_workers
            super().__init__(*args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(sim, "ThreadPoolExecutor", _RecordingPool)
    monkeypatch.setattr(sim, "_default_max_workers", lambda: 2)

    sim.run_sim(str(request))

    assert seen_workers["max_workers"] == 2


def test_run_sim_max_workers_flag_overrides_request_field(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"max_workers": 5},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_workers = {}
    real_pool = sim.ThreadPoolExecutor

    class _RecordingPool(real_pool):
        def __init__(self, max_workers=None, *args, **kwargs):
            seen_workers["max_workers"] = max_workers
            super().__init__(*args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(sim, "ThreadPoolExecutor", _RecordingPool)

    sim.run_sim(str(request), max_workers=1)

    assert seen_workers["max_workers"] == 1


@pytest.mark.parametrize("bad_value", [0, -1, 1.5, "3"])
def test_run_sim_invalid_max_workers_raises(tmp_path, bad_value):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"max_workers": bad_value},
        },
    )
    with pytest.raises(sim.SimError, match="max_workers"):
        sim.run_sim(str(request))


def test_default_max_workers_derives_from_cpu_count(monkeypatch):
    monkeypatch.setattr(sim.os, "cpu_count", lambda: 32)
    assert sim._default_max_workers() == 4

    monkeypatch.setattr(sim.os, "cpu_count", lambda: 1)
    assert sim._default_max_workers() == 1

    monkeypatch.setattr(sim.os, "cpu_count", lambda: None)
    assert sim._default_max_workers() == 1


def test_run_sim_stubbed_fail(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 0.5},
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "fail"
    assert report["failed"] == 1


def test_run_sim_stubbed_missing_measurement_is_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch, log_text="  Measurements for Transient Analysis\n\n"
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["measurements"][0]["value"] is None
    assert corner["measurements"][0]["status"] == "error"
    assert any(d["code"] == "measurement" for d in corner["diagnostics"])


#: A real ngspice-46 log (captured against the sky130 5T OTA composed by
#: `klt gen-compose`, per this issue's own repro) where gmin/source stepping
#: recovers from a singular DC operating-point matrix and the transient
#: still completes, producing correct measurement values -- yet also logs
#: "gmin stepping failed"/"source stepping failed" text along the way,
#: which is exactly why the fix cannot look at `singular_matrix` alone (see
#: `_recovered_from_stepping`'s docstring).
_RECOVERED_SINGULAR_MATRIX_LOG = (
    "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n\n"
    "Using SPARSE 1.3 as Direct Linear Solver\n"
    "Warning: singular matrix:  check node xota.g1\n\n"
    "Note: Starting dynamic gmin stepping\n"
    "Warning: singular matrix:  check node xota.g1\n\n"
    "Warning: Dynamic gmin stepping failed\n"
    "Note: Starting true gmin stepping\n"
    "Warning: singular matrix:  check node xota.g1\n\n"
    "Warning: True gmin stepping failed\n"
    "Note: Starting source stepping\n"
    "Warning: source stepping failed\n"
    "Note: Transient op started\n"
    "Note: Transient op finished successfully\n\n"
    "  Measurements for Transient Analysis\n\n"
    "vout_meas           =  1.00000e+00\n"
    "tail_meas           =  5.00000e-01\n"
)


def test_run_sim_stubbed_recovered_singular_matrix_reports_pass(tmp_path, monkeypatch):
    # The exact false positive this issue reports: a `singular matrix`
    # warning that ngspice's own gmin/source stepping recovers from, still
    # producing correct measurement values, must report `status: "pass"`,
    # not `"error"`.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1n"},
            "measurements": [
                {
                    "name": "vout_meas",
                    "spice": ".meas tran vout_meas find v(vout_node) at=1n",
                    "limits": {"min": 0.9, "max": 1.1},
                },
                {
                    "name": "tail_meas",
                    "spice": ".meas tran tail_meas find v(tail_node) at=1n",
                    "limits": {"min": 0.4, "max": 0.6},
                },
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    (corner,) = report["corners"]
    assert corner["status"] == "pass"
    assert corner["measurements"][0]["value"] == 1.0
    assert corner["measurements"][1]["value"] == 0.5
    # Recorded for visibility, but downgraded -- not fatal.
    codes = {d["code"]: d["severity"] for d in corner["diagnostics"]}
    assert codes["singular_matrix"] == "warning"
    assert codes.get("nonconvergence") == "warning"


def test_run_sim_stubbed_unrecovered_singular_matrix_still_errors(
    tmp_path, monkeypatch
):
    # A genuinely unrecovered singular matrix (ngspice's own abort trailer
    # present, no measurement values produced) must still classify as
    # `status: "error"` -- this fix narrows the false positive, it does not
    # remove the diagnostic.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1n"},
            "measurements": [
                {"name": "vout_meas", "spice": ".meas tran vout_meas find v(out) at=1n"}
            ],
        },
    )
    log = (
        "Warning: singular matrix:  check node b\n"
        "Warning: True gmin stepping failed\n"
        "Warning: source stepping failed\n"
        "Error: Transient op failed, timestep too small\n"
        "doAnalyses: TRAN:  Timestep too small; initial timepoint: cause unrecorded.\n"
        "tran simulation(s) aborted\n"
    )
    _stub_subprocess_run(monkeypatch, log_text=log)

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["status"] == "error"
    codes = {d["code"]: d["severity"] for d in corner["diagnostics"]}
    assert codes["singular_matrix"] == "error"


def test_run_sim_keep_artifacts_writes_log(tmp_path, monkeypatch):
    _write_body(tmp_path)
    artifacts_dir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request), artifacts_dir=str(artifacts_dir))

    log_path = report["corners"][0]["artifacts"]["log"]
    assert log_path is not None
    assert Path(log_path).read_text() == "clean run\n"
    assert Path(log_path).is_relative_to(artifacts_dir)


def test_run_sim_bundle_process_corner_writes_one_lib_per_section(
    tmp_path, monkeypatch
):
    """End-to-end (stubbed ngspice): a `corners.process` bundle entry flows
    from the request through `_expand_corners` and `_write_corner_deck`
    into the actual generated deck on disk, gf180mcu-style (one `.lib` card
    per device-family section, in declaration order)."""
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    artifacts_dir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {
                "process": [
                    "tt",
                    {
                        "name": "ss",
                        "sections": ["ss", "bjt_ss", "diode_ss"],
                    },
                ]
            },
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request), artifacts_dir=str(artifacts_dir))

    assert report["status"] == "pass"
    by_process = {c["process"]: c for c in report["corners"]}
    assert set(by_process) == {"tt", "ss"}

    models_lib = tmp_path / "corner.lib"

    tt_log = Path(by_process["tt"]["artifacts"]["log"])
    tt_deck = (tt_log.parent / "corner.cir").read_text().splitlines()
    assert [line for line in tt_deck if line.startswith(".lib")] == [
        f".lib {models_lib} tt"
    ]

    ss_log = Path(by_process["ss"]["artifacts"]["log"])
    ss_deck = (ss_log.parent / "corner.cir").read_text().splitlines()
    assert [line for line in ss_deck if line.startswith(".lib")] == [
        f".lib {models_lib} ss",
        f".lib {models_lib} bjt_ss",
        f".lib {models_lib} diode_ss",
    ]


def test_run_sim_without_keep_artifacts_cleans_up(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request))

    assert report["corners"][0]["artifacts"] == {
        "log": None,
        "raw": None,
        "waveform": None,
        "deck": None,
    }


def test_run_sim_keep_artifacts_populates_deck_matching_on_disk_file(
    tmp_path, monkeypatch
):
    """`artifacts.deck` references the exact ngspice deck `_write_corner_deck`
    already wrote to `<corner_dir>/corner.cir` -- absolute path, content
    matching the on-disk file, populated whenever `keep_artifacts` is true
    (no additional `options.waveforms` gate, unlike `raw`/`waveform`; see
    issue #356)."""
    _write_body(tmp_path)
    artifacts_dir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request), artifacts_dir=str(artifacts_dir))

    corner = report["corners"][0]
    deck_path = corner["artifacts"]["deck"]
    log_path = corner["artifacts"]["log"]
    assert deck_path is not None
    assert os.path.isabs(deck_path)
    assert Path(deck_path).is_relative_to(artifacts_dir)
    assert os.path.basename(deck_path) == "corner.cir"
    # `deck` and `log` are siblings in the same per-corner artifacts dir.
    assert deck_path == os.path.join(os.path.dirname(log_path), "corner.cir")
    # Content on disk is the actual deck `_write_corner_deck` wrote --
    # includes the analysis line and a reference to the netlist body.
    deck_text = Path(deck_path).read_text()
    assert "tran 1n 1u" in deck_text
    assert "body.spice" in deck_text


# --------------------------------------------------------------------------- #
# remote backend (#265) -- provisioning/transport are always faked; no test
# in this section ever touches a real AWS API, `ssh`/`scp` binary, or network
# socket (see remote_launcher/remote_transport's own test suites for their
# unit coverage of that plumbing in isolation).
# --------------------------------------------------------------------------- #


class _FakeRemoteLauncher:
    """Drop-in stand-in for ``remote_launcher.RemoteLauncher``: no AWS call,
    records constructor kwargs, tracks whether teardown (``__exit__``) ran."""

    #: Populated by the most recently constructed instance -- tests read this
    #: after ``run_sim`` returns (the launcher instance itself isn't
    #: reachable from the caller).
    last_instance: _FakeRemoteLauncher | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.terminated = False
        self.provision_error: Exception | None = None
        type(self).last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.terminated = True
        return False

    def provision(self):
        if self.provision_error is not None:
            raise self.provision_error
        return {
            "provider": "aws",
            "region": self.kwargs["region"],
            "instance_type": "c7i.xlarge",
            "instance_id": "i-fake123",
            "spot": True,
            "estimated_hourly_cost_usd": 0.1785,
            "ami_id": "ami-fake",
            "pdk_snapshot": "sky130A-2026.06.01",
            "spin_up_s": 1.2,
        }

    def get_public_ip(self):
        return "203.0.113.9"


def _base_remote_request(**overrides) -> dict:
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "models": {"pdk": "sky130A"},
        "backend": "remote",
        "remote": {
            "region": "us-east-1",
            "key_name": "fake-key",
            "ssh_key_path": "/fake/key.pem",
            "launcher_cidr": "203.0.113.4/32",
        },
    }
    request.update(overrides)
    return request


def _install_fake_remote_transport(monkeypatch, *, remote_report_factory=None):
    """Patch every ``sim.remote_transport``/``sim.RemoteLauncher`` call site
    ``_run_remote`` uses with an in-memory fake, and return the shared
    ``state`` dict the fakes record their calls into."""
    state: dict = {"push_job_calls": 0, "pull_calls": 0, "cleanup_calls": 0}

    monkeypatch.setattr(sim, "RemoteLauncher", _FakeRemoteLauncher)
    monkeypatch.setattr(sim.remote_transport, "wait_for_ssh", lambda *a, **k: 0.5)

    def fake_push_job(*, host, remote_job_dir, job, **kwargs):
        state["push_job_calls"] += 1
        state["host"] = host
        state["remote_job_dir"] = remote_job_dir
        state["job"] = job
        # Recover the two `_build_remote_job_description` inputs by their
        # `label` (see `sim._build_remote_job_description`) -- mirrors what
        # a real `push_job` call would have uploaded.
        for item in job.inputs:
            if item.label == "netlist":
                state["local_netlist_path"] = item.local_path
            elif item.label == "request":
                state["remote_request"] = json.loads(item.content)

    def fake_run_remote_job(*, host, remote_job_dir, job, timeout_s, **kwargs):
        state["run_remote_job_timeout_s"] = timeout_s
        if remote_report_factory is not None:
            return remote_report_factory(state)
        return {
            "schema_version": 1,
            "status": "pass",
            "corner_count": 1,
            "passed": 1,
            "failed": 0,
            "errored": 0,
            "environment": {"engine": "ngspice", "engine_version": "46"},
            "measurements": [],
            "corners": [],
        }

    def fake_pull_artifacts(**kwargs):
        state["pull_calls"] += 1
        state["pull_kwargs"] = kwargs

    def fake_cleanup_job(**kwargs):
        state["cleanup_calls"] += 1

    monkeypatch.setattr(sim.remote_transport, "push_job", fake_push_job)
    monkeypatch.setattr(sim.remote_transport, "run_remote_job", fake_run_remote_job)
    monkeypatch.setattr(sim.remote_transport, "pull_artifacts", fake_pull_artifacts)
    monkeypatch.setattr(sim.remote_transport, "cleanup_job", fake_cleanup_job)
    return state


def test_run_sim_remote_backend_requires_models_pdk(tmp_path):
    _write_body(tmp_path)
    request = _write_request(tmp_path, _base_remote_request(models={}))
    with pytest.raises(sim.SimError, match="requires request.models.pdk"):
        sim.run_sim(str(request))


def test_run_sim_remote_backend_requires_ssh_key_path(tmp_path):
    _write_body(tmp_path)
    bad = _base_remote_request()
    bad["remote"] = {**bad["remote"]}
    del bad["remote"]["ssh_key_path"]
    request = _write_request(tmp_path, bad)
    with pytest.raises(sim.SimError, match="requires request.remote.ssh_key_path"):
        sim.run_sim(str(request))


def test_run_sim_remote_backend_populates_environment_remote_block(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(tmp_path, _base_remote_request())
    _install_fake_remote_transport(monkeypatch)

    report = sim.run_sim(str(request))

    remote_env = report["environment"]["remote"]
    assert remote_env["provider"] == "aws"
    assert remote_env["region"] == "us-east-1"
    assert remote_env["instance_type"] == "c7i.xlarge"
    assert remote_env["instance_id"] == "i-fake123"
    assert remote_env["spot"] is True
    assert remote_env["ami_id"] == "ami-fake"
    assert remote_env["pdk_snapshot"] == "sky130A-2026.06.01"
    assert isinstance(remote_env["estimated_hourly_cost_usd"], float)
    assert isinstance(remote_env["spin_up_s"], float)
    # engine_version comes from the remote report, not the local process.
    assert report["environment"]["engine_version"] == "46"


def test_run_sim_remote_backend_pushes_local_parallel_request(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path, _base_remote_request(corners={"temperature_c": [10, 40]})
    )
    state = _install_fake_remote_transport(monkeypatch)

    sim.run_sim(str(request))

    pushed = state["remote_request"]
    assert pushed["backend"] == "local-parallel"
    assert pushed["netlist"] == remote_transport.REMOTE_NETLIST_FILENAME
    assert "remote" not in pushed
    assert pushed["corners"] == {"temperature_c": [10, 40]}
    assert state["local_netlist_path"].endswith("body.spice")
    assert state["push_job_calls"] == 1
    assert state["cleanup_calls"] == 1


def test_run_sim_remote_backend_provisioning_failure_raises_simerror_and_tears_down(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(tmp_path, _base_remote_request())
    _install_fake_remote_transport(monkeypatch)

    original_init = _FakeRemoteLauncher.__init__

    def failing_init(self, **kwargs):
        original_init(self, **kwargs)
        self.provision_error = rl.RemoteLaunchError("estimated cost exceeds ceiling")

    monkeypatch.setattr(_FakeRemoteLauncher, "__init__", failing_init)

    with pytest.raises(sim.SimError, match="remote backend failed"):
        sim.run_sim(str(request))

    assert _FakeRemoteLauncher.last_instance.terminated is True


def test_run_sim_remote_backend_keep_artifacts_pulls_and_rewrites_paths(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    artifacts_dir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path, _base_remote_request(options={"keep_artifacts": True})
    )

    def remote_report_factory(state):
        remote_root = remote_transport.artifacts_root(state["remote_job_dir"])
        return {
            "schema_version": 1,
            "status": "pass",
            "corner_count": 1,
            "passed": 1,
            "failed": 0,
            "errored": 0,
            "environment": {"engine": "ngspice", "engine_version": "46"},
            "measurements": [],
            "corners": [
                {
                    "corner_id": "default/novdd/27C",
                    "status": "pass",
                    "runtime_s": 0.1,
                    "measurements": [],
                    "diagnostics": [],
                    "artifacts": {
                        "log": f"{remote_root}/default_novdd_27C/ngspice.log",
                        "raw": None,
                        "waveform": None,
                    },
                }
            ],
        }

    state = _install_fake_remote_transport(
        monkeypatch, remote_report_factory=remote_report_factory
    )

    report = sim.run_sim(str(request), artifacts_dir=str(artifacts_dir))

    assert state["pull_calls"] == 1
    assert state["pull_kwargs"]["local_artifacts_dir"] == str(artifacts_dir)
    log_path = report["corners"][0]["artifacts"]["log"]
    assert log_path == str(artifacts_dir / "default_novdd_27C" / "ngspice.log")


def _corner_measurement_summary(report: dict) -> list[dict]:
    """Extract only the values a `remote` run is guaranteed to reproduce
    bit-for-bit against an equivalent `local` run (decision 5) -- corner id,
    status, and each measurement's value/status/margin. Excludes
    `runtime_s`/`artifacts` (paths/timing legitimately differ by backend)."""
    return [
        {
            "corner_id": c["corner_id"],
            "status": c["status"],
            "measurements": [
                {
                    "name": m["name"],
                    "value": m["value"],
                    "status": m["status"],
                    "margin": m["margin"],
                }
                for m in c["measurements"]
            ],
        }
        for c in report["corners"]
    ]


def test_run_sim_remote_backend_measurements_are_value_identical_to_local(
    tmp_path, monkeypatch
):
    # Acceptance criterion: "A `remote` run against the same netlist/models
    # as a `local` run produces value-identical `.meas` measurements". The
    # remote transport's `run_remote_job` is faked to actually invoke
    # `sim.run_sim(..., backend="local-parallel")` locally against the
    # pushed request+netlist -- exactly what a real remote host would do by
    # running `klt sim ... --backend local-parallel` -- so this exercises
    # the real `_run_local_parallel`/`_run_corner` code path twice (once as
    # `local`, once "as if remote") against the identical stubbed ngspice
    # subprocess, proving the two backends are the same code, not two
    # implementations that happen to agree.
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "corners": {"temperature_c": [10, 40]},
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
    }
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    local_request = _write_request(tmp_path, base, name="local.json")
    local_report = sim.run_sim(str(local_request), backend="local")

    remote_request = _write_request(
        tmp_path,
        _base_remote_request(**base, models={"pdk": "sky130A"}),
        name="remote.json",
    )

    remote_side_dir = tmp_path / "remote_side"
    remote_side_dir.mkdir()

    def remote_report_factory(state):
        netlist_dst = remote_side_dir / remote_transport.REMOTE_NETLIST_FILENAME
        netlist_dst.write_text(Path(state["local_netlist_path"]).read_text())
        request_dst = remote_side_dir / remote_transport.REMOTE_REQUEST_FILENAME
        request_dst.write_text(json.dumps(state["remote_request"]))
        return sim.run_sim(str(request_dst))

    _install_fake_remote_transport(
        monkeypatch, remote_report_factory=remote_report_factory
    )

    remote_report = sim.run_sim(str(remote_request))

    assert _corner_measurement_summary(local_report) == _corner_measurement_summary(
        remote_report
    )
    assert (
        local_report["environment"]["engine_version"]
        == remote_report["environment"]["engine_version"]
    )


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_stubbed_json_contract(tmp_path, monkeypatch, capsys):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    exit_code = main(["sim", str(request), "--format", "json"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data.keys()) == {
        "schema_version",
        "netlist",
        "status",
        "corner_count",
        "passed",
        "failed",
        "errored",
        "environment",
        "provenance",
        "measurements",
        "corners",
    }
    prov = data["provenance"]
    assert set(prov.keys()) == {
        "klt_version",
        "klayout_version",
        "pdk",
        "deck",
        "input",
    }
    assert isinstance(prov["klt_version"], str)
    # This request declares no process axis / model library, so no model
    # deck or PDK is resolved.
    assert prov["pdk"] is None
    assert prov["deck"] is None
    assert prov["input"] is None


def test_cli_default_format_is_text(tmp_path, monkeypatch, capsys):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    exit_code = main(["sim", str(request)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "netlist:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_exit_code_measurement_failed(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 0.1},
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    assert main(["sim", str(request), "--format", "json"]) == 3


def test_cli_exit_code_corner_errored(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    assert main(["sim", str(request), "--format", "json"]) == 4


def test_cli_unresolvable_request_error_envelope(tmp_path, capsys):
    exit_code = main(["sim", str(tmp_path / "nope.json"), "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "sim"
    assert "not found" in error["error"]["message"]


def test_cli_outdir_flag_overrides_default(tmp_path, monkeypatch):
    _write_body(tmp_path)
    outdir = tmp_path / "custom-out"
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    main(["sim", str(request), "--outdir", str(outdir), "--format", "json"])

    assert outdir.is_dir()
    assert any(outdir.rglob("ngspice.log"))


# --------------------------------------------------------------------------- #
# Integration: real ngspice (skipped when not installed)
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_integration_process_corner_selects_lib_section(tmp_path):
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {"process": ["tt", "ss"]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    by_process = {
        c["process"]: c["measurements"][0]["value"] for c in report["corners"]
    }
    # tt: corner_scale isn't referenced by this body -- both corners just
    # confirm .lib section selection didn't error; values are equal here.
    assert set(by_process) == {"tt", "ss"}


@_SKIP_NO_NGSPICE
def test_integration_monte_carlo_seed_reaches_ngspice_and_is_reproducible(tmp_path):
    """End-to-end confirmation of the seed contract against real ngspice
    (the test plan's "Manual verification" step, automated here since
    ngspice happens to be available): `.options seed=` (written from each
    sample's derived `rndseed`, see `_write_corner_deck`) actually reaches
    ngspice's `AGAUSS` random-function seed -- distinct samples draw
    distinct values, and re-running the identical request reproduces the
    exact same sequence of measured values, not just the exact same
    sequence of derived seed metadata (already covered by the stubbed
    tests above)."""
    body = tmp_path / "body.spice"
    body.write_text(
        "* AGAUSS(0,1,1) is seeded by klt sim's `.options seed=` card -- a\n"
        "* stand-in for a mismatch-aware PDK model's own behavioral variation.\n"
        ".param vdd=1.0\n"
        "Vdd vdd 0 DC {vdd}\n"
        ".param mc_rand = {AGAUSS(0,1,1)}\n"
        "R1 vdd out {1k*(1+0.01*mc_rand)}\n"
        "R2 out 0 1k\n"
        "C1 out 0 1n\n"
    )
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "monte_carlo": {"n": 4, "seed": 20260801, "vary": "mismatch"},
            "analysis": {"kind": "tran", "args": "1n 5u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=5u"}
            ],
        },
    )

    report_a = sim.run_sim(str(request), backend="local-parallel")
    report_b = sim.run_sim(str(request), backend="local-parallel")

    assert report_a["status"] == "pass"
    assert report_a["corner_count"] == 4

    def values_by_sample(report):
        return {
            c["monte_carlo"]["sample_index"]: c["measurements"][0]["value"]
            for c in report["corners"]
        }

    values_a = values_by_sample(report_a)
    values_b = values_by_sample(report_b)
    # Reproducible: identical request -> identical measured sequence.
    assert values_a == pytest.approx(values_b)
    # Not a no-op sampler: the seed actually varies AGAUSS's draw.
    assert len({round(v, 9) for v in values_a.values()}) > 1


@_SKIP_NO_NGSPICE
def test_integration_supply_and_temperature_sweep(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {
                "supply_v": {"vdd": [1.0, 2.0]},
                "temperature_c": [27, 125],
            },
            "analysis": {"kind": "tran", "args": "1n 5u"},
            "measurements": [
                {
                    "name": "vout_final",
                    "spice": ".meas tran vout_final FIND v(out) AT=5u",
                    "unit": "V",
                    "limits": {"min": 0.5, "max": 2.5},
                }
            ],
        },
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    assert report["corner_count"] == 4
    values = sorted(c["measurements"][0]["value"] for c in report["corners"])
    assert values == pytest.approx([1.0, 1.0, 2.0, 2.0])


@_SKIP_NO_NGSPICE
def test_integration_timeout_is_killed_and_classified(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"timeout_s": 60},
        },
    )
    # Force a real timeout deterministically -- ngspice's own process-startup
    # overhead (tens of milliseconds, per the stubbed tests' runtime_s
    # values) reliably exceeds an absurdly small budget, without needing to
    # construct an actual nonconvergent hang.
    request_data = json.loads(request.read_text())
    request_data["options"]["timeout_s"] = 0.001
    request.write_text(json.dumps(request_data))

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["status"] == "error"
    assert any(d["code"] == "timeout" for d in corner["diagnostics"])


@_SKIP_NO_NGSPICE
def test_integration_missing_measurement_value_is_error(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "never",
                    "spice": ".meas tran never FIND v(out) WHEN v(out)=99",
                }
            ],
        },
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["measurements"][0]["value"] is None
    assert corner["measurements"][0]["status"] == "error"


@_SKIP_NO_NGSPICE
def test_integration_waveform_artifact(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
            "options": {"keep_artifacts": True, "waveforms": True},
        },
    )

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    waveform_path = corner["artifacts"]["waveform"]
    assert waveform_path is not None
    waveform = json.loads(Path(waveform_path).read_text())
    assert waveform["variables"][0]["name"] == "time"
    assert len(waveform["points"]) > 0


@_SKIP_NO_NGSPICE
def test_integration_exit_codes(tmp_path):
    _write_body(tmp_path)
    pass_request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"min": 0.5},
                }
            ],
        },
        name="pass_request.json",
    )
    assert main(["sim", str(pass_request), "--format", "json"]) == 0

    fail_request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 0.1},
                }
            ],
        },
        name="fail_request.json",
    )
    assert main(["sim", str(fail_request), "--format", "json"]) == 3

    error_request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "never",
                    "spice": ".meas tran never FIND v(out) WHEN v(out)=99",
                }
            ],
        },
        name="error_request.json",
    )
    assert main(["sim", str(error_request), "--format", "json"]) == 4


# --------------------------------------------------------------------------- #
# Worked example (examples/sim/), regenerated by examples/sim/generate.py
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
@pytest.mark.skipif(
    not EXAMPLES_DIR.exists(), reason="examples/sim/ fixtures not generated"
)
def test_examples_sim_worked_example_passes():
    request_path = EXAMPLES_DIR / "request.json"
    if not request_path.exists():
        pytest.skip(
            "examples/sim/request.json not generated -- run examples/sim/generate.py"
        )

    report = sim.run_sim(str(request_path))

    assert report["schema_version"] == 1
    assert report["status"] == "pass"
    assert report["corner_count"] == 8
    assert report["measurements"][0]["name"] == "vout"


def test_pdk_module_is_the_only_resolution_path(monkeypatch, tmp_path):
    """Guard against a future regression re-introducing a hand-rolled PDK
    path lookup: `_resolve_models_lib`'s pdk-variant branch must go through
    `klayout_tools.pdk.find_pdk` (issue #45), not a private reimplementation."""
    calls = []
    original = pdk.find_pdk

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(sim, "find_pdk", spy)

    install_root = tmp_path / "install"
    variant_dir = install_root / "sky130A" / "libs.tech"
    (variant_dir / "ngspice").mkdir(parents=True)
    (variant_dir / "ngspice" / "sky130.lib.spice").write_text(".lib tt\n.endl tt\n")

    sim._resolve_models_lib(
        {
            "pdk": "sky130A",
            "pdk_root": str(install_root),
            "lib": "libs.tech/ngspice/sky130.lib.spice",
        },
        request_dir=str(tmp_path),
    )

    assert len(calls) == 1
