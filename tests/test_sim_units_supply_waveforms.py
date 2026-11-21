"""``corners.supply_waveforms`` alongside ``corners.units`` (issue #4981)."""

from __future__ import annotations

import pytest

from klayout_tools import sim


def test_units_supply_waveforms_expand_per_unit():
    points = sim._expand_corners(
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
    (point,) = sim._expand_corners({"units": [{"supply_v": {"vdd": 1.8}}]}, [])
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
        sim._expand_corners(
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
