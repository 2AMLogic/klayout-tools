"""Tests for `monte_carlo.injected` design-owned mismatch (issue #2911,
`src/klayout_tools/sim_injected.py`). The ngspice subprocess is stubbed."""

from __future__ import annotations

import json
import statistics
import subprocess
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim, sim_injected

BODY = (
    ".param vdd=1.0\nVdd vdd 0 DC {vdd}\n"
    "XR1 vdd out rh m='mc_inj_xr1_m'\n"
    "XR2 out 0 rh m='mc_inj_xr2_m'\n"
    "C1 out 0 1n\n"
)


def _stub(monkeypatch, decks: list[str] | None = None):
    real_run = subprocess.run

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        if "-o" not in cmd:  # git etc. from provenance probes
            return real_run(
                cmd, capture_output=capture_output, text=text, timeout=timeout, cwd=cwd
            )
        log_path = cmd[cmd.index("-o") + 1]
        if decks is not None:
            decks.append(Path(cmd[cmd.index("-b") + 1]).read_text())
        Path(log_path).write_text("")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _request(tmp_path: Path, mc: dict, body: str = BODY) -> str:
    (tmp_path / "body.spice").write_text(body)
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "monte_carlo": mc,
                "analysis": {"kind": "tran", "args": "1n 1u"},
            }
        )
    )
    return str(path)


def _mc(**extra) -> dict:
    return {
        "n": 6,
        "seed": 11,
        "vary": "mismatch",
        "injected": [
            {"instance": "XR1", "parameter": "m", "relative_sigma": 0.02},
            {"instance": "XR2", "parameter": "m", "relative_sigma": 0.02},
        ],
        **extra,
    }


@pytest.mark.parametrize(
    "injected, match",
    [
        ([], "non-empty"),
        (["x"], r"injected\[0\] must be an object"),
        (
            [{"instance": "XR1", "parameter": "m", "relative_sigma": 0.1, "x": 1}],
            "unknown",
        ),
        ([{"parameter": "m", "relative_sigma": 0.1}], "exactly one"),
        (
            [
                {
                    "instance": "XR1",
                    "pattern": "x*",
                    "parameter": "m",
                    "relative_sigma": 0.1,
                }
            ],
            "exactly one",
        ),
        (
            [{"instance": "XR1", "parameter": "m", "relative_sigma": 0}],
            "relative_sigma",
        ),
        (
            [{"instance": "XR1", "parameter": "m", "relative_sigma": -1}],
            "relative_sigma",
        ),
        (
            [
                {
                    "instance": "XR1",
                    "parameter": "m",
                    "relative_sigma": 0.1,
                    "distribution": "x",
                }
            ],
            "distribution",
        ),
        (
            [{"instance": "NOPE", "parameter": "m", "relative_sigma": 0.1}],
            "matches no instance",
        ),
        (
            [{"pattern": "zz*", "parameter": "m", "relative_sigma": 0.1}],
            "matches no instance",
        ),
        (
            [{"instance": "C1", "parameter": "c", "relative_sigma": 0.1}],
            "never references",
        ),
    ],
)
def test_injected_validation_errors(tmp_path, injected, match):
    mc = {"n": 2, "seed": 1, "vary": "mismatch", "injected": injected}
    with pytest.raises(sim.SimError, match=match):
        sim.run_sim(_request(tmp_path, mc))


def test_injected_scale_requires_injected_and_nonnegative(tmp_path):
    with pytest.raises(sim.SimError, match="requires"):
        sim.run_sim(
            _request(
                tmp_path, {"n": 2, "seed": 1, "vary": "mismatch", "injected_scale": 2}
            )
        )
    with pytest.raises(sim.SimError, match="injected_scale"):
        sim.run_sim(_request(tmp_path, _mc(injected_scale=-1)))


def test_pattern_expands_to_matching_instances(tmp_path, monkeypatch):
    _stub(monkeypatch)
    mc = {
        "n": 2,
        "seed": 1,
        "vary": "mismatch",
        "injected": [{"pattern": "XR*", "parameter": "m", "relative_sigma": 0.02}],
    }
    report = sim.run_sim(_request(tmp_path, mc))
    drawn = report["corners"][0]["monte_carlo"]["injected"]
    assert [d["instance"] for d in drawn] == ["xr1", "xr2"]


