"""Tests for the declarative timed ``stimulus`` request block (issue #2977)
and the ``environment.backend`` provenance block.

Unit tests (validation, PWL expansion, deck emission, collision/conflict
errors, backend provenance) always run; the integration test runs one real
single-corner ``ngspice -b`` and skips when ngspice is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim, sim_stimulus

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

_TRAN = {"kind": "tran", "args": "1n 10n"}


def _stim(**signal):
    base = {
        "source": "vclk",
        "node": "clk",
        "events": [{"t_s": 1e-9, "level": 1.8}],
    }
    base.update(signal)
    return {"stimulus": {"signals": [base]}}


def _resolve(request, analysis=_TRAN):
    return sim_stimulus.resolve_stimulus(request, analysis)


# --------------------------------------------------------------------------- #
# Expansion
# --------------------------------------------------------------------------- #


def test_no_stimulus_resolves_to_nothing():
    assert _resolve({}) == []


def test_valid_schedule_expands_to_expected_pwl_points():
    (sig,) = _resolve(
        _stim(
            initial=0.0,
            rise_s=1e-10,
            fall_s=2e-10,
            events=[
                {"t_s": 1e-9, "level": 1.8},
                {"t_s": 5e-9, "level": 0.0},
            ],
        )
    )
    assert sig["pwl"] == [
        [0.0, 0.0],
        [1e-9, 0.0],
        [1e-9 + 1e-10, 1.8],
        [5e-9, 1.8],
        [5e-9 + 2e-10, 0.0],
    ]
    assert [e["edge"] for e in sig["events"]] == ["rise", "fall"]


def test_skew_shifts_every_edge_and_is_echoed():
    (sig,) = _resolve(_stim(skew_s=2e-10, rise_s=1e-10))
    event = sig["events"][0]
    assert event["t_s"] == 1e-9
    assert event["t_applied_s"] == pytest.approx(1.2e-9)
    assert sig["skew_s"] == 2e-10
    assert sig["pwl"][-1] == [pytest.approx(1.3e-9), 1.8]


def test_zero_rise_time_is_applied_as_the_minimum_transition():
    (sig,) = _resolve(_stim(rise_s=0.0))
    assert sig["events"][0]["transition_s"] == sim_stimulus.MIN_TRANSITION_S
    times = [t for t, _ in sig["pwl"]]
    assert times == sorted(set(times))


def test_edge_at_t0_does_not_duplicate_the_initial_point():
    (sig,) = _resolve(_stim(events=[{"t_s": 0.0, "level": 1.8}], rise_s=1e-10))
    assert sig["pwl"] == [[0.0, 0.0], [1e-10, 1.8]]


def test_event_transition_overrides_signal_default():
    (sig,) = _resolve(
        _stim(rise_s=1e-10, events=[{"t_s": 1e-9, "level": 1.8, "transition_s": 5e-11}])
    )
    assert sig["events"][0]["transition_s"] == 5e-11


def test_simultaneous_edges_on_different_signals_are_independent():
    request = {
        "stimulus": {
            "signals": [
                {"source": "va", "node": "a", "events": [{"t_s": 1e-9, "level": 1}]},
                {"source": "vb", "node": "b", "events": [{"t_s": 1e-9, "level": 1}]},
            ]
        }
    }
    a, b = _resolve(request)
    assert a["pwl"] == b["pwl"]
    lines = sim_stimulus.deck_lines([a, b])
    assert lines[0].startswith("va a 0 PWL(") and lines[1].startswith("vb b 0 PWL(")


def test_deck_line_is_a_pwl_source_card():
    lines = sim_stimulus.deck_lines(_resolve(_stim(ref="vss")))
    assert len(lines) == 1
    assert lines[0].startswith("vclk clk vss PWL(") and lines[0].endswith(")")


def test_hold_event_adds_no_points():
    (sig,) = _resolve(
        _stim(
            events=[
                {"t_s": 1e-9, "level": 0.0},
                {"t_s": 2e-9, "level": 1.8},
            ]
        )
    )
    assert sig["events"][0]["edge"] == "hold"
    assert sig["pwl"][0] == [0.0, 0.0]
    assert len(sig["pwl"]) == 3


# --------------------------------------------------------------------------- #
# Validation: coded diagnostics
# --------------------------------------------------------------------------- #


def _code(request, analysis=_TRAN):
    with pytest.raises(sim_stimulus.StimulusError) as info:
        _resolve(request, analysis)
    return info.value.code


def test_non_monotonic_times_are_refused():
    request = _stim(events=[{"t_s": 2e-9, "level": 1}, {"t_s": 1e-9, "level": 0}])
    assert _code(request) == sim_stimulus.CODE_BAD_TIME


def test_negative_time_is_refused():
    assert _code(_stim(events=[{"t_s": -1e-9, "level": 1}])) == (
        sim_stimulus.CODE_BAD_TIME
    )


def test_skew_pushing_an_edge_negative_is_refused():
    assert _code(_stim(skew_s=-2e-9)) == sim_stimulus.CODE_BAD_TIME


def test_skew_pushing_an_edge_past_the_stop_time_is_refused():
    assert _code(_stim(skew_s=9.5e-9)) == sim_stimulus.CODE_AFTER_STOP


def test_overlapping_edges_are_refused():
    request = _stim(
        rise_s=2e-9,
        events=[{"t_s": 1e-9, "level": 1}, {"t_s": 2e-9, "level": 0}],
    )
    assert _code(request) == sim_stimulus.CODE_OVERLAP


@pytest.mark.parametrize(
    "mutation",
    [
        {"source": "clk_src"},  # not a V source
        {"source": 3},
        {"node": ""},
        {"events": []},
        {"bogus": 1},
        {"initial": "high"},
        {"rise_s": -1e-9},
    ],
)
def test_malformed_signal_is_refused(mutation):
    with pytest.raises(sim_stimulus.StimulusError):
        _resolve(_stim(**mutation))


def test_unknown_event_key_is_refused():
    with pytest.raises(sim_stimulus.StimulusError, match="unknown key"):
        _resolve(_stim(events=[{"t_s": 1e-9, "level": 1, "when": 2}]))


def test_duplicate_source_names_are_refused():
    request = _stim()
    request["stimulus"]["signals"].append(dict(request["stimulus"]["signals"][0]))
    with pytest.raises(sim_stimulus.StimulusError, match="twice"):
        _resolve(request)


def test_non_tran_analysis_is_refused():
    assert _code(_stim(), {"kind": "dc", "args": "v1 0 1 0.1"}) == (
        sim_stimulus.CODE_UNSUPPORTED
    )


def test_collision_with_a_netlist_source_is_an_explicit_error():
    signals = _resolve(_stim())
    with pytest.raises(sim_stimulus.StimulusError) as info:
        sim_stimulus.check_collisions(signals, {"vclk", "r1"})
    assert info.value.code == sim_stimulus.CODE_COLLISION


def test_supply_override_of_a_stimulus_source_is_refused():
    signals = _resolve(_stim())
    with pytest.raises(sim_stimulus.StimulusError) as info:
        sim_stimulus.check_supply_conflict(signals, {"vclk"})
    assert info.value.code == sim_stimulus.CODE_SUPPLY_CONFLICT


def test_sim_wrapper_raises_a_coded_sim_error(tmp_path):
    netlist = tmp_path / "body.spice"
    netlist.write_text("Vclk clk 0 0\nR1 clk 0 1k\n")
    with pytest.raises(sim.SimError) as info:
        sim._resolve_stimulus(_stim(), _TRAN, "ngspice", str(netlist), [])
    assert info.value.code == sim_stimulus.CODE_COLLISION


def test_xyce_engine_is_refused(tmp_path):
    netlist = tmp_path / "body.spice"
    netlist.write_text("R1 clk 0 1k\n")
    with pytest.raises(sim.SimError) as info:
        sim._resolve_stimulus(_stim(), _TRAN, "xyce", str(netlist), [])
    assert info.value.code == sim_stimulus.CODE_UNSUPPORTED


# --------------------------------------------------------------------------- #
# Deck emission and fingerprint
# --------------------------------------------------------------------------- #


def test_stimulus_lines_land_after_the_netlist_include(tmp_path):
    lines = sim_stimulus.deck_lines(_resolve(_stim()))
    point = sim.CornerPoint(process=None, supply_v={}, temperature_c=27)
    deck = tmp_path / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck),
        netlist_path="/n.cir",
        models_lib=None,
        point=point,
        analysis=_TRAN,
        measurements_spec=[],
        raw_path=None,
        stimulus_lines=lines,
    )
    text = deck.read_text()
    assert text.index(".include /n.cir") < text.index("vclk clk 0 PWL(")
    assert text.index("vclk clk 0 PWL(") < text.index(".temp")


# --------------------------------------------------------------------------- #
# Backend provenance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("flag", "request_backend", "env", "expected"),
    [
        ("local", "remote", "batch", "flag"),
        (None, "local", "batch", "request"),
        (None, None, "batch", "env"),
        (None, None, "", "default"),
    ],
)
def test_backend_source_precedence(monkeypatch, flag, request_backend, env, expected):
    monkeypatch.setenv(sim.BACKEND_ENV, env)
    request = {} if request_backend is None else {"backend": request_backend}
    assert sim._backend_source(flag, request) == expected


# --------------------------------------------------------------------------- #
# Integration (real ngspice, single corner)
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_integration_two_signal_schedule_drives_the_circuit_and_is_echoed(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)
    (tmp_path / "body.spice").write_text("Ra a 0 1k\nRb b 0 1k\n")
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "0.1n 10n"},
        "stimulus": {
            "signals": [
                {
                    "source": "va",
                    "node": "a",
                    "rise_s": 1e-10,
                    "events": [{"t_s": 2e-9, "level": 1.0}],
                },
                {
                    "source": "vb",
                    "node": "b",
                    "skew_s": 3e-9,
                    "rise_s": 1e-10,
                    "events": [{"t_s": 2e-9, "level": 1.0}],
                },
            ]
        },
        "measurements": [
            {"name": "a_late", "spice": ".meas tran a_late FIND v(a) AT=8n"},
            {"name": "b_early", "spice": ".meas tran b_early FIND v(b) AT=4n"},
            {"name": "b_late", "spice": ".meas tran b_late FIND v(b) AT=8n"},
        ],
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))

    report = sim.run_sim(str(path))

    (corner,) = report["corners"]
    assert corner["diagnostics"] == []
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert values["a_late"] == pytest.approx(1.0, abs=1e-6)
    assert values["b_early"] == pytest.approx(0.0, abs=1e-6)  # skewed past 4n
    assert values["b_late"] == pytest.approx(1.0, abs=1e-6)
    echo = report["environment"]["stimulus"]["signals"]
    assert [s["source"] for s in echo] == ["va", "vb"]
    assert echo[1]["events"][0]["t_applied_s"] == pytest.approx(5e-9)
    assert report["environment"]["backend"] == {
        "resolved": "local",
        "source": "default",
        "requested": "local",
    }
