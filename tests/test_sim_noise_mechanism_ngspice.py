"""``measurements[].noise.mechanism`` against a real ngspice (#5553).

The engine-gated half of the form's coverage: that the thermal and flicker
shares `klt sim` reports really do add in power to the total form's number,
on decks whose noise is dominated by a known generator family. Split from
`test_sim_noise_mechanism.py` (its unit half) only to keep each file inside
the repo's per-file size ratchet.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim

_SKIP_NO_NGSPICE = pytest.mark.skipif(
    shutil.which("ngspice") is None or os.environ.get("KLT_SKIP_NGSPICE_TESTS") == "1",
    reason="ngspice is not installed on this machine",
)


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _run(tmp_path, analysis, measurements):
    request = _write_request(
        tmp_path,
        {"netlist": "tb.spice", "analysis": analysis, "measurements": measurements},
    )
    report = sim.run_sim(str(request))
    (corner,) = report["corners"]
    return corner


def _write_mos(tmp_path: Path) -> None:
    (tmp_path / "tb.spice").write_text(
        ".model nm nmos level=1 kf=1e-24 af=1.0 vto=0.5 kp=100u\n"
        "Vdd vdd 0 2\n"
        "V1 in 0 DC 0.7 AC 1\n"
        "Rload vdd out 10k\n"
        "M1 out in 0 0 nm w=10u l=1u\n"
    )


def _mechanism_set(band):
    return [
        {"name": "total", "noise": {"quantity": "onoise", "band_hz": band}},
        {
            "name": "thermal",
            "noise": {"quantity": "onoise", "band_hz": band, "mechanism": "thermal"},
        },
        {
            "name": "flicker",
            "noise": {"quantity": "onoise", "band_hz": band, "mechanism": "flicker"},
        },
    ]


@_SKIP_NO_NGSPICE
def test_thermal_and_flicker_add_in_power_to_the_total_form(tmp_path):
    _write_mos(tmp_path)
    corner = _run(
        tmp_path,
        {"kind": "noise", "args": "v(out) V1 dec 20 10 1e7 1"},
        _mechanism_set([100, 1e6]),
    )
    assert corner["diagnostics"] == [], corner["diagnostics"]
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert math.hypot(values["thermal"], values["flicker"]) == pytest.approx(
        values["total"], rel=1e-3
    )
    # A kf=1e-24 device over a decade from 100 Hz is flicker-dominated.
    assert values["flicker"] > values["thermal"]


@_SKIP_NO_NGSPICE
def test_the_completeness_evidence_rides_the_response(tmp_path):
    _write_mos(tmp_path)
    corner = _run(
        tmp_path,
        {"kind": "noise", "args": "v(out) V1 dec 20 10 1e7 1"},
        _mechanism_set([100, 1e6]),
    )
    entry = next(m for m in corner["measurements"] if m["name"] == "thermal")
    assert entry["noise"]["mechanism"] == "thermal"
    assert entry["noise"]["unclassified_suffixes"] == []
    assert entry["noise"]["residual_relative"] == pytest.approx(0.0, abs=1e-9)
    assert entry["noise"]["total"] == pytest.approx(
        next(m for m in corner["measurements"] if m["name"] == "total")["value"],
        rel=1e-6,
    )
    # The total form keeps its old shape: no `noise` sibling.
    assert "noise" not in next(
        m for m in corner["measurements"] if m["name"] == "total"
    )


@_SKIP_NO_NGSPICE
def test_a_resistor_only_deck_is_all_thermal_and_no_flicker(tmp_path):
    (tmp_path / "tb.spice").write_text(
        "V1 in 0 DC 0 AC 1\nR1 in out 10k\nC1 out 0 1n\n"
    )
    corner = _run(
        tmp_path,
        {"kind": "noise", "args": "v(out) V1 dec 50 10 1e7 1"},
        _mechanism_set([1e3, 1e5]),
    )
    assert corner["diagnostics"] == [], corner["diagnostics"]
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert values["flicker"] == 0.0
    assert values["thermal"] == pytest.approx(values["total"], rel=1e-6)


@_SKIP_NO_NGSPICE
def test_a_diode_raises_the_unclassified_diagnostic_without_losing_the_value(tmp_path):
    (tmp_path / "tb.spice").write_text(
        ".model nm nmos level=1 kf=1e-24 af=1.0 vto=0.5 kp=100u\n"
        ".model dmod d is=1e-14 rs=5\n"
        "Vdd vdd 0 2\n"
        "V1 in 0 DC 0.7 AC 1\n"
        "Rload vdd out 10k\n"
        "M1 out in 0 0 nm w=10u l=1u\n"
        "D1 vdd out dmod\n"
    )
    corner = _run(
        tmp_path,
        {"kind": "noise", "args": "v(out) V1 dec 20 10 1e7 1"},
        [
            {
                "name": "thermal",
                "noise": {
                    "quantity": "onoise",
                    "band_hz": [100, 1e6],
                    "mechanism": "thermal",
                },
            }
        ],
    )
    (diagnostic,) = corner["diagnostics"]
    assert diagnostic["severity"] == "warning"
    assert diagnostic["code"] == "noise_mechanism_incomplete"
    assert "id" in diagnostic["message"]
    entry = corner["measurements"][0]
    assert entry["value"] is not None and entry["status"] == "pass"
    assert entry["noise"]["unclassified_suffixes"] == ["id"]


@_SKIP_NO_NGSPICE
def test_hierarchical_instance_names_are_aggregated_without_being_named(tmp_path):
    (tmp_path / "tb.spice").write_text(
        ".model nm nmos level=1 kf=1e-24 af=1.0 vto=0.5 kp=100u\n"
        ".subckt amp d g\nMi d g 0 0 nm w=10u l=1u\n.ends\n"
        "Vdd vdd 0 2\n"
        "V1 in 0 DC 0.7 AC 1\n"
        "Rload vdd out 10k\n"
        "X1 out in amp\n"
    )
    corner = _run(
        tmp_path,
        {"kind": "noise", "args": "v(out) V1 dec 20 10 1e7 1"},
        _mechanism_set([100, 1e6]),
    )
    assert corner["diagnostics"] == [], corner["diagnostics"]
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert values["flicker"] > 0.0
    assert math.hypot(values["thermal"], values["flicker"]) == pytest.approx(
        values["total"], rel=1e-3
    )


@_SKIP_NO_NGSPICE
def test_spot_shares_add_in_power_on_and_between_grid_points(tmp_path):
    _write_mos(tmp_path)
    args = {"kind": "noise", "args": "v(out) V1 dec 20 10 1e7 1"}
    for hz in (1e3, 12345.0):
        corner = _run(
            tmp_path,
            args,
            [
                {"name": "total", "noise": {"quantity": "onoise", "at_hz": hz}},
                {
                    "name": "thermal",
                    "noise": {
                        "quantity": "onoise",
                        "at_hz": hz,
                        "mechanism": "thermal",
                    },
                },
                {
                    "name": "flicker",
                    "noise": {
                        "quantity": "onoise",
                        "at_hz": hz,
                        "mechanism": "flicker",
                    },
                },
            ],
        )
        values = {m["name"]: m["value"] for m in corner["measurements"]}
        assert math.hypot(values["thermal"], values["flicker"]) == pytest.approx(
            values["total"], rel=1e-3
        ), hz


@_SKIP_NO_NGSPICE
def test_missing_pts_per_summary_is_refused_before_any_run(tmp_path):
    _write_mos(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "tb.spice",
            "analysis": {"kind": "noise", "args": "v(out) V1 dec 20 10 1e7"},
            "measurements": [
                {
                    "name": "thermal",
                    "noise": {
                        "quantity": "onoise",
                        "band_hz": [100, 1e6],
                        "mechanism": "thermal",
                    },
                }
            ],
        },
    )
    with pytest.raises(sim.SimError, match="pts_per_summary"):
        sim.run_sim(str(request))


# ----------------------------------------------- noise.top_n (issue #5558)


def _top_n_set(top_n, **where):
    """A plain total entry and the same entry asking for `top_n` contributors."""
    body = {"quantity": "onoise", **where}
    return [
        {"name": "total", "noise": dict(body)},
        {"name": "ranked", "noise": {**body, "top_n": top_n}},
    ]


def _by_name(corner):
    return {m["name"]: m for m in corner["measurements"]}


_NOISE_ARGS = {"kind": "noise", "args": "v(out) V1 dec 20 10 1e7 1"}


@_SKIP_NO_NGSPICE
def test_top_n_leaves_the_total_value_untouched_and_ranks_the_leaves(tmp_path):
    _write_mos(tmp_path)
    corner = _run(tmp_path, _NOISE_ARGS, _top_n_set(10, band_hz=[100, 1e6]))
    assert corner["diagnostics"] == [], corner["diagnostics"]
    entries = _by_name(corner)
    total, ranked = entries["total"], entries["ranked"]
    assert {k: ranked[k] for k in ("value", "status", "margin")} == {
        k: total[k] for k in ("value", "status", "margin")
    }
    assert "noise" not in total
    contributors = ranked["noise"]["contributors"]
    assert [c["name"] for c in contributors] == ["m1_1overf", "m1_id", "rload_thermal"]
    assert [c["mechanism"] for c in contributors] == ["flicker", "thermal", "thermal"]
    values = [c["value"] for c in contributors]
    assert values == sorted(values, reverse=True)
    # Zero-power leaves (the level-1 device's `rd`/`rs`) never pad the list.
    assert all(v > 0.0 for v in values)
    # Amplitudes add in power, so the list is the decomposition of the value.
    power_sum = math.sqrt(sum(v * v for v in values))
    assert power_sum == pytest.approx(total["value"], rel=1e-3)


@_SKIP_NO_NGSPICE
@pytest.mark.parametrize("hz", [1e3, 12345.0])
def test_top_n_spot_values_add_in_power_on_and_between_grid_points(tmp_path, hz):
    _write_mos(tmp_path)
    corner = _run(tmp_path, _NOISE_ARGS, _top_n_set(10, at_hz=hz))
    entries = _by_name(corner)
    assert entries["ranked"]["value"] == entries["total"]["value"]
    power_sum = math.sqrt(
        sum(c["value"] ** 2 for c in entries["ranked"]["noise"]["contributors"])
    )
    assert power_sum == pytest.approx(entries["total"]["value"], rel=1e-3)


@_SKIP_NO_NGSPICE
def test_top_n_one_is_the_largest_contributor_alone(tmp_path):
    _write_mos(tmp_path)
    everything = _by_name(
        _run(tmp_path, _NOISE_ARGS, _top_n_set(100, band_hz=[100, 1e6]))
    )["ranked"]["noise"]["contributors"]
    (only,) = _by_name(_run(tmp_path, _NOISE_ARGS, _top_n_set(1, band_hz=[100, 1e6])))[
        "ranked"
    ]["noise"]["contributors"]
    assert only == everything[0]


@_SKIP_NO_NGSPICE
def test_top_n_with_a_mechanism_ranks_only_that_mechanisms_leaves(tmp_path):
    _write_mos(tmp_path)
    band = [100, 1e6]
    corner = _run(
        tmp_path,
        _NOISE_ARGS,
        [
            {
                "name": "flicker",
                "noise": {
                    "quantity": "onoise",
                    "band_hz": band,
                    "mechanism": "flicker",
                },
            },
            {
                "name": "ranked",
                "noise": {
                    "quantity": "onoise",
                    "band_hz": band,
                    "mechanism": "flicker",
                    "top_n": 5,
                },
            },
        ],
    )
    entries = _by_name(corner)
    assert entries["ranked"]["value"] == entries["flicker"]["value"]
    noise = entries["ranked"]["noise"]
    # The completeness evidence #5553 ships is intact; the list joins it.
    assert noise["mechanism"] == "flicker" and noise["unclassified_suffixes"] == []
    assert [c["mechanism"] for c in noise["contributors"]] == ["flicker"]
    assert "contributors" not in entries["flicker"]["noise"]


@_SKIP_NO_NGSPICE
def test_top_n_names_a_hierarchical_instance_path_verbatim(tmp_path):
    (tmp_path / "tb.spice").write_text(
        ".model nm nmos level=1 kf=1e-24 af=1.0 vto=0.5 kp=100u\n"
        ".subckt amp d g\nMi d g 0 0 nm w=10u l=1u\n.ends\n"
        "Vdd vdd 0 2\n"
        "V1 in 0 DC 0.7 AC 1\n"
        "Rload vdd out 10k\n"
        "X1 out in amp\n"
    )
    corner = _run(tmp_path, _NOISE_ARGS, _top_n_set(1, band_hz=[100, 1e6]))
    (top,) = _by_name(corner)["ranked"]["noise"]["contributors"]
    # `.` and `_` both occur in one name; nothing is split or normalised.
    assert top == {
        "name": "m.x1.mi_1overf",
        "value": top["value"],
        "mechanism": "flicker",
    }


@_SKIP_NO_NGSPICE
def test_top_n_survives_keep_artifacts_false(tmp_path):
    """The rawfile is reduced inside the corner, so the artifacts directory
    being cleaned afterwards (what a `batch` worker does) loses nothing."""
    _write_mos(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "tb.spice",
            "analysis": _NOISE_ARGS,
            "options": {"keep_artifacts": False},
            "measurements": _top_n_set(2, band_hz=[100, 1e6]),
        },
    )
    (corner,) = sim.run_sim(str(request))["corners"]
    contributors = _by_name(corner)["ranked"]["noise"]["contributors"]
    assert [c["name"] for c in contributors] == ["m1_1overf", "m1_id"]


@_SKIP_NO_NGSPICE
def test_top_n_without_pts_per_summary_is_refused_before_any_run(tmp_path):
    _write_mos(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "tb.spice",
            "analysis": {"kind": "noise", "args": "v(out) V1 dec 20 10 1e7"},
            "measurements": _top_n_set(3, band_hz=[100, 1e6]),
        },
    )
    with pytest.raises(sim.SimError, match=r"ranked.*noise\.top_n 3.*pts_per_summary"):
        sim.run_sim(str(request))
