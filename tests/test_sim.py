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
    mc_env = report["environment"]["monte_carlo"]
    assert mc_env["n"] == 3
    assert mc_env["seed"] == 1
    assert mc_env["vary"] == "both"
    # `vary: "both"` includes mismatch -> the per-family mismatch-activity
    # report (#355) is present; no `models.pdk` was declared, so activity is
    # unverified (`active: None`) for every family the netlist body (R1/C1)
    # instantiates.
    assert {f["family"] for f in mc_env["family_mismatch"]} == {
        "resistor",
        "capacitor",
    }
    assert all(f["active"] is None for f in mc_env["family_mismatch"])
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


# --------------------------------------------------------------------------- #
# Per-family mismatch-activity report (#355) -- a device family whose deck
# mismatch ships structurally disabled (e.g. gf180mcu's poly resistor) must
# never be indistinguishable from a family that actually got sampled just
# because the run's global mismatch switch/section was engaged.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "line,expected",
    [
        ("R1 vdd out 1k", "resistor"),
        ("C1 out 0 1n", "capacitor"),
        ("D1 anode cathode diode_model", "diode"),
        ("Q1 c b e npn_model", "bipolar"),
        ("M1 d g s b nmos_model L=1u W=1u", "mosfet"),
        ("XM1 d g s b nfet_03v3 L=1u W=1u", "mosfet"),
        ("XR1 a b res_xpoly_1p1000pl W=1", "resistor"),
        ("XQ1 c b e bjt_pnp_model", "bipolar"),
        ("XC1 a b moscap_model", "capacitor"),
        ("XU1 a b some_unrecognised_block", "other"),
        ("V1 vdd 0 DC 1.0", None),
        ("I1 a b DC 1m", None),
        ("L1 a b 1u", None),
    ],
)
def test_classify_device_family(line, expected):
    element_type = line[0].upper()
    assert sim._classify_device_family(element_type, line) == expected


def test_detect_device_families_skips_comments_continuations_and_dot_cards():
    text = (
        "* a full-line comment\n"
        ".param vdd=1.0\n"
        "Vdd vdd 0 DC {vdd}\n"
        "R1 vdd out 1k\n"
        "+ ; a continuation line, not a device\n"
        "C1 out 0 1n\n"
        "\n"
        "R2 out 0 1k\n"
    )
    # R2 repeats an already-seen family (resistor) -- first-seen order, no
    # duplicates.
    assert sim._detect_device_families(text) == ["resistor", "capacitor"]


def test_mismatch_family_report_gf180mcu_flags_resistor_inactive(tmp_path):
    """The concrete case this issue is about: gf180mcu's poly-resistor
    mismatch hook is structurally disabled, and must be reported as such
    even though MOS/BJT mismatch is active under the same global switch."""
    netlist = tmp_path / "body.spice"
    netlist.write_text(
        "XR1 a b res_xpoly_1p1000pl W=1\n"
        "XM1 d g s b nfet_03v3 L=1u W=1u\n"
        "XQ1 c b e bjt_npn_model\n"
    )

    report = sim._mismatch_family_report(str(netlist), "gf180mcuC")

    by_family = {entry["family"]: entry for entry in report}
    resistor_note = by_family["resistor"]["note"]
    assert by_family["resistor"]["active"] is False
    assert "hardcoded" in resistor_note or "disabled" in resistor_note
    assert by_family["mosfet"]["active"] is True
    assert by_family["bipolar"]["active"] is True


def test_mismatch_family_report_sky130(tmp_path):
    netlist = tmp_path / "body.spice"
    netlist.write_text(
        "XM1 d g s b sky130_fd_pr__nfet_01v8 L=1u W=1u\n"
        "XQ1 c b e sky130_fd_pr__npn_model\n"
    )

    report = sim._mismatch_family_report(str(netlist), "sky130A")

    by_family = {entry["family"]: entry for entry in report}
    assert by_family["mosfet"]["active"] is True
    assert by_family["bipolar"]["active"] is True


def test_mismatch_family_report_unrecognised_pdk_family_is_unverified(tmp_path):
    """A family with no curated table entry -- including an unrecognised
    PDK -- is reported `active: None` ("not independently verified"),
    never guessed `True`."""
    netlist = tmp_path / "body.spice"
    netlist.write_text("R1 vdd out 1k\nC1 out 0 1n\n")

    report = sim._mismatch_family_report(str(netlist), "not_a_real_pdk")

    for entry in report:
        assert entry["active"] is None
        assert "not independently verified" in entry["note"]


def test_mismatch_family_report_missing_pdk_variant_is_unverified(tmp_path):
    netlist = tmp_path / "body.spice"
    netlist.write_text("R1 vdd out 1k\n")

    report = sim._mismatch_family_report(str(netlist), None)

    assert report == [
        {
            "family": "resistor",
            "active": None,
            "note": (
                "PDK family could not be determined for this request (set "
                "models.pdk) -- mismatch activity for the 'resistor' "
                "family could not be verified; treat any sampled spread "
                "for it as unconfirmed."
            ),
        }
    ]


def test_run_sim_monte_carlo_family_mismatch_present_only_when_vary_includes_mismatch(
    tmp_path, monkeypatch
):
    """`vary: "process"` never touches mismatch sampling -- no
    `family_mismatch` report is emitted (there is nothing to report on)."""
    request = _mc_request(tmp_path, {"n": 2, "seed": 1, "vary": "process"})
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert "family_mismatch" not in report["environment"]["monte_carlo"]


