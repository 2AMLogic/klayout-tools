"""Tests for `klt sim`'s solver-option corner axis (`corners.options`, issue #3125).

A tolerance ladder (`.options reltol` rungs) cannot be a `.control` loop --
`.options` cannot be altered from the control language -- so the axis zips
equal-length arrays of numbers by index, sits innermost (below `params`), and
is emitted as one `.options` card per point in the generated deck. Sibling of
`tests/test_sim_params.py`: unit tests need no simulator, the order
measurement runs the real `ngspice -b` and skips without it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import op_sanity, sim, sim_remote

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)


def _expand(corners: dict, exclude: list | None = None) -> list[sim.CornerPoint]:
    return sim._expand_corners(corners, exclude or [])


# --------------------------------------------------------------------------- #
# Expansion, identity, validation
# --------------------------------------------------------------------------- #


def test_two_reltol_rungs_make_two_corners_with_option_suffixed_ids():
    points = _expand({"options": {"reltol": [1e-3, 1e-4]}})
    assert [p.corner_id for p in points] == [
        "default/novdd/27C/options:reltol=0.001",
        "default/novdd/27C/options:reltol=0.0001",
    ]
    assert [p.options for p in points] == [{"reltol": 0.001}, {"reltol": 0.0001}]


def test_names_zip_by_index_and_are_lowercased():
    points = _expand({"options": {"RelTol": [1e-3, 1e-4], "itl4": [10, 100]}})
    assert [p.options for p in points] == [
        {"itl4": 10, "reltol": 0.001},
        {"itl4": 100, "reltol": 0.0001},
    ]


def test_options_axis_is_innermost_below_params():
    points = _expand(
        {
            "temperature_c": [27, 85],
            "params": {"r": [1, 2]},
            "options": {"reltol": [1e-3, 1e-4]},
        }
    )
    assert [p.corner_id for p in points[:4]] == [
        "default/novdd/27C/params:r=1/options:reltol=0.001",
        "default/novdd/27C/params:r=1/options:reltol=0.0001",
        "default/novdd/27C/params:r=2/options:reltol=0.001",
        "default/novdd/27C/params:r=2/options:reltol=0.0001",
    ]
    assert len(points) == 8


def test_omitted_or_empty_options_preserve_legacy_ids_and_slugs():
    legacy = _expand({"temperature_c": [27, 85]})
    for corners in (
        {"temperature_c": [27, 85], "options": {}},
        {"temperature_c": [27, 85], "options": None},
    ):
        points = _expand(corners)
        assert [p.corner_id for p in points] == [p.corner_id for p in legacy]
        assert [p.slug for p in points] == [p.slug for p in legacy]
        assert all(p.options == {} for p in points)


def test_slug_is_hashed_and_distinguishes_close_values():
    a, b = _expand({"options": {"reltol": [1e-4, 1.0000000000000002e-4]}})
    assert a.slug != b.slug
    assert "_params_" in a.slug and "/" not in a.slug and "=" not in a.slug
    both = _expand({"params": {"r": [1]}, "options": {"reltol": [1e-4]}})[0]
    assert both.slug.startswith("default_novdd_27C_params_")
    assert both.slug != _expand({"params": {"r": [1]}})[0].slug


def test_mc_suffix_follows_options():
    points, _ = sim._expand_monte_carlo(
        _expand({"options": {"reltol": [1e-3, 1e-4]}}),
        {"n": 2, "seed": 3, "vary": "both"},
    )
    assert points[0].corner_id == "default/novdd/27C/options:reltol=0.001/mc0"
    assert all(p.options for p in points)
    assert len({p.slug for p in points}) == len(points)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ("reltol", "must be an object"),
        ({"bogus": [1]}, "not a supported numeric solver option"),
        ({"method": [1]}, "not a supported numeric solver option"),
        ({"reltol": []}, "nonempty array"),
        ({"reltol": 1e-3}, "nonempty array"),
        ({"reltol": [True]}, "finite number"),
        ({"reltol": ["1e-3"]}, "finite number"),
        ({"reltol": [float("nan")]}, "finite number"),
        ({"reltol": [1e-3, 1e-4], "abstol": [1e-12]}, "same length"),
        ({"reltol": [1e-3, 1e-3]}, "twice"),
        ({"reltol": [1e-3], "RELTOL": [1e-4]}, "more than once"),
    ],
)
def test_malformed_options_are_rejected(options, message):
    with pytest.raises(sim.SimError, match=message):
        _expand({"options": options})


def test_allow_list_covers_the_documented_minimum():
    for name in ("reltol", "abstol", "vntol", "itl1", "itl2", "itl3", "itl4", "gmin"):
        assert name in sim.CORNER_OPTION_NAMES


# --------------------------------------------------------------------------- #
# exclude
# --------------------------------------------------------------------------- #


def test_exclude_options_selects_a_rung():
    points = _expand(
        {"temperature_c": [27, 85], "options": {"reltol": [1e-3, 1e-4]}},
        [{"temperature_c": 85, "options": {"reltol": 1e-4}}],
    )
    assert [p.corner_id for p in points] == [
        "default/novdd/27C/options:reltol=0.001",
        "default/novdd/27C/options:reltol=0.0001",
        "default/novdd/85C/options:reltol=0.001",
    ]


def test_exclude_unknown_option_name_is_rejected():
    with pytest.raises(sim.SimError, match="not a corners.options option"):
        _expand({"options": {"reltol": [1e-3]}}, [{"options": {"abstol": 1e-12}}])
    with pytest.raises(sim.SimError, match="not a corners.options option"):
        _expand({}, [{"options": {"reltol": 1e-3}}])
    with pytest.raises(sim.SimError, match="must be an object"):
        _expand({"options": {"reltol": [1e-3]}}, [{"options": 1}])


# --------------------------------------------------------------------------- #
# Wire, report, engine refusals
# --------------------------------------------------------------------------- #


def test_wire_round_trip_preserves_options():
    points, _ = sim._expand_monte_carlo(
        _expand({"params": {"r": [1]}, "options": {"reltol": [1e-3, 1e-4]}}),
        {"n": 2, "seed": 3, "vary": "both"},
    )
    back = sim._corner_points_from_wire(
        json.loads(json.dumps(sim._corner_points_to_wire(points)))
    )
    assert [p.corner_id for p in back] == [p.corner_id for p in points]
    assert [p.options for p in back] == [p.options for p in points]


def test_wire_omits_the_field_without_options_and_revalidates_it():
    (point,) = _expand({})
    assert "options" not in sim._corner_points_to_wire([point])[0]
    wire = sim._corner_points_to_wire(_expand({"options": {"reltol": [1e-3]}}))
    wire[0]["options"] = {"method": 1}
    with pytest.raises(sim.SimError, match="not a supported"):
        sim._corner_points_from_wire(wire)


def test_unrun_corner_report_carries_options():
    (point,) = _expand({"options": {"reltol": [1e-4]}})
    report = sim_remote._unrun_corner_report(point, [], "lost_shard")
    assert report["options"] == {"reltol": 0.0001}
    (legacy,) = _expand({})
    assert "options" not in sim_remote._unrun_corner_report(legacy, [], "lost_shard")


def test_xyce_refuses_an_options_axis():
    with pytest.raises(sim.SimError, match="corners.options"):
        sim._enforce_xyce_support_boundary(
            engine="xyce",
            backend="local",
            corners_spec={"options": {"reltol": [1e-3]}},
            monte_carlo_declared=False,
            fail_fast_probe=False,
            osdi_preload_declared=False,
            expr_measurements_declared=False,
            save_mode_declared=False,
            analysis_steps_declared=False,
            crossings_declared=False,
        )


def _legacy_corner(point: sim.CornerPoint) -> dict:
    return {
        "corner_id": point.corner_id.split("/options:")[0],
        "status": "pass",
        "diagnostics": [],
    }


def _honest_corner(point: sim.CornerPoint) -> dict:
    return {
        "corner_id": point.corner_id,
        "options": dict(point.options),
        "status": "pass",
        "diagnostics": [],
    }


def test_updated_worker_echo_is_accepted():
    points = _expand({"options": {"reltol": [1e-3, 1e-4]}})
    sim._require_options_echo(points, [_honest_corner(p) for p in points])


def test_worker_that_drops_the_axis_cannot_return_a_compatible_pass():
    points = _expand({"options": {"reltol": [1e-3, 1e-4]}})
    with pytest.raises(sim.SimError) as raised:
        sim._require_options_echo(points, [_legacy_corner(p) for p in points])
    assert raised.value.code == sim.OPTIONS_UNSUPPORTED_WORKER_CODE


def test_requests_without_options_and_local_backends_are_never_gated():
    points = _expand({"temperature_c": [27, 85]})
    sim._require_options_echo(points, [_legacy_corner(p) for p in points])
    opt_points = _expand({"options": {"reltol": [1e-3]}})
    sim._require_options_echo(
        opt_points, [_legacy_corner(p) for p in opt_points], backend="local"
    )


def test_op_sanity_refuses_an_options_axis():
    (point,) = _expand({"options": {"reltol": [1e-3]}})
    with pytest.raises(op_sanity.OpSanityError, match="corners.options"):
        op_sanity._refuse_param_axis(point)


# --------------------------------------------------------------------------- #
# Deck emission and the measured injection order
# --------------------------------------------------------------------------- #


def _write_deck(tmp_path: Path, point: sim.CornerPoint, **kwargs) -> list[str]:
    deck = tmp_path / "deck.spice"
    sim._write_corner_deck(
        deck_path=str(deck),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=point,
        analysis={"kind": "op", "args": ""},
        measurements_spec=[],
        raw_path=None,
        **kwargs,
    )
    return deck.read_text().splitlines()


def test_decks_of_two_rungs_differ_only_by_the_options_card(tmp_path):
    first, second = _expand({"options": {"reltol": [1e-3, 1e-4]}})
    a = _write_deck(tmp_path, first)
    b = _write_deck(tmp_path, second)
    assert [x for x in a if x not in b] == [".options reltol=0.001"]
    assert [x for x in b if x not in a] == [".options reltol=0.0001"]
    assert len(a) == len(b)
    # Ahead of the body include and of the control block.
    assert a.index(".options reltol=0.001") < a.index(f".include {tmp_path}/body.spice")


def test_deck_without_options_is_unchanged(tmp_path):
    (point,) = _expand({})
    assert not any(line.startswith(".options") for line in _write_deck(tmp_path, point))


def test_options_card_precedes_request_level_ngspice_options(tmp_path):
    (point,) = _expand({"options": {"reltol": [1e-4], "itl4": [100]}})
    lines = _write_deck(tmp_path, point, ngspice_options={"reltol": 1e-2})
    assert ".options itl4=100 reltol=0.0001" in lines
    assert lines.index(".options itl4=100 reltol=0.0001") < lines.index(
        ".options reltol=0.01"
    )


@_SKIP_NO_NGSPICE
def test_real_ngspice_corner_card_wins_over_body_and_request_options(tmp_path):
    """Measured, not assumed: ngspice honors the FIRST `.options` card that
    sets a name, so the generated card (ahead of the `.include`) beats a
    body-level `.options reltol=` and `options.ngspice_options`."""
    (tmp_path / "body.spice").write_text("V1 in 0 1\nR1 in 0 1k\n.options reltol=0.5\n")
    (point,) = _expand({"options": {"reltol": [1e-4]}})
    lines = _write_deck(tmp_path, point, ngspice_options={"reltol": 0.25})
    # Report the effective option from inside the run.
    lines.insert(lines.index("quit"), "echo EFFECTIVE_RELTOL=$reltol")
    deck = tmp_path / "probe.spice"
    deck.write_text("\n".join(lines) + "\n")
    out = subprocess.run(
        ["ngspice", "-b", str(deck)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    ).stdout
    effective = [x for x in out.splitlines() if x.startswith("EFFECTIVE_RELTOL=")]
    assert effective and float(effective[0].split("=", 1)[1]) == pytest.approx(1e-4)

    # Control: without the corner axis the body-level card is the one in force.
    (plain,) = _expand({})
    base = _write_deck(tmp_path, plain, ngspice_options={"reltol": 0.25})
    base.insert(base.index("quit"), "echo EFFECTIVE_RELTOL=$reltol")
    deck.write_text("\n".join(base) + "\n")
    out = subprocess.run(
        ["ngspice", "-b", str(deck)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    ).stdout
    effective = [x for x in out.splitlines() if x.startswith("EFFECTIVE_RELTOL=")]
    assert float(effective[0].split("=", 1)[1]) == pytest.approx(0.5)


@_SKIP_NO_NGSPICE
def test_real_ngspice_run_sim_reports_each_rung(tmp_path):
    (tmp_path / "body.spice").write_text("V1 in 0 1\nR1 in 0 1k\n")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "op", "args": ""},
                "measurements": [{"name": "i1", "expr": "i(v1)", "unit": "A"}],
                "corners": {"options": {"reltol": [1e-3, 1e-4]}},
                "backend": "local",
            }
        )
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    assert [c["options"] for c in report["corners"]] == [
        {"reltol": 0.001},
        {"reltol": 0.0001},
    ]
    assert [c["corner_id"] for c in report["corners"]] == [
        "default/novdd/27C/options:reltol=0.001",
        "default/novdd/27C/options:reltol=0.0001",
    ]
