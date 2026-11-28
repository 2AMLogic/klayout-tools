"""Supply waveform scaling in explicit corners and Monte Carlo samples."""

from __future__ import annotations

import pytest

from klayout_tools import sim
from test_sim import _boom, _scaled_request_doc, _write_request


def test_units_supply_waveforms_expand_per_unit():
    points = sim.expand_corners(
        {
            "units": [
                {"supply_v": {"vdd": 1.62}},
                {"supply_v": {"vdd": 1.98, "vref": 0.5}},
            ],
            "supply_waveforms": ["vdd"],
        },
        [],
    )
    assert [p.supply_waveforms for p in points] == [("vdd",), ("vdd",)]
    back = sim._corner_points_from_wire(sim._corner_points_to_wire(points))[1]
    assert back.supply_waveforms == ("vdd",)


def test_units_without_supply_waveforms_unchanged():
    (point,) = sim.expand_corners({"units": [{"supply_v": {"vdd": 1.8}}]}, [])
    assert point.supply_waveforms == ()


@pytest.mark.parametrize(
    "wf,match",
    [
        (["vref"], r"units\[1\]"),
        ("vdd", "unique"),
        (["vdd", "vdd"], "unique"),
        ([" "], "unique"),
    ],
)
def test_units_supply_waveforms_validation(wf, match):
    with pytest.raises(sim.SimError, match=match):
        sim.expand_corners(
            {
                "units": [
                    {"supply_v": {"vdd": 1.8, "vref": 0.5}},
                    {"supply_v": {"vdd": 1.7}},
                ],
                "supply_waveforms": wf,
            },
            [],
        )


class _SourceError(Exception):
    pass


def _find(text: str, name: str = "vdd"):
    from klayout_tools import sim_supply_waveform

    return sim_supply_waveform.find_source(
        text, name, option="corners.supply_waveforms", error_cls=_SourceError
    )


@pytest.mark.parametrize(
    "netlist",
    [
        "; just a comment\nvdd vdd 0 pwl(0 0 1n 1.8)\n.end\n",
        "title\n   ; indented comment only\nvdd vdd 0 pwl(0 0 1n 1.8)\n",
        "vdd vdd 0 pwl(0 0 1n 1.8)\n; trailing comment-only line\n",
        ".subckt cell a\n; comment inside subckt\n.ends\nvdd vdd 0 pwl(0 0 1n 1.8)\n",
        "$ dollar comment\nvdd vdd 0 pwl(0 0 1n 1.8)\n.end\n",
        "title\n   $ indented dollar comment\nvdd vdd 0 pwl(0 0 1n 1.8)\n",
    ],
)
def test_find_source_skips_comment_only_lines(netlist):
    found = _find(netlist)
    assert found["kind"] == "pwl"


def test_find_source_comment_only_netlist_raises_error_cls():
    with pytest.raises(_SourceError, match="vdd"):
        _find("; just a comment\n")


def _mc_supply_waveforms_request(tmp_path, body, scaled, name="request.json"):
    """One DC rail plus a 3.3 V-peak swept waveform rail requested at 2.5 V
    (issue #7069's repro shape): the peak differs from the requested rail
    voltage, so only the supply_waveforms listing can carry the override."""
    doc = _scaled_request_doc(
        tmp_path,
        body,
        {"vsup": [3.3], "vchop": [2.5]},
        scaled,
        {"monte_carlo": {"n": 5, "seed": 1, "vary": "mismatch"}},
    )
    return _write_request(tmp_path, doc, name)


@pytest.mark.parametrize(
    "decl",
    [
        "Vchop chop 0 PULSE(0 3.3 0 1n 1n 1u 2u)",
        "Vchop chop 0 PWL(0 0 1u 3.3)",
    ],
)
def test_supply_waveforms_dry_run_valid_with_monte_carlo(tmp_path, monkeypatch, decl):
    """#7069: the Monte Carlo fan-out used to drop each base corner's
    waveform tuple, so a listed rail was rejected as an unsupported
    ``supply_v`` override -- exactly as if ``supply_waveforms`` were
    absent -- the moment ``monte_carlo`` expanded the units."""
    monkeypatch.setattr(sim.subprocess, "run", _boom)
    monkeypatch.setattr(sim.subprocess, "Popen", _boom)
    body = f"{decl}\nVsup vdd 0 3.3\nR1 vdd 0 1k\nR2 chop 0 1k\n"
    mc = _mc_supply_waveforms_request(tmp_path, body, ["vchop"])
    base_doc = _scaled_request_doc(
        tmp_path,
        body,
        {"vsup": [3.3], "vchop": [2.5]},
        ["vchop"],
        {},
    )
    base = sim.run_sim(
        str(_write_request(tmp_path, base_doc, "base.json")), dry_run=True
    )
    assert base["status"] == "valid"
    assert base["unit_count"] == 1

    mc_report = sim.run_sim(str(mc), dry_run=True)
    assert mc_report["status"] == "valid"
    assert mc_report["unit_count"] == 5
    assert [u["sample_index"] for u in mc_report["units"]] == list(range(5))


def test_supply_waveforms_omitted_with_monte_carlo_still_rejected(
    tmp_path, monkeypatch
):
    """The exemption is the listing, not Monte Carlo: without
    ``supply_waveforms`` the waveform-rail override keeps failing closed
    even under ``monte_carlo``."""
    monkeypatch.setattr(sim.subprocess, "run", _boom)
    monkeypatch.setattr(sim.subprocess, "Popen", _boom)
    body = "Vchop chop 0 PULSE(0 3.3 0 1n 1n 1u 2u)\nVsup vdd 0 3.3\nR1 vdd 0 1k\n"
    path = _mc_supply_waveforms_request(tmp_path, body, [])
    with pytest.raises(sim.SimError) as exc:
        sim.run_sim(str(path), dry_run=True)
    message = str(exc.value)
    assert "'vchop'" in message and "not supported" in message
    assert "corners.supply_waveforms" in message


def test_supply_waveforms_each_mc_sample_retains_base_corner_rail():
    """Point-level check (#7069): every sample carries its base corner's
    waveform tuple and that corner's own rail voltage, for two requested
    waveform voltages."""
    bases = sim.expand_corners(
        {
            "supply_v": {"vsup": [3.3, 3.45], "vchop": [2.5, 3.0]},
            "supply_waveforms": ["vchop"],
            "temperature_c": [27],
        },
        [],
    )
    assert len(bases) == 2
    sampled, _ = sim._expand_monte_carlo(bases, {"n": 5, "seed": 1, "vary": "mismatch"})
    assert len(sampled) == 10
    for index, base in enumerate(bases):
        for sample in sampled[index * 5 : (index + 1) * 5]:
            assert sample.supply_waveforms == ("vchop",)
            assert sample.supply_v["vchop"] == base.supply_v["vchop"]
            assert sample.supply_v["vsup"] == base.supply_v["vsup"]