def test_run_sim_monte_carlo_family_mismatch_reports_gf180mcu_resistor_disabled(
    tmp_path, monkeypatch
):
    """End-to-end (`run_sim`): a gf180mcu request declaring `models.pdk`
    and mismatch sampling gets a `family_mismatch` report that flags the
    resistor family inactive, never lumping it in with the active MOS/BJT
    families."""
    body = tmp_path / "body.spice"
    body.write_text(
        ".param vdd=1.0\n"
        "Vdd vdd 0 DC {vdd}\n"
        "XR1 vdd mid res_xpoly_1p1000pl W=1\n"
        "XM1 mid g out b nfet_03v3 L=1u W=1u\n"
        "C1 out 0 1n\n"
    )
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"pdk": "gf180mcuC"},
            "monte_carlo": {"n": 2, "seed": 1, "vary": "mismatch"},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    family_mismatch = report["environment"]["monte_carlo"]["family_mismatch"]
    by_family = {entry["family"]: entry["active"] for entry in family_mismatch}
    assert by_family["resistor"] is False
    assert by_family["mosfet"] is True
    # capacitor has no curated gf180mcu table entry -- unverified, not
    # assumed active or inactive.
    assert by_family["capacitor"] is None


# --------------------------------------------------------------------------- #
# Monte Carlo statistics rollup (#349)
# --------------------------------------------------------------------------- #

#: A deliberately hand-checkable sample set: mean 3.0, sample (n-1) sigma
#: sqrt(2.5), min 1.0, max 5.0, p5 1.2, p50 3.0, p95 4.8.
_KNOWN_SAMPLES = [3.0, 1.0, 5.0, 2.0, 4.0]
_KNOWN_SIGMA = 2.5**0.5


@pytest.mark.parametrize(
    ("percentile", "expected"),
    [
        (0, 1.0),
        (5, 1.2),
        (25, 2.0),
        (50, 3.0),
        (95, 4.8),
        (100, 5.0),
    ],
)
def test_percentile_linear_interpolation(percentile, expected):
    assert sim._percentile(sorted(_KNOWN_SAMPLES), percentile) == pytest.approx(
        expected
    )


def test_percentile_single_sample_is_that_sample():
    assert sim._percentile([1.25], 5) == 1.25
    assert sim._percentile([1.25], 95) == 1.25


@pytest.mark.parametrize(
    ("percentile", "key"), [(5, "p5"), (50, "p50"), (2.5, "p2.5"), (97.5, "p97.5")]
)
def test_quantile_key_formatting(percentile, key):
    assert sim._quantile_key(percentile) == key


def test_sample_statistics_known_sample_set():
    stats = sim._sample_statistics(
        _KNOWN_SAMPLES,
        errored=0,
        quantiles=sim.DEFAULT_MC_QUANTILES,
        k_sigma=None,
        limits=None,
    )

    assert stats["n"] == 5
    assert stats["errored"] == 0
    assert stats["mean"] == pytest.approx(3.0)
    assert stats["stddev"] == pytest.approx(_KNOWN_SIGMA)
    assert stats["min"] == 1.0
    assert stats["max"] == 5.0
    assert stats["quantiles"]["p5"] == pytest.approx(1.2)
    assert stats["quantiles"]["p50"] == pytest.approx(3.0)
    assert stats["quantiles"]["p95"] == pytest.approx(4.8)
    assert stats["sigma_window"] is None  # no k_sigma declared


def test_sample_statistics_honours_configured_quantiles():
    stats = sim._sample_statistics(
        _KNOWN_SAMPLES,
        errored=0,
        quantiles=(2.5, 97.5),
        k_sigma=None,
        limits=None,
    )

    assert list(stats["quantiles"]) == ["p2.5", "p97.5"]
    assert stats["quantiles"]["p2.5"] == pytest.approx(1.1)


def test_sample_statistics_single_sample_has_no_stddev():
    stats = sim._sample_statistics(
        [1.2],
        errored=0,
        quantiles=sim.DEFAULT_MC_QUANTILES,
        k_sigma=3.0,
        limits={"min": 1.0, "max": 1.4},
    )

    assert stats["n"] == 1
    assert stats["mean"] == 1.2
    # Never faked as 0.0 -- a fabricated zero sigma would silently pass any
    # window check.
    assert stats["stddev"] is None
    assert stats["sigma_window"] is None


def test_sample_statistics_no_usable_values_is_all_null():
    stats = sim._sample_statistics(
        [],
        errored=3,
        quantiles=sim.DEFAULT_MC_QUANTILES,
        k_sigma=3.0,
        limits={"max": 1.0},
    )

    assert stats["n"] == 0
    assert stats["errored"] == 3
    assert stats["mean"] is None
    assert stats["stddev"] is None
    assert stats["min"] is None and stats["max"] is None
    assert stats["quantiles"] == {"p5": None, "p50": None, "p95": None}
    assert stats["sigma_window"] is None


def test_evaluate_sigma_window_pass_exactly_at_the_limits():
    # mean 1.0, sigma 0.25, k 2 -> window is exactly [0.5, 1.5] (all values
    # binary-exact), and a value *equal* to a bound passes, as for a single
    # deterministic value.
    window = sim._evaluate_sigma_window(1.0, 0.25, 2.0, {"min": 0.5, "max": 1.5})

    assert window == {
        "k": 2.0,
        "low": 0.5,
        "high": 1.5,
        "status": "pass",
        "margin": 0.0,
    }


def test_evaluate_sigma_window_fails_just_outside_the_lower_bound():
    window = sim._evaluate_sigma_window(1.0, 0.25, 2.0, {"min": 0.5625, "max": 1.5})

    assert window["status"] == "fail"
    assert window["margin"] == pytest.approx(-0.0625)


def test_evaluate_sigma_window_fails_just_outside_the_upper_bound():
    window = sim._evaluate_sigma_window(1.0, 0.25, 2.0, {"min": 0.5, "max": 1.4375})

    assert window["status"] == "fail"
    assert window["margin"] == pytest.approx(-0.0625)


def test_evaluate_sigma_window_without_limits_never_fails():
    window = sim._evaluate_sigma_window(1.0, 0.25, 3.0, None)

    assert window["status"] == "pass"
    assert window["margin"] is None


def test_evaluate_sigma_window_k_zero_is_the_mean_itself():
    window = sim._evaluate_sigma_window(1.0, 0.25, 0.0, {"min": 0.9, "max": 1.1})

    assert (window["low"], window["high"]) == (1.0, 1.0)
    assert window["status"] == "pass"


