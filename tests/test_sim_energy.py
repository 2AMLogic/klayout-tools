"""Tests for ``measurements[].energy`` -- a source's energy over a transient
window from one declaration (issue #3033).

Unit tests (validation, reduction, deck emission, grading through a stubbed
``ngspice``) always run; the integration tests run one real single-corner
``ngspice -b`` each and skip when ngspice is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_energy

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

_DECL = {
    "name": "e_vdd",
    "energy": {"source": "vdd", "voltage": "v(a)", "from": "10n", "to": "90n"},
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
# integrate_energy: sign convention, splitting, window edges
# --------------------------------------------------------------------------- #


def test_dc_source_into_resistor_delivers_v2_over_r_times_dt():
    # 1 V across 1 kohm: SPICE current into the + terminal is -1 mA.
    scale = [0.0, 1e-6, 2e-6, 3e-6]
    out = sim_energy.integrate_energy(scale, [1.0] * 4, [-1e-3] * 4)
    assert out.net == pytest.approx(1e-3 * 3e-6)
    assert out.gross == pytest.approx(out.net)
    assert out.recovered == 0.0
    assert out.net == pytest.approx(-1.0 * out.charge)


def test_source_that_absorbs_reports_recovered_energy():
    out = sim_energy.integrate_energy([0.0, 1.0], [2.0, 2.0], [0.5, 0.5])
    assert out.gross == 0.0
    assert out.recovered == pytest.approx(1.0)
    assert out.net == pytest.approx(-1.0)


def test_sign_change_mid_segment_is_split_at_the_zero():
    # p goes +1 -> -1 over one second: equal lobes of area 0.25.
    out = sim_energy.integrate_energy([0.0, 1.0], [1.0, 1.0], [-1.0, 1.0])
    assert out.gross == pytest.approx(0.25)
    assert out.recovered == pytest.approx(0.25)
    assert out.net == pytest.approx(0.0)


def test_window_edges_between_samples_are_interpolated():
    out = sim_energy.integrate_energy(
        [0.0, 1.0, 2.0], [1.0] * 3, [-1.0] * 3, start=0.25, stop=1.5
    )
    assert out.net == pytest.approx(1.25)
    assert (out.start, out.stop) == (0.25, 1.5)


def test_window_edge_exactly_on_a_sample():
    out = sim_energy.integrate_energy(
        [0.0, 1.0, 2.0], [1.0] * 3, [-1.0] * 3, start=1.0, stop=2.0
    )
    assert out.net == pytest.approx(1.0)


def test_window_inside_a_single_segment():
    out = sim_energy.integrate_energy(
        [0.0, 1.0], [1.0, 1.0], [-1.0, -1.0], start=0.2, stop=0.3
    )
    assert out.net == pytest.approx(0.1)


def test_window_outside_the_samples_is_an_error():
    out = sim_energy.integrate_energy([0.0, 1.0], [1.0] * 2, [1.0] * 2, stop=2.0)
    assert out.error and "outside the saved samples" in out.error


def test_single_sample_is_an_error():
    assert sim_energy.integrate_energy([0.0], [1.0], [1.0]).error


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("energy", "match"),
    [
        ({"voltage": "v(a)"}, "energy.source is required"),
        ({"source": "vd d", "voltage": "v(a)"}, "energy.source is required"),
        ({"source": "vdd"}, "energy.voltage is required"),
        ({"source": "vdd", "voltage": "v(a)\nquit"}, "single line"),
        ({"source": "vdd", "voltage": "v(a)", "from": "soon"}, "energy.from"),
        (
            {"source": "vdd", "voltage": "v(a)", "from": "2u", "to": "1u"},
            "must be after",
        ),
        ({"source": "vdd", "voltage": "v(a)", "from": 1, "to": 1}, "must be after"),
        ({"source": "vdd", "voltage": "v(a)", "split": True}, "unknown energy key"),
        ("vdd", "must be an object"),
    ],
)
def test_malformed_energy_declarations_are_refused(energy, match):
    with pytest.raises(sim.SimError, match=match):
        sim._validate_measurement_forms([{"name": "e", "energy": energy}])


@pytest.mark.parametrize(
    "other",
    [
        {"spice": ".meas tran t MAX v(a)"},
        {"expr": "v(a)"},
        {"crossings": {"signal": "v(a)", "level": 0.5}},
    ],
)
def test_energy_combined_with_another_form_is_refused(other):
    spec = {"name": "e", "energy": {"source": "vdd", "voltage": "v(a)"}, **other}
    with pytest.raises(sim.SimError, match="declare exactly one"):
        sim._validate_measurement_forms([spec])


@pytest.mark.parametrize("kind", ["op", "dc", "ac"])
def test_energy_requires_a_transient(tmp_path, kind):
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
def test_energy_window_outside_the_analysis_is_refused(window, args):
    spec = {"name": "e", "energy": {"source": "vdd", "voltage": "v(a)", **window}}
    with pytest.raises(sim.SimError, match="outside the transient analysis window"):
        sim_energy.validate_energy_context([spec], {"kind": "tran", "args": args})


def test_energy_window_inside_the_analysis_is_accepted():
    sim_energy.validate_energy_context([_DECL], {"kind": "tran", "args": "1n 100n"})


def test_energy_is_refused_for_xyce(tmp_path):
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
    with pytest.raises(sim.SimError, match="energy is not supported for engine 'xyce'"):
        sim.run_sim(str(request))


def test_energy_is_refused_with_analysis_steps(tmp_path):
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


def test_deck_dumps_voltage_and_current_after_the_analysis(tmp_path):
    lines = _deck(tmp_path, [_DECL])
    tran = lines.index("tran 1n 100n")
    assert lines.index("set filetype=ascii") < tran
    raw = tmp_path / "energy_0.raw"
    let_v = lines.index("let __klt_en_0_v = v(a)")
    let_i = lines.index("let __klt_en_0_i = i(vdd)")
    write = lines.index(f"write {raw} __klt_en_0_v __klt_en_0_i")
    assert tran < let_v < let_i < write < lines.index("quit")


def test_deck_adds_no_behavioural_power_node(tmp_path):
    lines = _deck(tmp_path, [_DECL])
    assert not any(line.lower().startswith("b") for line in lines)


def test_deck_without_energy_is_unchanged(tmp_path):
    lines = _deck(tmp_path, [{"name": "v", "spice": ".meas tran v MAX v(a)"}])
    assert not any("__klt_en" in line or "filetype" in line for line in lines)


# --------------------------------------------------------------------------- #
# run_sim with ngspice stubbed: grading and the response shape
# --------------------------------------------------------------------------- #


def _rawfile(points: list[tuple[float, float, float]]) -> str:
    lines = [
        "Title: stub",
        "Plotname: Transient Analysis",
        "Flags: real",
        "No. Variables: 3",
        f"No. Points: {len(points)}",
        "Variables:",
        "\t0\ttime\ttime",
        "\t1\tv(__klt_en_0_v)\tvoltage",
        "\t2\t__klt_en_0_i\tcurrent",
        "Values:",
    ]
    for index, (t, v, i) in enumerate(points):
        lines.append(f" {index}\t{t!r}")
        lines.append(f"\t{v!r}")
        lines.append(f"\t{i!r}")
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
            with open(os.path.join(cwd, "energy_0.raw"), "w", encoding="utf-8") as h:
                h.write(raw)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


_DC = [(t * 1e-8, 1.0, -1e-3) for t in range(11)]


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


def test_energy_only_request_carries_the_breakdown_and_is_checked(
    tmp_path, monkeypatch
):
    _stub_ngspice(monkeypatch, raw=_rawfile(_DC))
    decl = {"name": "e_vdd", "energy": {"source": "vdd", "voltage": "v(a)"}}

    report = sim.run_sim(str(_request(tmp_path, decl)))

    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["status"] == "pass"
    assert measurement["value"] == pytest.approx(1e-3 * 1e-7)
    energy = measurement["energy"]
    assert energy["net"] == measurement["value"]
    assert energy["gross"] == pytest.approx(energy["net"])
    assert energy["recovered"] == 0.0
    assert energy["net"] == pytest.approx(-1.0 * energy["charge"])
    assert (energy["from"], energy["to"]) == (0.0, pytest.approx(1e-7))
    assert report["status"] == "pass"
    assert report["coverage"]["nothing_checked"] is False


def test_energy_net_is_graded_against_limits(tmp_path, monkeypatch):
    _stub_ngspice(monkeypatch, raw=_rawfile(_DC))
    decl = {
        "name": "e_vdd",
        "energy": {"source": "vdd", "voltage": "v(a)"},
        "limits": {"max": 5e-11},
    }

    report = sim.run_sim(str(_request(tmp_path, decl)))

    (measurement,) = report["corners"][0]["measurements"]
    assert measurement["status"] == "fail"
    assert measurement["energy"]["net"] == pytest.approx(1e-10)


def test_unresolved_source_is_a_measurement_error_without_a_breakdown(
    tmp_path, monkeypatch
):
    _stub_ngspice(monkeypatch, raw=None)
    decl = {"name": "e_x", "energy": {"source": "vnope", "voltage": "v(a)"}}

    report = sim.run_sim(str(_request(tmp_path, decl)))

    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["status"] == "error"
    assert measurement["value"] is None
    assert "energy" not in measurement
    (diagnostic,) = [d for d in corner["diagnostics"] if d["code"] == "measurement"]
    assert "'vnope'" in diagnostic["message"]


# --------------------------------------------------------------------------- #
# Integration: real single-corner ngspice runs
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_integration_dc_source_into_resistor(tmp_path):
    (tmp_path / "rc.spice").write_text("Vdd a 0 DC 1\nR1 a 0 1k\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "rc.spice",
            "analysis": {"kind": "tran", "args": "1n 100n"},
            "measurements": [_DECL],
            "options": {"keep_artifacts": False},
        },
    )

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["diagnostics"] == []
    (measurement,) = corner["measurements"]
    energy = measurement["energy"]
    assert energy["net"] == pytest.approx(1e-3 * 80e-9, rel=1e-6)
    assert energy["recovered"] == pytest.approx(0.0, abs=1e-18)
    assert energy["net"] == pytest.approx(-1.0 * energy["charge"], rel=1e-6)


@_SKIP_NO_NGSPICE
def test_integration_capacitor_discharging_into_the_source_recovers_energy(tmp_path):
    # C1 starts at 2 V and discharges through R into a 1 V source: the
    # source absorbs charge, so it recovers energy and delivers none.
    (tmp_path / "dis.spice").write_text("Vs a 0 DC 1\nR1 a b 1k\nC1 b 0 1n IC=2\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "dis.spice",
            "analysis": {"kind": "tran", "args": "10n 500n uic"},
            "measurements": [
                {"name": "e_vs", "energy": {"source": "vs", "voltage": "v(a)"}}
            ],
        },
    )

    report = sim.run_sim(str(request))

    (measurement,) = report["corners"][0]["measurements"]
    energy = measurement["energy"]
    assert energy["recovered"] > 0
    assert energy["gross"] == pytest.approx(0.0, abs=1e-18)
    assert energy["net"] == pytest.approx(energy["gross"] - energy["recovered"])
    assert measurement["value"] == energy["net"] < 0


@_SKIP_NO_NGSPICE
def test_integration_unknown_source_does_not_starve_a_later_one(tmp_path):
    (tmp_path / "rc.spice").write_text("Vdd a 0 DC 1\nR1 a 0 1k\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "rc.spice",
            "analysis": {"kind": "tran", "args": "1n 100n"},
            "measurements": [
                {"name": "bad", "energy": {"source": "vnope", "voltage": "v(a)"}},
                {"name": "good", "energy": {"source": "vdd", "voltage": "v(a)"}},
            ],
        },
    )

    report = sim.run_sim(str(request))

    by_name = {m["name"]: m for m in report["corners"][0]["measurements"]}
    assert by_name["bad"]["status"] == "error"
    assert by_name["good"]["value"] == pytest.approx(1e-3 * 100e-9, rel=1e-6)