def test_report_records_draws_and_family(tmp_path, monkeypatch):
    _stub(monkeypatch)
    report = sim.run_sim(_request(tmp_path, _mc()))
    for corner in report["corners"]:
        for item in corner["monte_carlo"]["injected"]:
            assert set(item) == {
                "instance",
                "parameter",
                "draw",
                "relative_delta",
                "value",
            }
            assert item["value"] == pytest.approx(1 + item["relative_delta"])
    (entry,) = [
        f
        for f in report["environment"]["monte_carlo"]["family_mismatch"]
        if f.get("source") == "injected"
    ]
    assert entry["active"] is True
    assert entry["relative_sigma"] == 0.02
    assert entry["instances"] == ["xr1", "xr2"]
    assert report["environment"]["monte_carlo"]["injected_scale"] == 1.0


def test_draws_are_deterministic_and_independent_of_n(tmp_path, monkeypatch):
    _stub(monkeypatch)
    a = sim.run_sim(_request(tmp_path, _mc()))
    b = sim.run_sim(_request(tmp_path, _mc()))
    big = sim.run_sim(_request(tmp_path, {**_mc(), "n": 20}))

    def draws(report):
        return [c["monte_carlo"]["injected"] for c in report["corners"]]

    assert draws(a) == draws(b)
    assert draws(big)[:6] == draws(a)
    assert len({d[0]["draw"] for d in draws(a)}) == 6  # varies per sample


def test_scale_zero_is_bit_identical_to_no_injection_and_scale_widens(
    tmp_path, monkeypatch
):
    off = []
    _stub(monkeypatch, off)
    report0 = sim.run_sim(_request(tmp_path, _mc(injected_scale=0)))
    assert all(
        i["relative_delta"] == 0 and i["value"] == 1.0
        for c in report0["corners"]
        for i in c["monte_carlo"]["injected"]
    )
    assert any(".param mc_inj_xr1_m=1.0" in deck for deck in off)

    def spread(scale):
        report = sim.run_sim(
            _request(tmp_path, {**_mc(injected_scale=scale), "n": 200})
        )
        return statistics.pstdev(
            c["monte_carlo"]["injected"][0]["relative_delta"] for c in report["corners"]
        )

    s1, s3 = spread(1), spread(3)
    assert s1 == pytest.approx(0.02, rel=0.25)
    assert s3 == pytest.approx(3 * s1, rel=1e-9)


def test_vary_process_pins_draws_and_reports_inactive(tmp_path, monkeypatch):
    _stub(monkeypatch)
    report = sim.run_sim(_request(tmp_path, {**_mc(), "vary": "process"}))
    assert (
        len({c["monte_carlo"]["injected"][0]["draw"] for c in report["corners"]}) == 1
    )
    (entry,) = report["environment"]["monte_carlo"]["family_mismatch"]
    assert entry["source"] == "injected" and entry["active"] is False


def test_deck_carries_params(tmp_path, monkeypatch):
    decks: list[str] = []
    _stub(monkeypatch, decks)
    sim.run_sim(_request(tmp_path, _mc()))
    assert decks
    assert ".param mc_inj_scale=1.0" in decks[0]
    assert ".param mc_inj_xr1_m_delta=" in decks[0]
    assert ".param mc_inj_xr2_m=" in decks[0]


def test_uniform_distribution_is_bounded():
    resolved = [
        {
            "instance": "r1",
            "parameter": "m",
            "relative_sigma": 0.1,
            "distribution": "uniform",
            "param": "mc_inj_r1_m",
            "card": "r1",
        }
    ]
    zs = [sim_injected.draw(resolved, s, 1.0)[0]["draw"] for s in range(200)]
    assert max(abs(z) for z in zs) <= 3**0.5
    assert statistics.pstdev(zs) == pytest.approx(1.0, rel=0.15)


def test_injected_points_round_trip_the_fleet_wire(tmp_path, monkeypatch):
    _stub(monkeypatch)
    resolved = [
        {
            "instance": "r1",
            "parameter": "m",
            "relative_sigma": 0.1,
            "distribution": "gaussian",
            "param": "mc_inj_r1_m",
            "card": "r1",
        }
    ]
    points, _ = sim._expand_monte_carlo(
        [sim.CornerPoint(None, {}, 27.0)],
        {"n": 2, "seed": 3, "vary": "mismatch"},
        (resolved, 1.0),
    )
    back = sim._corner_points_from_wire(sim._corner_points_to_wire(points))
    assert [p.mc_injected for p in back] == [p.mc_injected for p in points]
