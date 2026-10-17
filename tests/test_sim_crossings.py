"""Tests for ``measurements[].crossings`` -- a threshold-crossing time series
from one declaration (issue #2868).

Unit tests (validation, interpolation, deck emission, grading through a
stubbed ``ngspice``) always run; the two integration tests run one real
single-corner ``ngspice -b`` each and skip when ngspice is not installed.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_crossings

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

_DECL = {
    "name": "t_out",
    "crossings": {"signal": "v(out)", "level": 0.5, "edge": "rise"},
}


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _write_body(tmp_path: Path) -> Path:
    path = tmp_path / "body.spice"
    path.write_text("V1 out 0 SIN(0.5 0.5 1e6)\nR1 out 0 1k\n")
    return path


# --------------------------------------------------------------------------- #
# find_crossings: linear interpolation, polarity, window, cap
# --------------------------------------------------------------------------- #


def _sine(freq: float, step: float, stop: float, *, offset=0.0, phase=0.0):
    count = int(round(stop / step)) + 1
    scale = [index * step for index in range(count)]
    values = [offset + math.sin(2 * math.pi * freq * t + phase) for t in scale]
    return scale, values


def test_sine_crossings_are_interpolated_far_below_the_timestep():
    """1 MHz sine sampled at 10 ns: a nearest-sample extractor would be off by
    up to 5 ns; linear interpolation of a sine near its zero crossing is off
    by well under a picosecond."""
    step = 10e-9
    scale, values = _sine(1e6, step, 10e-6, phase=0.3)
    outcome = sim_crossings.find_crossings(scale, values, 0.0, edge="rise")

    # sin(2*pi*f*t + 0.3) rises through 0 at t = (k - 0.3/(2*pi)) / f.
    expected = [(k - 0.3 / (2 * math.pi)) / 1e6 for k in range(1, 11)]
    assert outcome.total == len(expected)
    assert outcome.edges == ["rise"] * len(expected)
    worst = max(abs(a - b) for a, b in zip(outcome.times, expected, strict=True))
    assert worst < step / 1000


def test_ramp_crossing_is_exact():
    outcome = sim_crossings.find_crossings([0.0, 1.0, 2.0], [0.0, 1.0, 2.0], 0.25)
    assert outcome.times == [pytest.approx(0.25)]


def test_either_reports_both_polarities_in_time_order():
    scale = [0.0, 1.0, 2.0, 3.0, 4.0]
    values = [0.0, 1.0, 0.0, 1.0, 0.0]
    outcome = sim_crossings.find_crossings(scale, values, 0.5, edge="either")
    assert outcome.times == pytest.approx([0.5, 1.5, 2.5, 3.5])
    assert outcome.edges == ["rise", "fall", "rise", "fall"]
    fall = sim_crossings.find_crossings(scale, values, 0.5, edge="fall")
    assert fall.times == pytest.approx([1.5, 3.5])


def test_a_signal_starting_above_the_level_has_no_initial_rise():
    outcome = sim_crossings.find_crossings([0.0, 1.0, 2.0], [1.0, 0.0, 1.0], 0.5)
    assert outcome.times == pytest.approx([1.5])


def test_a_sample_exactly_on_the_level_counts_once():
    outcome = sim_crossings.find_crossings(
        [0.0, 1.0, 2.0, 3.0], [0.0, 0.5, 0.5, 1.0], 0.5
    )
    assert outcome.times == pytest.approx([1.0])


def test_level_never_reached_is_zero_crossings_not_an_error():
    outcome = sim_crossings.find_crossings([0.0, 1.0], [0.0, 0.1], 0.5)
    assert (outcome.total, outcome.times, outcome.truncated) == (0, [], False)
    assert outcome.error is None


def test_window_and_cap_are_honoured_and_truncation_is_reported():
    scale, values = _sine(1.0, 1e-3, 10.0)
    outcome = sim_crossings.find_crossings(
        scale, values, 0.0, edge="rise", start=2.5, stop=8.5, max_count=3
    )
    # Rising zero crossings at t = 3, 4, ..., 8 (six inside the window).
    assert outcome.total == 6
    assert outcome.times == pytest.approx([3.0, 4.0, 5.0], abs=1e-9)
    assert outcome.truncated is True


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("crossings", "match"),
    [
        ({"level": 0.5}, "crossings.signal is required"),
        ({"signal": "", "level": 0.5}, "crossings.signal is required"),
        ({"signal": "v(a)\nquit", "level": 0.5}, "single line"),
        ({"signal": "v(a)"}, "crossings.level is required"),
        ({"signal": "v(a)", "level": "half"}, "crossings.level"),
        ({"signal": "v(a)", "level": 0.5, "edge": "up"}, "crossings.edge"),
        ({"signal": "v(a)", "level": 0.5, "max": 0}, "crossings.max"),
        ({"signal": "v(a)", "level": 0.5, "max": True}, "crossings.max"),
        ({"signal": "v(a)", "level": 0.5, "from": "soon"}, "crossings.from"),
        ({"signal": "v(a)", "level": 0.5, "from": "2u", "to": "1u"}, "before"),
        ({"signal": "v(a)", "level": 0.5, "tmax": 1}, "unknown crossings key"),
        ("v(a)", "must be an object"),
    ],
)
def test_malformed_crossings_declarations_are_refused(crossings, match):
    with pytest.raises(sim.SimError, match=match):
        sim._validate_measurement_forms([{"name": "t", "crossings": crossings}])


@pytest.mark.parametrize(
    "other", [{"spice": ".meas tran t MAX v(a)"}, {"expr": "v(a)"}]
)
def test_crossings_combined_with_another_form_is_refused(other):
    spec = {"name": "t", "crossings": {"signal": "v(a)", "level": 0.5}, **other}
    with pytest.raises(sim.SimError, match="declare exactly one"):
        sim._validate_measurement_forms([spec])


def test_crossings_name_colliding_with_an_expr_is_refused():
    with pytest.raises(sim.SimError, match="declared more than once"):
        sim._validate_measurement_forms([_DECL, {"name": "T_OUT", "expr": "v(out)"}])


def test_crossings_window_accepts_numbers_and_spice_literals():
    sim._validate_measurement_forms(
        [
            {
                "name": "t",
                "crossings": {"signal": "v(a)", "level": 0, "from": 1e-8, "to": "5u"},
            }
        ]
    )


@pytest.mark.parametrize("kind", ["op", "dc", "ac"])
def test_crossings_requires_a_transient(tmp_path, kind):
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


def test_crossings_is_refused_for_xyce(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_DECL],
        },
    )
    with pytest.raises(
        sim.SimError, match="crossings is not supported for engine 'xyce'"
    ):
        sim.run_sim(str(request))


def test_crossings_is_refused_with_analysis_steps(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis_steps": [
                {
                    "name": "s1",
                    "analysis": {"kind": "tran", "args": "1n 1u"},
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


def _deck(tmp_path: Path, measurements_spec, raw_path=None) -> list[str]:
    deck_path = tmp_path / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=sim.CornerPoint(None, {}, 27),
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=measurements_spec,
        raw_path=raw_path,
    )
    return deck_path.read_text().splitlines()


def test_deck_dumps_each_crossings_signal_after_the_analysis(tmp_path):
    lines = _deck(
        tmp_path,
        [
            {"name": "vx", "expr": "v(out) * 2"},
            _DECL,
            {"name": "t2", "crossings": {"signal": "v(a) - v(b)", "level": 0}},
        ],
    )
    tran = lines.index("tran 1n 1u")
    assert lines.index("set filetype=ascii") < tran
    let0 = lines.index("let __klt_xing_0 = v(out)")
    write0 = lines.index(f"write {tmp_path / 'crossings_0.raw'} __klt_xing_0")
    let1 = lines.index("let __klt_xing_1 = v(a) - v(b)")
    assert tran < lines.index("print vx") < let0 < write0 < let1 < lines.index("quit")
    assert "save all" in lines


def test_deck_crossings_follow_the_waveform_write(tmp_path):
    raw = str(tmp_path / "waveform.raw")
    lines = _deck(tmp_path, [_DECL], raw_path=raw)
    assert lines.index(f"write {raw}") < lines.index("let __klt_xing_0 = v(out)")
    assert lines.count("set filetype=ascii") == 1


def test_deck_without_crossings_is_unchanged(tmp_path):
    lines = _deck(tmp_path, [{"name": "v", "spice": ".meas tran v MAX v(out)"}])
    assert not any("__klt_xing" in line or "filetype" in line for line in lines)


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
        "\t1\tv(__klt_xing_0)\tvoltage",
        "Values:",
    ]
    for index, (t, v) in enumerate(points):
        lines.append(f" {index}\t{t!r}")
        lines.append(f"\t{v!r}")
    return "\n".join(lines) + "\n"


def _stub_ngspice(monkeypatch, *, raw: str | None):
    """Stand in for `ngspice -b`: write the log, and the crossings rawfile the
    deck's `write` would have produced. Binary resolution is stubbed to the
    bare name so no real ngspice is needed (cf. tests/test_sim.py)."""
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
            with open(os.path.join(cwd, "crossings_0.raw"), "w", encoding="utf-8") as h:
                h.write(raw)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


_TRIANGLE = [(0.0, 0.0), (1e-9, 1.0), (2e-9, 0.0), (3e-9, 1.0), (4e-9, 0.0)]


def test_crossings_only_request_is_checked_and_carries_the_series(
    tmp_path, monkeypatch
):
    """A request whose only measurement is `crossings` is graded `pass` and
    counted as checked -- not `not_checked` / exit 4 (cf. #3158)."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 4n"},
            "measurements": [
                {
                    "name": "t_out",
                    "crossings": {
                        "signal": "v(out)",
                        "level": 0.5,
                        "edge": "either",
                        "max": 3,
                    },
                }
            ],
            "options": {"keep_artifacts": False},
        },
    )
    _stub_ngspice(monkeypatch, raw=_rawfile(_TRIANGLE))

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    assert report["coverage"]["nothing_checked"] is False
    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["value"] == 4
    assert measurement["status"] == "pass"
    assert measurement["crossings"]["times"] == pytest.approx([0.5e-9, 1.5e-9, 2.5e-9])
    assert measurement["crossings"]["edges"] == ["rise", "fall", "rise"]
    assert measurement["crossings"]["truncated"] is True
    assert corner["diagnostics"] == []
    # keep_artifacts: false -- the corner's scratch directory is gone, and the
    # report still carries the whole series.
    assert corner["artifacts"]["raw"] is None


