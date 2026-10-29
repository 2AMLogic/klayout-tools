"""Tests for ``measurements[].stress`` -- the worst |V| across a node pair over
a settled transient window from one declaration (issue #4592).

Unit tests (validation, reduction, deck emission, grading through a stubbed
``ngspice``) always run; the integration test runs one real single-corner
``ngspice -b`` and skips when ngspice is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_stress

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

_DECL = {
    "name": "vstress_md1",
    "stress": {"a": "d1", "b": "s1", "from": "20n", "to": "90n"},
    "unit": "V",
    "limits": {"max": 5.5},
}


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _write_body(tmp_path: Path) -> Path:
    path = tmp_path / "body.spice"
    path.write_text("Vdd a 0 DC 1\nR1 a 0 1k\n")
    return path


# --------------------------------------------------------------------------- #
# reduce_stress: window, sign, interpolation
# --------------------------------------------------------------------------- #


def test_spike_outside_the_window_is_ignored():
    scale = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    diff = [9.0, 1.0, 1.5, 1.2, 1.0, -8.0]
    out = sim_stress.reduce_stress(scale, diff, start=1.0, stop=4.0)
    assert out.error is None
    assert out.peak == pytest.approx(1.5)
    assert out.peak_time == pytest.approx(2.0)
    assert (out.start, out.stop) == (1.0, 4.0)


def test_whole_run_is_the_default_window():
    out = sim_stress.reduce_stress([0.0, 1.0, 2.0], [0.5, -3.0, 1.0])
    assert out.peak == pytest.approx(3.0)
    assert out.peak_time == pytest.approx(1.0)


def test_negative_excursion_counts_by_magnitude():
    out = sim_stress.reduce_stress([0.0, 1.0], [-2.0, 1.0])
    assert out.peak == pytest.approx(2.0)


def test_window_edge_between_samples_is_interpolated():
    # Linear 0 -> 10 over [0, 10]; the window ends at t=4 -> 4.
    out = sim_stress.reduce_stress([0.0, 10.0], [0.0, 10.0], start=1.0, stop=4.0)
    assert out.peak == pytest.approx(4.0)
    assert out.peak_time == pytest.approx(4.0)


def test_window_with_no_interior_sample_still_has_a_value():
    out = sim_stress.reduce_stress([0.0, 10.0], [2.0, 4.0], start=3.0, stop=4.0)
    assert out.error is None
    assert out.peak == pytest.approx(2.8)


def test_window_outside_the_samples_is_an_error():
    out = sim_stress.reduce_stress([0.0, 1.0], [0.0, 1.0], start=0.5, stop=2.0)
    assert out.error is not None
    assert "outside the saved samples" in out.error


def test_single_sample_is_an_error():
    assert sim_stress.reduce_stress([0.0], [1.0]).error is not None


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("stress", "match"),
    [
        ({"b": "s1"}, "stress.a is required"),
        ({"a": "d 1"}, "stress.a is required"),
        ({"a": "d1)\nquit"}, "stress.a is required"),
        ({"a": "d1", "b": 3}, "stress.b must be"),
        ({"a": "d1", "from": "soon"}, "stress.from"),
        ({"a": "d1", "from": "2u", "to": "1u"}, "must be after"),
        ({"a": "d1", "from": 1, "to": 1}, "must be after"),
        ({"a": "d1", "peak": True}, "unknown stress key"),
        ("d1", "must be an object"),
    ],
)
def test_malformed_stress_declarations_are_refused(stress, match):
    with pytest.raises(sim.SimError, match=match):
        sim._validate_measurement_forms([{"name": "s", "stress": stress}])


@pytest.mark.parametrize(
    "other",
    [
        {"spice": ".meas tran t MAX v(a)"},
        {"expr": "v(a)"},
        {"crossings": {"signal": "v(a)", "level": 0.5}},
        {"energy": {"source": "vdd", "voltage": "v(a)"}},
    ],
)
def test_stress_combined_with_another_form_is_refused(other):
    spec = {"name": "s", "stress": {"a": "d1"}, **other}
    with pytest.raises(sim.SimError, match="declare exactly one"):
        sim._validate_measurement_forms([spec])


def test_stress_with_only_ground_reference_is_accepted():
    sim._validate_measurement_forms([{"name": "s", "stress": {"a": "d1"}}])


@pytest.mark.parametrize("kind", ["op", "dc", "ac"])
def test_stress_requires_a_transient(tmp_path, kind):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": kind, "args": ""},
            "measurements": [_DECL],
        },
    )
    with pytest.raises(sim.SimError, match='requires analysis.kind "tran"'):
        sim.run_sim(str(request))


@pytest.mark.parametrize(
    ("window", "args"),
    [
        ({"from": "10n", "to": "2u"}, "1n 1u"),
        ({"from": "1n", "to": "50n"}, "1n 100n 10n"),
    ],
)
def test_stress_window_outside_the_analysis_is_refused(window, args):
    spec = {"name": "s", "stress": {"a": "d1", **window}}
    with pytest.raises(sim.SimError, match="outside the transient analysis window"):
        sim_stress.validate_stress_context([spec], {"kind": "tran", "args": args})


def test_stress_window_inside_the_analysis_is_accepted():
    sim_stress.validate_stress_context([_DECL], {"kind": "tran", "args": "1n 100n"})


def test_stress_is_refused_for_xyce(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 100n"},
            "measurements": [_DECL],
        },
    )
    with pytest.raises(sim.SimError, match="stress is not supported for engine 'xyce'"):
        sim.run_sim(str(request))


def test_stress_is_refused_with_analysis_steps(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis_steps": [
                {
                    "name": "s1",
                    "analysis": {"kind": "tran", "args": "1n 100n"},
                    "measurements": [_DECL],
                }
            ],
        },
    )
    with pytest.raises(sim.SimError, match="not supported with analysis_steps"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def _deck(tmp_path: Path, measurements_spec) -> list[str]:
    deck_path = tmp_path / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=sim.CornerPoint(None, {}, 27),
        analysis={"kind": "tran", "args": "1n 100n"},
        measurements_spec=measurements_spec,
        raw_path=None,
    )
    return deck_path.read_text().splitlines()


def test_deck_dumps_the_difference_after_the_analysis(tmp_path):
    lines = _deck(tmp_path, [_DECL])
    tran = lines.index("tran 1n 100n")
    assert lines.index("set filetype=ascii") < tran
    raw = tmp_path / "stress_0.raw"
    let = lines.index("let __klt_vs_0 = v(d1) - v(s1)")
    write = lines.index(f"write {raw} __klt_vs_0")
    assert tran < let < write < lines.index("quit")


def test_deck_without_b_measures_against_ground(tmp_path):
    lines = _deck(tmp_path, [{"name": "s", "stress": {"a": "d1"}}])
    assert "let __klt_vs_0 = v(d1)" in lines


def test_deck_without_stress_is_unchanged(tmp_path):
    lines = _deck(tmp_path, [{"name": "v", "spice": ".meas tran v MAX v(a)"}])
    assert not any("__klt_vs" in line or "filetype" in line for line in lines)


# --------------------------------------------------------------------------- #
# run_sim with ngspice stubbed: grading and the response shape
# --------------------------------------------------------------------------- #


def _rawfile(points: list[tuple[float, float]]) -> str:
    lines = [
        "Title: stub",
        "Plotname: Transient Analysis",
        "Flags: real",
        "No. Variables: 2",
        f"No. Points: {len(points)}",
        "Variables:",
        "\t0\ttime\ttime",
        "\t1\t__klt_vs_0\tvoltage",
        "Values:",
    ]
    for index, (t, v) in enumerate(points):
        lines.append(f" {index}\t{t!r}")
        lines.append(f"\t{v!r}")
    return "\n".join(lines) + "\n"


def _stub_ngspice(monkeypatch, *, raw: str | None):
    real_which = shutil.which
    monkeypatch.setattr(
        paths_module.shutil,
        "which",
        lambda cmd, *a, **k: cmd if cmd == "ngspice" else real_which(cmd, *a, **k),
    )

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("No. of Data Rows : 5\n")
        if raw is not None:
            with open(os.path.join(cwd, "stress_0.raw"), "w", encoding="utf-8") as h:
                h.write(raw)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


#: A 7 V edge-charging spike at t=0, then a settled 5 V (never above it).
_WAVE = [(0.0, 7.0), (1e-8, 5.0), (3e-8, 5.0), (6e-8, 4.0), (1e-7, 4.0)]


def _request(tmp_path: Path, decl: dict) -> Path:
    _write_body(tmp_path)
    return _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "10n 100n"},
            "measurements": [decl],
            "options": {"keep_artifacts": False},
        },
    )


def test_settled_window_passes_a_limit_the_spike_would_break(tmp_path, monkeypatch):
    _stub_ngspice(monkeypatch, raw=_rawfile(_WAVE))
    decl = {
        "name": "vs",
        "stress": {"a": "d1", "from": "10n", "to": "100n"},
        "unit": "V",
        "limits": {"max": 5.5},
    }

    report = sim.run_sim(str(_request(tmp_path, decl)))

    (measurement,) = report["corners"][0]["measurements"]
    assert measurement["status"] == "pass"
    assert measurement["value"] == pytest.approx(5.0)
    assert measurement["margin"] == pytest.approx(0.5)
    assert measurement["stress"]["from"] == pytest.approx(1e-8)
    assert measurement["stress"]["to"] == pytest.approx(1e-7)
    assert measurement["stress"]["peak_time"] == pytest.approx(1e-8)


def test_stress_over_the_limit_fails_with_negative_margin(tmp_path, monkeypatch):
    _stub_ngspice(monkeypatch, raw=_rawfile(_WAVE))
    decl = {
        "name": "vs",
        "stress": {"a": "d1", "from": "10n", "to": "100n"},
        "limits": {"max": 4.5},
    }

    report = sim.run_sim(str(_request(tmp_path, decl)))

    (measurement,) = report["corners"][0]["measurements"]
    assert measurement["status"] == "fail"
    assert measurement["margin"] < 0
    assert report["status"] == "fail"


def test_unresolved_node_is_a_measurement_error_without_a_stress_member(
    tmp_path, monkeypatch
):
    _stub_ngspice(monkeypatch, raw=None)
    decl = {"name": "vs", "stress": {"a": "nope"}}

    report = sim.run_sim(str(_request(tmp_path, decl)))

    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["status"] == "error"
    assert measurement["value"] is None
    assert "stress" not in measurement
    (diagnostic,) = [d for d in corner["diagnostics"] if d["code"] == "measurement"]
    assert "'nope'" in diagnostic["message"]


# --------------------------------------------------------------------------- #
# Integration: a real single-corner ngspice run
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_integration_windowed_peak_excludes_the_early_overshoot(tmp_path):
    # A pulse source overshoots to 3 V for the first 10 ns, then settles at 1 V.
    (tmp_path / "p.spice").write_text(
        "V1 a 0 PWL(0 3 10n 3 11n 1 200n 1)\nR1 a b 1k\nR2 b 0 1k\n"
    )
    request = _write_request(
        tmp_path,
        {
            "netlist": "p.spice",
            "analysis": {"kind": "tran", "args": "1n 100n"},
            "measurements": [
                {
                    "name": "settled",
                    "stress": {"a": "a", "b": "b", "from": "20n", "to": "90n"},
                    "limits": {"max": 0.6},
                },
                {"name": "whole", "stress": {"a": "a", "b": "b"}},
            ],
            "options": {"keep_artifacts": False},
        },
    )

    report = sim.run_sim(str(request))

    settled, whole = report["corners"][0]["measurements"]
    assert settled["value"] == pytest.approx(0.5, abs=1e-3)
    assert settled["status"] == "pass"
    assert whole["value"] == pytest.approx(1.5, abs=1e-3)