def test_rollup_measurements_without_monte_carlo_config_is_unchanged():
    """A plain corner matrix keeps today's exact per-measurement shape."""
    corners = [
        {
            "corner_id": "a",
            "monte_carlo": None,
            "measurements": [
                {"name": "vref", "value": 1.20, "margin": 0.01, "status": "pass"}
            ],
        }
    ]

    (entry,) = sim._rollup_measurements(
        [{"name": "vref", "unit": "V", "limits": {"max": 1.21}}], corners
    )

    assert set(entry) == {"name", "unit", "limits", "status", "worst_case"}


def test_rollup_measurements_monte_carlo_config_but_no_sampled_corners():
    """`monte_carlo` config with only unsampled corners adds no statistics."""
    corners = [
        {
            "corner_id": "a",
            "monte_carlo": None,
            "measurements": [
                {"name": "vref", "value": 1.20, "margin": 0.01, "status": "pass"}
            ],
        }
    ]

    (entry,) = sim._rollup_measurements(
        [{"name": "vref", "unit": "V", "limits": {"max": 1.21}}],
        corners,
        {"quantiles": sim.DEFAULT_MC_QUANTILES, "k_sigma": 3.0},
    )

    assert "monte_carlo" not in entry


def _sampled_corner(base: str, sample_index: int, value: float | None) -> dict:
    return {
        "corner_id": f"{base}/mc{sample_index}",
        "monte_carlo": {"sample_index": sample_index, "seed": 1},
        "measurements": [
            {
                "name": "vref",
                "value": value,
                "margin": None,
                "status": "error" if value is None else "pass",
            }
        ],
    }


def test_rollup_measurements_counts_unextractable_samples_separately():
    corners = [
        _sampled_corner("tt/1.800V/27C", 0, 1.0),
        _sampled_corner("tt/1.800V/27C", 1, None),
        _sampled_corner("tt/1.800V/27C", 2, 3.0),
    ]

    (entry,) = sim._rollup_measurements(
        [{"name": "vref"}],
        corners,
        {"quantiles": sim.DEFAULT_MC_QUANTILES, "k_sigma": None},
    )

    assert entry["monte_carlo"]["n"] == 2  # only the samples that produced a number
    assert entry["monte_carlo"]["errored"] == 1
    assert entry["monte_carlo"]["mean"] == pytest.approx(2.0)


def test_rollup_measurements_by_corner_splits_per_originating_corner():
    corners = [
        _sampled_corner("tt/1.800V/27C", 0, 1.0),
        _sampled_corner("tt/1.800V/27C", 1, 3.0),
        _sampled_corner("ss/1.800V/27C", 0, 10.0),
        _sampled_corner("ss/1.800V/27C", 1, 20.0),
    ]

    (entry,) = sim._rollup_measurements(
        [{"name": "vref"}],
        corners,
        {"quantiles": sim.DEFAULT_MC_QUANTILES, "k_sigma": None},
    )

    mc = entry["monte_carlo"]
    assert mc["n"] == 4  # pooled across every corner
    by_corner = {c["corner_id"]: c for c in mc["by_corner"]}
    assert list(by_corner) == ["tt/1.800V/27C", "ss/1.800V/27C"]  # corner order
    assert by_corner["tt/1.800V/27C"]["mean"] == pytest.approx(2.0)
    assert by_corner["ss/1.800V/27C"]["mean"] == pytest.approx(15.0)