def test_crossings_count_is_graded_against_limits(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 4n"},
            "measurements": [{**_DECL, "limits": {"min": 5}}],
        },
    )
    _stub_ngspice(monkeypatch, raw=_rawfile(_TRIANGLE))

    report = sim.run_sim(str(request))

    (measurement,) = report["corners"][0]["measurements"]
    assert (measurement["value"], measurement["status"], measurement["margin"]) == (
        2,
        "fail",
        -3,
    )
    assert measurement["crossings"]["times"] == pytest.approx([0.5e-9, 2.5e-9])
    assert report["status"] == "fail"


def test_unresolved_signal_is_a_measurement_error_without_a_series(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 4n"},
            "measurements": [_DECL],
        },
    )
    _stub_ngspice(monkeypatch, raw=None)

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["status"] == "error"
    assert measurement["value"] is None
    assert "crossings" not in measurement
    (diagnostic,) = corner["diagnostics"]
    assert diagnostic["code"] == "measurement"
    assert "did not resolve" in diagnostic["message"]


# --------------------------------------------------------------------------- #
# Integration: one real single-corner ngspice run each
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_integration_one_declaration_matches_per_edge_meas_cards(tmp_path):
    """The whole point: one `crossings` declaration reproduces what a stack
    of `.meas ... WHEN v(x)=<thr> RISE=<k>` cards measures, edge by edge."""
    body = tmp_path / "osc.spice"
    body.write_text("V1 a 0 SIN(0.9 0.9 1e6)\nR1 a b 1k\nC1 b 0 100f\n")
    edges = 4
    cards = [
        {"name": f"r{k}", "spice": f".meas tran r{k} WHEN v(b)=0.9 RISE={k}"}
        for k in range(1, edges + 1)
    ]
    request = _write_request(
        tmp_path,
        {
            "netlist": "osc.spice",
            "analysis": {"kind": "tran", "args": "5n 4.6u"},
            "measurements": [
                {"name": "t_b", "crossings": {"signal": "v(b)", "level": 0.9}},
                *cards,
            ],
            "options": {"keep_artifacts": False},
        },
    )

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["diagnostics"] == []
    by_name = {m["name"]: m for m in corner["measurements"]}
    series = by_name["t_b"]["crossings"]["times"]
    assert by_name["t_b"]["value"] == len(series) == edges
    for k in range(1, edges + 1):
        assert series[k - 1] == pytest.approx(by_name[f"r{k}"]["value"], abs=1e-12)


@_SKIP_NO_NGSPICE
def test_integration_unresolved_signal_does_not_starve_a_later_one(tmp_path):
    body = tmp_path / "osc.spice"
    body.write_text("V1 a 0 SIN(0.9 0.9 1e6)\nR1 a 0 1k\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "osc.spice",
            "analysis": {"kind": "tran", "args": "5n 3u"},
            "measurements": [
                {"name": "bad", "crossings": {"signal": "v(nope)", "level": 0.9}},
                {
                    "name": "good",
                    "crossings": {"signal": "v(a)", "level": 0.9, "edge": "fall"},
                },
            ],
        },
    )

    report = sim.run_sim(str(request))

    by_name = {m["name"]: m for m in report["corners"][0]["measurements"]}
    assert by_name["bad"]["status"] == "error"
    assert by_name["good"]["value"] == 3
    assert by_name["good"]["crossings"]["times"] == pytest.approx(
        [0.5e-6, 1.5e-6, 2.5e-6], abs=1e-12
    )