def _stub_subprocess_values(monkeypatch, name: str, values: list[float | None]) -> None:
    """Stub ngspice so successive corner runs report ``name = values[i]``.

    The `local` backend runs the expanded corner list sequentially in the
    deterministic sample order (`_expand_monte_carlo`), so call index maps
    1:1 onto sample index. A ``None`` entry stands in for an unextractable
    measurement (ngspice's own ``failed!`` line).
    """
    state = {"index": 0}

    def fake_run(cmd, capture_output, text, timeout):
        log_path = cmd[cmd.index("-o") + 1]
        value = values[state["index"]]
        state["index"] += 1
        with open(log_path, "w", encoding="utf-8") as handle:
            if value is None:
                handle.write(f".meas tran {name} find v(out) at=1u failed!\n")
            else:
                handle.write(f"{name} = {value!r}\n")
        return _FakeCompleted("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


_VOUT_MEAS = {
    "name": "vout",
    "spice": ".meas tran vout FIND v(out) AT=1u",
    "unit": "V",
}


def test_run_sim_monte_carlo_statistics_match_hand_computed_values(
    tmp_path, monkeypatch
):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch"},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    (entry,) = report["measurements"]
    mc = entry["monte_carlo"]
    assert mc["n"] == 5
    assert mc["errored"] == 0
    assert mc["mean"] == pytest.approx(3.0)
    assert mc["stddev"] == pytest.approx(_KNOWN_SIGMA)
    assert mc["min"] == 1.0
    assert mc["max"] == 5.0
    assert mc["quantiles"]["p5"] == pytest.approx(1.2)
    assert mc["quantiles"]["p50"] == pytest.approx(3.0)
    assert mc["quantiles"]["p95"] == pytest.approx(4.8)
    assert mc["sigma_window"] is None
    assert [c["corner_id"] for c in mc["by_corner"]] == ["default/novdd/27C"]


def test_run_sim_without_monte_carlo_has_no_statistics_block(tmp_path, monkeypatch):
    """A plain corner matrix response is byte-identical in shape to today's."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_VOUT_MEAS],
        },
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0])

    report = sim.run_sim(str(request))

    (entry,) = report["measurements"]
    assert set(entry) == {"name", "unit", "limits", "status", "worst_case"}


def test_run_sim_monte_carlo_sigma_window_pass_keeps_run_passing(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 1.0},
        measurements=[{**_VOUT_MEAS, "limits": {"min": 1.0, "max": 5.0}}],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    window = report["measurements"][0]["monte_carlo"]["sigma_window"]
    assert window["k"] == 1.0
    assert window["low"] == pytest.approx(3.0 - _KNOWN_SIGMA)
    assert window["high"] == pytest.approx(3.0 + _KNOWN_SIGMA)
    assert window["status"] == "pass"
    assert report["measurements"][0]["status"] == "pass"
    assert report["status"] == "pass"


def test_run_sim_monte_carlo_sigma_window_fail_fails_the_run(tmp_path, monkeypatch):
    """Every individual sample passes its limits, but mean +/- 3*sigma does
    not fit inside the window -- a real design failure, so the run fails."""
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        measurements=[{**_VOUT_MEAS, "limits": {"min": 0.5, "max": 5.5}}],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    # Every corner still passes -- nothing about per-sample evaluation moved.
    assert all(c["status"] == "pass" for c in report["corners"])
    assert report["failed"] == 0
    window = report["measurements"][0]["monte_carlo"]["sigma_window"]
    assert window["status"] == "fail"
    assert window["margin"] < 0
    assert report["measurements"][0]["status"] == "fail"
    assert report["status"] == "fail"


def test_run_sim_monte_carlo_sigma_window_needs_limits_to_fail(tmp_path, monkeypatch):
    """No `limits` -> reported but never fails, exactly as for a single value."""
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    window = report["measurements"][0]["monte_carlo"]["sigma_window"]
    assert window["status"] == "pass"
    assert window["margin"] is None
    assert report["status"] == "pass"


def test_run_sim_monte_carlo_per_measurement_k_sigma_overrides_the_default(
    tmp_path, monkeypatch
):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        measurements=[{**_VOUT_MEAS, "k_sigma": 1.0, "limits": {"min": 1.0}}],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    window = report["measurements"][0]["monte_carlo"]["sigma_window"]
    assert window["k"] == 1.0  # per-measurement override, not the run-wide 3.0
    assert window["status"] == "pass"  # would fail at k=3


def test_run_sim_monte_carlo_errored_sample_excluded_from_statistics(
    tmp_path, monkeypatch
):
    request = _mc_request(
        tmp_path,
        {"n": 3, "seed": 1, "vary": "mismatch"},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0, None, 3.0])

    report = sim.run_sim(str(request))

    mc = report["measurements"][0]["monte_carlo"]
    assert mc["n"] == 2
    assert mc["errored"] == 1
    assert mc["mean"] == pytest.approx(2.0)
    assert report["status"] == "error"  # the unextractable sample still errors


def test_run_sim_monte_carlo_statistics_pool_and_split_across_corners(
    tmp_path, monkeypatch
):
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch"},
        two_corners=True,
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0, 3.0, 10.0, 20.0])

    report = sim.run_sim(str(request))

    mc = report["measurements"][0]["monte_carlo"]
    assert mc["n"] == 4
    assert mc["mean"] == pytest.approx(8.5)
    by_corner = {c["corner_id"]: c["mean"] for c in mc["by_corner"]}
    assert by_corner == {
        "tt/novdd/27C": pytest.approx(2.0),
        "ss/novdd/27C": pytest.approx(15.0),
    }


def test_run_sim_monte_carlo_configured_quantiles_are_echoed(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "quantiles": [1, 50, 99], "k_sigma": 3},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    echo = dict(report["environment"]["monte_carlo"])
    # `family_mismatch` (#355) is an independent additive field a
    # mismatch-varying run always carries -- not part of the statistics
    # echo this test pins.
    echo.pop("family_mismatch", None)
    assert echo == {
        "n": 5,
        "seed": 1,
        "vary": "mismatch",
        "quantiles": [1.0, 50.0, 99.0],
        "k_sigma": 3.0,
    }
    assert list(report["measurements"][0]["monte_carlo"]["quantiles"]) == [
        "p1",
        "p50",
        "p99",
    ]


def test_run_sim_monte_carlo_sampling_only_request_echo_is_unchanged(
    tmp_path, monkeypatch
):
    request = _mc_request(
        tmp_path, {"n": 2, "seed": 1, "vary": "mismatch"}, measurements=[_VOUT_MEAS]
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0, 2.0])

    report = sim.run_sim(str(request))

    echo = dict(report["environment"]["monte_carlo"])
    # As above: `family_mismatch` (#355) is orthogonal to the statistics
    # echo -- a request declaring neither `quantiles` nor `k_sigma` must add
    # neither key.
    echo.pop("family_mismatch", None)
    assert echo == {
        "n": 2,
        "seed": 1,
        "vary": "mismatch",
    }


def test_run_sim_monte_carlo_statistics_and_family_mismatch_coexist(
    tmp_path, monkeypatch
):
    """The statistics echo (#349) and the per-family mismatch-activity
    report (#355) are independent additive fields on the *same*
    `environment.monte_carlo` block -- a mismatch-varying request that also
    declares `k_sigma` must carry both, and still get its per-measurement
    statistics rollup."""
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    echo = report["environment"]["monte_carlo"]
    assert echo["k_sigma"] == 3.0
    assert [entry["family"] for entry in echo["family_mismatch"]] == [
        "resistor",
        "capacitor",
    ]
    mc = report["measurements"][0]["monte_carlo"]
    assert mc["n"] == 5
    assert mc["stddev"] == pytest.approx(_KNOWN_SIGMA)
    assert mc["sigma_window"]["k"] == 3.0


@pytest.mark.parametrize(
    "quantiles",
    [[], "p50", [101], [-1], ["p50"], [True]],
)
def test_run_sim_monte_carlo_bad_quantiles_raises(tmp_path, quantiles):
    request = _mc_request(
        tmp_path, {"n": 2, "seed": 1, "vary": "mismatch", "quantiles": quantiles}
    )

    with pytest.raises(sim.SimError, match="monte_carlo.quantiles"):
        sim.run_sim(str(request))


@pytest.mark.parametrize("k_sigma", [-1, "3", True])
def test_run_sim_monte_carlo_bad_k_sigma_raises(tmp_path, k_sigma):
    request = _mc_request(
        tmp_path, {"n": 2, "seed": 1, "vary": "mismatch", "k_sigma": k_sigma}
    )

    with pytest.raises(sim.SimError, match="monte_carlo.k_sigma"):
        sim.run_sim(str(request))


@pytest.mark.parametrize("k_sigma", [-1, "3", True])
def test_run_sim_measurement_bad_k_sigma_raises(tmp_path, k_sigma):
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch"},
        measurements=[{**_VOUT_MEAS, "k_sigma": k_sigma}],
    )

    with pytest.raises(sim.SimError, match="k_sigma"):
        sim.run_sim(str(request))


def test_run_sim_monte_carlo_quantiles_deduped_preserving_order(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch", "quantiles": [50, 5, 50]},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0, 3.0])

    report = sim.run_sim(str(request))

    assert list(report["measurements"][0]["monte_carlo"]["quantiles"]) == ["p50", "p5"]


def test_cli_sim_sigma_window_failure_exits_3(tmp_path, monkeypatch, capsys):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        measurements=[{**_VOUT_MEAS, "limits": {"min": 0.5, "max": 5.5}}],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    assert main(["sim", str(request), "--format", "json"]) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["measurements"][0]["monte_carlo"]["sigma_window"]["status"] == "fail"


def test_cli_sim_text_output_reports_monte_carlo_statistics(
    tmp_path, monkeypatch, capsys
):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 1.0},
        measurements=[{**_VOUT_MEAS, "limits": {"min": 1.0, "max": 5.0}}],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    assert main(["sim", str(request)]) == 0

    out = capsys.readouterr().out
    assert "mc: n=5 mean=3.0" in out
    assert "mean+/-1sigma" in out


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


# --------------------------------------------------------------------------- #
# Fleet shard/merge engine (Epic #375 Phase 1A, #376) -- pure logic, no AWS
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("hosts", range(1, 8))
def test_shard_corner_points_round_trips_for_every_host_count(hosts):
    points = [sim.CornerPoint(None, {}, float(i)) for i in range(7)]

    shards = sim._shard_corner_points(points, hosts)

    assert len(shards) == hosts
    # Concatenating every shard, in shard order, reproduces the original
    # list exactly -- the round-trip every merge in this module depends on.
    assert [p for shard in shards for p in shard] == points
    sizes = [len(shard) for shard in shards]
    assert max(sizes) - min(sizes) <= 1


def test_shard_corner_points_hosts_exceeding_units_leaves_trailing_shards_empty():
    points = [sim.CornerPoint(None, {}, 1.0), sim.CornerPoint(None, {}, 2.0)]

    shards = sim._shard_corner_points(points, 5)

    assert len(shards) == 5
    assert [p for shard in shards for p in shard] == points
    assert sum(1 for shard in shards if shard) == 2


@pytest.mark.parametrize("bad_hosts", [0, -1])
def test_shard_corner_points_rejects_non_positive_hosts(bad_hosts):
    with pytest.raises(sim.SimError, match="hosts"):
        sim._shard_corner_points([], bad_hosts)


def test_lost_shard_corner_shape_matches_run_corner_error_shape():
    point = sim.CornerPoint("tt", {"vdd": 1.8}, 25.0)

    corner = sim._lost_shard_corner(
        point,
        [
            {"name": "vout", "unit": "V"},
            {"name": "iq"},
        ],
        "shard lost: boom",
    )

    assert corner["corner_id"] == point.corner_id
    assert corner["process"] == "tt"
    assert corner["supply_v"] == {"vdd": 1.8}
    assert corner["temperature_c"] == 25.0
    assert corner["status"] == "error"
    assert corner["monte_carlo"] is None
    assert corner["diagnostics"] == [
        {"severity": "error", "code": "lost_shard", "message": "shard lost: boom"}
    ]
    assert corner["measurements"] == [
        {"name": "vout", "value": None, "unit": "V", "status": "error", "margin": None},
        {"name": "iq", "value": None, "unit": None, "status": "error", "margin": None},
    ]
    assert corner["artifacts"] == {
        "log": None,
        "raw": None,
        "waveform": None,
        "deck": None,
    }


def test_lost_shard_corner_carries_monte_carlo_block_for_a_sampled_point():
    point = sim.CornerPoint(
        "tt",
        {},
        25.0,
        sample_index=3,
        mc_seed={"rndseed": 111, "process_seed": 222, "mismatch_seed": 333},
    )

    corner = sim._lost_shard_corner(point, [], "shard lost: boom")

    assert corner["corner_id"].endswith("/mc3")
    assert corner["monte_carlo"] == {
        "sample_index": 3,
        "seed": 111,
        "process_seed": 222,
        "mismatch_seed": 333,
    }


def test_run_sharded_merges_in_global_order_regardless_of_completion_order():
    # Sleep inversely with each shard's own marker so shards complete in
    # *reverse* submission order under the pool -- actually exercising
    # index-based reassembly rather than accidentally passing because
    # completion order happened to match shard order.
    points = [sim.CornerPoint(None, {}, float(i)) for i in range(6)]

    def shard_runner(shard_points):
        time.sleep(0.01 * (10 - shard_points[0].temperature_c))
        corners = [
            {"corner_id": p.corner_id, "marker": p.temperature_c} for p in shard_points
        ]
        return corners, None, None

    corners, engine_version, remote_environment = sim._run_sharded(
        shard_runner=shard_runner,
        corner_points=points,
        hosts=3,
        measurements_spec=[],
    )

    assert [c["marker"] for c in corners] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert engine_version is None
    assert remote_environment is None


def test_run_sharded_lost_shard_produces_per_unit_errors_and_correct_counts():
    points = [sim.CornerPoint(None, {}, float(i)) for i in range(6)]

    def shard_runner(shard_points):
        if shard_points[0].temperature_c == 2.0:
            raise RuntimeError("boom")
        corners = [
            {
                "corner_id": p.corner_id,
                "status": "pass",
                "measurements": [],
                "diagnostics": [],
                "artifacts": {},
                "monte_carlo": None,
            }
            for p in shard_points
        ]
        return corners, "46", None

    corners, engine_version, remote_environment = sim._run_sharded(
        shard_runner=shard_runner,
        corner_points=points,
        hosts=3,
        measurements_spec=[{"name": "vout", "unit": "V"}],
    )

    assert len(corners) == 6
    assert [c["status"] for c in corners] == [
        "pass",
        "pass",
        "error",
        "error",
        "pass",
        "pass",
    ]
    errored = [c for c in corners if c["status"] == "error"]
    assert len(errored) == 2
    for corner in errored:
        assert corner["diagnostics"] == [
            {
                "severity": "error",
                "code": "lost_shard",
                "message": "shard lost: boom",
            }
        ]
        assert corner["measurements"] == [
            {
                "name": "vout",
                "value": None,
                "unit": "V",
                "status": "error",
                "margin": None,
            }
        ]
    # `engine_version` -- last non-None wins, in shard (global unit) order --
    # is unaffected by the lost shard in the middle.
    assert engine_version == "46"
    assert remote_environment is None


def test_run_sharded_builds_fleet_array_when_any_shard_has_remote_environment():
    points = [sim.CornerPoint(None, {}, float(i)) for i in range(4)]

    def shard_runner(shard_points):
        remote_env = {"instance_id": f"i-{int(shard_points[0].temperature_c)}"}
        corners = [{"corner_id": p.corner_id} for p in shard_points]
        return corners, None, remote_env

    _, _, remote_environment = sim._run_sharded(
        shard_runner=shard_runner, corner_points=points, hosts=2, measurements_spec=[]
    )

    assert remote_environment == {
        "fleet": [{"instance_id": "i-0"}, {"instance_id": "i-2"}]
    }


def test_run_sharded_fleet_array_has_null_entry_for_lost_shard():
    points = [sim.CornerPoint(None, {}, float(i)) for i in range(4)]

    def shard_runner(shard_points):
        if shard_points[0].temperature_c == 2.0:
            raise RuntimeError("provisioning failed")
        corners = [{"corner_id": p.corner_id} for p in shard_points]
        return corners, None, {"instance_id": "i-0"}

    _, _, remote_environment = sim._run_sharded(
        shard_runner=shard_runner, corner_points=points, hosts=2, measurements_spec=[]
    )

    assert remote_environment == {"fleet": [{"instance_id": "i-0"}, None]}


def test_run_sharded_never_calls_shard_runner_for_an_empty_shard():
    points = [sim.CornerPoint(None, {}, 1.0)]
    calls = []

    def shard_runner(shard_points):
        calls.append(list(shard_points))
        return [{"corner_id": p.corner_id} for p in shard_points], None, None

    corners, _, _ = sim._run_sharded(
        shard_runner=shard_runner, corner_points=points, hosts=3, measurements_spec=[]
    )

    assert len(calls) == 1  # only the one non-empty shard was ever run
    assert len(corners) == 1


def test_run_sim_hosts_one_is_byte_identical_to_hosts_absent(tmp_path, monkeypatch):
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "corners": {"temperature_c": [10, 20, 30]},
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
    request = _write_request(tmp_path, base)

    default_report = sim.run_sim(str(request))
    explicit_hosts_report = sim.run_sim(str(request), hosts=1)

    assert _stripped_report(default_report) == _stripped_report(explicit_hosts_report)


def test_run_sim_local_parallel_hosts_shards_match_unsharded_report(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "backend": "local-parallel",
        "corners": {"temperature_c": [10, 20, 30, 40, 50, 60, 70]},
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
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )
    unsharded_req = _write_request(tmp_path, base, name="unsharded.json")
    sharded_req = _write_request(tmp_path, base, name="sharded.json")

    unsharded_report = sim.run_sim(str(unsharded_req))
    sharded_report = sim.run_sim(str(sharded_req), hosts=3)

    assert [c["corner_id"] for c in sharded_report["corners"]] == [
        c["corner_id"] for c in unsharded_report["corners"]
    ]
    assert _stripped_report(sharded_report) == _stripped_report(unsharded_report)
    assert "remote" not in sharded_report["environment"]


def test_run_sim_monte_carlo_statistics_over_sharded_run_equal_unsharded(
    tmp_path, monkeypatch
):
    # `hosts > 1` must not change a single sampled value -- MC seeds are
    # already absolute (`sample_index`-derived, #348) -- so the merged
    # report's Monte Carlo statistics must equal the unsharded run's,
    # regardless of which shard/thread actually produced which sample.
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "backend": "local-parallel",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
        ],
        "monte_carlo": {"n": 12, "seed": 42, "vary": "mismatch"},
        "options": {"max_workers": 4},
    }

    def fake_run(cmd, capture_output, text, timeout):
        deck_path = cmd[2]
        log_path = cmd[cmd.index("-o") + 1]
        deck_text = Path(deck_path).read_text()
        sample_index = int(re.search(r"mc_sample_index=(\d+)", deck_text).group(1))
        value = 1.0 + 0.01 * sample_index
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "  Measurements for Transient Analysis\n\n"
                f"vout                =  {value:.5e}\n"
            )
        return _FakeCompleted("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    unsharded_req = _write_request(tmp_path, base, name="unsharded.json")
    sharded_req = _write_request(tmp_path, base, name="sharded.json")

    unsharded_report = sim.run_sim(str(unsharded_req))
    sharded_report = sim.run_sim(str(sharded_req), hosts=4)

    assert [c["corner_id"] for c in sharded_report["corners"]] == [
        c["corner_id"] for c in unsharded_report["corners"]
    ]
    assert sharded_report["measurements"] == unsharded_report["measurements"]
    assert sharded_report["corner_count"] == unsharded_report["corner_count"]
    assert sharded_report["passed"] == unsharded_report["passed"]


def test_run_sim_hosts_flag_overrides_request_field(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "corners": {"temperature_c": [10, 20, 30, 40]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "remote": {"hosts": 4},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_hosts = []
    real_shard = sim._shard_corner_points

    def spy(points, hosts):
        seen_hosts.append(hosts)
        return real_shard(points, hosts)

    monkeypatch.setattr(sim, "_shard_corner_points", spy)

    sim.run_sim(str(request), hosts=2)

    assert seen_hosts == [2]


def test_run_sim_hosts_request_field_used_when_flag_omitted(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "corners": {"temperature_c": [10, 20, 30, 40]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "remote": {"hosts": 4},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_hosts = []
    real_shard = sim._shard_corner_points

    def spy(points, hosts):
        seen_hosts.append(hosts)
        return real_shard(points, hosts)

    monkeypatch.setattr(sim, "_shard_corner_points", spy)

    sim.run_sim(str(request))

    assert seen_hosts == [4]


@pytest.mark.parametrize("bad_value", [0, -1, 1.5, "3"])
def test_run_sim_invalid_hosts_raises(tmp_path, bad_value):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "remote": {"hosts": bad_value},
        },
    )
    with pytest.raises(sim.SimError, match="hosts"):
        sim.run_sim(str(request))


def test_run_sim_hosts_greater_than_one_with_remote_backend_raises_before_any_aws_call(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "remote",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "models": {"pdk": "sky130A"},
            "remote": {
                "region": "us-east-1",
                "ssh_key_path": "/tmp/key.pem",
                "hosts": 2,
            },
        },
    )

    def _unexpected(*args, **kwargs):
        raise AssertionError(
            "hosts>1 + backend='remote' must not touch AWS in this issue"
        )

    monkeypatch.setattr(sim, "RemoteLauncher", _unexpected)

    with pytest.raises(sim.SimError, match="not yet supported"):
        sim.run_sim(str(request))


def test_cli_hosts_flag_shards_the_run(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "corners": {"temperature_c": [10, 20, 30, 40]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_hosts = []
    real_shard = sim._shard_corner_points

    def spy(points, hosts):
        seen_hosts.append(hosts)
        return real_shard(points, hosts)

    monkeypatch.setattr(sim, "_shard_corner_points", spy)

    main(["sim", str(request), "--hosts", "2", "--format", "json"])

    assert seen_hosts == [2]


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


def test_run_sim_keep_artifacts_exposes_deck_path(tmp_path, monkeypatch):
    """`artifacts.deck` references the exact synthesized `corner.cir` that
    `_write_corner_deck` already writes to disk (#356) -- same absolute
    path, same content, only populated when `keep_artifacts` is true."""
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

    deck_path = report["corners"][0]["artifacts"]["deck"]
    assert deck_path is not None
    deck_file = Path(deck_path)
    assert deck_file.is_relative_to(artifacts_dir)
    assert deck_file.name == "corner.cir"
    # `_write_corner_deck` synthesizes this file; confirm it's the real deck
    # ngspice consumed, not a stub -- the analysis card must be present.
    deck_text = deck_file.read_text()
    assert "tran 1n 1u" in deck_text


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


def test_run_sim_remote_backend_plumbs_ami_manifest_field_to_launcher(
    tmp_path, monkeypatch
):
    # request.remote.ami_manifest (issue #370) must reach
    # RemoteLauncher(manifest_path=...) unchanged -- the explicit-override
    # tier of remote_launcher's AMI manifest resolution order.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        _base_remote_request(
            remote={
                **_base_remote_request()["remote"],
                "ami_manifest": "/opt/klt/my-manifest.json",
            }
        ),
    )
    _install_fake_remote_transport(monkeypatch)

    sim.run_sim(str(request))

    assert (
        _FakeRemoteLauncher.last_instance.kwargs["manifest_path"]
        == "/opt/klt/my-manifest.json"
    )


def test_run_sim_remote_backend_omits_ami_manifest_defers_to_launcher_resolution(
    tmp_path, monkeypatch
):
    # No request.remote.ami_manifest -- manifest_path passed through as
    # None, so RemoteLauncher/load_ami_manifest's own $KLT_AMI_MANIFEST ->
    # user-scope -> packaged-default fallback chain applies.
    _write_body(tmp_path)
    request = _write_request(tmp_path, _base_remote_request())
    _install_fake_remote_transport(monkeypatch)

    sim.run_sim(str(request))

    assert _FakeRemoteLauncher.last_instance.kwargs["manifest_path"] is None


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
                        "deck": f"{remote_root}/default_novdd_27C/corner.cir",
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
    deck_path = report["corners"][0]["artifacts"]["deck"]
    assert deck_path == str(artifacts_dir / "default_novdd_27C" / "corner.cir")


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


@_SKIP_NO_NGSPICE
@pytest.mark.skipif(
    not EXAMPLES_DIR.exists(), reason="examples/sim/ fixtures not generated"
)
def test_examples_sim_monte_carlo_statistics_shape():
    """The Monte Carlo worked example, run live: `testbench-mc.spice`'s
    `agauss`-drawn `R1` must actually spread under the seed `klt sim` writes,
    and the sample set must reduce to the statistics block documented in
    docs/cli/sim.md. Stands in for the byte-exact golden fixture this example
    deliberately doesn't have (`runtime_s`/`engine_version` vary)."""
    request_path = EXAMPLES_DIR / "request-monte-carlo.json"
    if not request_path.exists():
        pytest.skip(
            "examples/sim/request-monte-carlo.json not generated -- "
            "run examples/sim/generate.py"
        )

    report = sim.run_sim(str(request_path))

    assert report["status"] == "pass"
    assert report["corner_count"] == 20
    assert report["environment"]["monte_carlo"]["quantiles"] == [5.0, 50.0, 95.0]

    mc = report["measurements"][0]["monte_carlo"]
    assert mc["n"] == 20
    assert mc["errored"] == 0
    # The seeded `agauss` draw actually varies -- not a pinned constant.
    assert mc["stddev"] > 0
    assert mc["min"] < mc["mean"] < mc["max"]
    assert mc["min"] <= mc["quantiles"]["p5"] <= mc["quantiles"]["p50"]
    assert mc["quantiles"]["p50"] <= mc["quantiles"]["p95"] <= mc["max"]
    window = mc["sigma_window"]
    assert window["k"] == 3.0
    assert window["low"] == pytest.approx(mc["mean"] - 3 * mc["stddev"])
    assert window["high"] == pytest.approx(mc["mean"] + 3 * mc["stddev"])
    assert window["status"] == "pass"
    assert [c["corner_id"] for c in mc["by_corner"]] == ["tt/novdd/27C"]


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


# --------------------------------------------------------------------------- #
# Remote worked example (examples/sim-remote/) -- Epic #253's closing
# validation matrix, committed as a worked example (#373).
#
# These tests are deliberately CI-checkable *without* AWS and without a
# 12-minute ngspice run: they validate the two requests through the same
# `sim.load_request()` path `klt sim` itself uses, exercise the remote
# block's pure-local cost gate, and assert the committed reference reports'
# documented equivalence (identical corner values across backends, the
# additive `environment.remote` block only on the remote one). Actually
# *running* `matrix-remote.request.json` needs AWS credentials.
# --------------------------------------------------------------------------- #

SIM_REMOTE_EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "sim-remote"

_SKIP_NO_SIM_REMOTE_EXAMPLE = pytest.mark.skipif(
    not SIM_REMOTE_EXAMPLES_DIR.exists(),
    reason="examples/sim-remote/ is not present",
)


def _load_sim_remote_json(name: str) -> dict:
    with open(SIM_REMOTE_EXAMPLES_DIR / name, encoding="utf-8") as handle:
        return json.load(handle)


@_SKIP_NO_SIM_REMOTE_EXAMPLE
def test_examples_sim_remote_requests_differ_only_by_backend():
    """The example's whole point: two requests that are the *same* corner
    matrix, differing only in where it runs. `load_request` is the shipped
    validation path (`klt sim` calls it before dispatch), so this is the
    schema check the issue's acceptance criteria ask for -- no AWS needed."""
    local = sim.load_request(str(SIM_REMOTE_EXAMPLES_DIR / "matrix-local.request.json"))
    remote = sim.load_request(
        str(SIM_REMOTE_EXAMPLES_DIR / "matrix-remote.request.json")
    )

    assert local["backend"] == "local"
    assert remote["backend"] == "remote"
    assert local["backend"] in sim.SUPPORTED_BACKENDS
    assert remote["backend"] in sim.SUPPORTED_BACKENDS

    # Everything except `backend` (and the remote-only provisioning block)
    # must be byte-identical -- otherwise the comparison proves nothing.
    assert {k: v for k, v in local.items() if k != "backend"} == {
        k: v for k, v in remote.items() if k not in ("backend", "remote")
    }

    # The netlist both requests name resolves next to them.
    assert (SIM_REMOTE_EXAMPLES_DIR / local["netlist"]).is_file()

    # `remote` requires `models.pdk` (docs/cli/sim.md's field table).
    assert remote["models"]["pdk"] == "sky130A"


@_SKIP_NO_SIM_REMOTE_EXAMPLE
def test_examples_sim_remote_request_carries_placeholder_credentials():
    """The committed request must never ship the validation run's real
    keypair/security-group identifiers (the issue calls this out
    explicitly) -- a reader substitutes their own."""
    remote = _load_sim_remote_json("matrix-remote.request.json")["remote"]

    for field in ("key_name", "ssh_key_path", "security_group_id"):
        assert "<" in remote[field] and ">" in remote[field], (
            f"remote.{field} must be a documented placeholder, not a real value"
        )
    assert not re.match(r"^sg-[0-9a-f]+$", remote["security_group_id"])


@_SKIP_NO_SIM_REMOTE_EXAMPLE
def test_examples_sim_remote_request_passes_the_cost_gate():
    """`remote_launcher`'s sizing + cost gate are pure local computation --
    they run to completion with no AWS credentials, so the example's
    `max_hourly_cost_usd` can be verified in CI to actually admit the
    instance the 5-corner matrix sizes to (the epic's `c7i.12xlarge`)."""
    request = _load_sim_remote_json("matrix-remote.request.json")
    remote = request["remote"]
    corner_count = len(request["corners"]["process"])

    instance_type = rl.select_instance_type(corner_count)
    assert instance_type == "c7i.12xlarge"

    hourly = rl.require_cost_config(
        region=remote["region"],
        instance_type=instance_type,
        spot=remote["spot"],
        max_hourly_cost_usd=remote["max_hourly_cost_usd"],
    )
    assert hourly <= remote["max_hourly_cost_usd"]


@_SKIP_NO_SIM_REMOTE_EXAMPLE
def test_examples_sim_remote_reference_reports_agree_across_backends():
    """Epic #253's headline claim, pinned as a test: the `remote` backend is
    the same code path on a different box, so every corner's measured value
    and verdict matches the `local` run exactly -- only `environment.remote`
    and wall-clock timing differ."""
    local = _load_sim_remote_json("matrix-local.report.json")
    remote = _load_sim_remote_json("matrix-remote.report.json")

    for report in (local, remote):
        assert report["schema_version"] == 1
        assert report["status"] == "pass"
        assert report["corner_count"] == 5
        assert report["passed"] == 5

    # Additive `environment.remote`, present only for the remote run.
    assert "remote" not in local["environment"]
    remote_env = remote["environment"]["remote"]
    for field in (
        "provider",
        "region",
        "instance_type",
        "instance_id",
        "spot",
        "estimated_hourly_cost_usd",
        "ami_id",
        "pdk_snapshot",
        "spin_up_s",
    ):
        assert field in remote_env, f"environment.remote is missing {field}"

    # Corner-for-corner, measurement-for-measurement equality.
    assert [c["corner_id"] for c in local["corners"]] == [
        c["corner_id"] for c in remote["corners"]
    ]
    for local_corner, remote_corner in zip(
        local["corners"], remote["corners"], strict=True
    ):
        assert local_corner["status"] == remote_corner["status"]
        assert local_corner["measurements"] == remote_corner["measurements"]

    # ... and the same rollup verdicts on top of them.
    assert local["measurements"] == remote["measurements"]
