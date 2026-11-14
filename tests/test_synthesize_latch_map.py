"""``request.latch_map`` (issue #4458): map inferred latches onto library cells."""

from __future__ import annotations

import json
import os

import pytest

from klayout_tools import synthesize
from klayout_tools.synthesize import SynthesizeError, _write_script
from test_synthesize import _REAL_SKY130_VARIANT
from test_synthesize_arithmetic import _synth_script_path, requires_engine

_POS = {"cell": "DLAT", "enable": "GATE", "data": "D", "q": "Q"}
_NEG = {"cell": "DLATN", "enable": "GATE_N", "data": "D", "q": "Q"}

#: The common open-source clock gate: a transparent-low latch plus an AND.
_CLOCK_GATE_RTL = """
module clk_gate (input clk, input en, output gclk);
  reg en_l;
  always @* if (!clk) en_l = en;
  assign gclk = clk & en_l;
endmodule
"""


def _request(latch_map, **extra):
    request = {"sources": ["a.v"], "hdl_toplevel": "t", "pdk": {}}
    if latch_map is not None:
        request["latch_map"] = latch_map
    request.update(extra)
    return request


def test_absent_latch_map_resolves_to_none():
    assert synthesize._resolve_latch_map(_request(None)) is None


def test_latch_map_is_an_allowed_request_key():
    assert "latch_map" in synthesize._ALLOWED_REQUEST_KEYS


def test_resolve_accepts_one_or_both_polarities():
    one = synthesize._resolve_latch_map(_request({"negative": _NEG}))
    assert one == {"negative": _NEG}
    both = synthesize._resolve_latch_map(_request({"positive": _POS, "negative": _NEG}))
    assert set(both) == {"positive", "negative"}


@pytest.mark.parametrize(
    "latch_map, fragment",
    [
        ([], "non-empty object"),
        ({}, "non-empty object"),
        ({"sideways": _POS}, "sideways"),
        ({"positive": "DLAT"}, "must be an object"),
        ({"positive": {**_POS, "extra": "x"}}, "extra"),
        (
            {"positive": {k: v for k, v in _POS.items() if k != "q"}},
            "latch_map.positive.q",
        ),
        ({"positive": {**_POS, "cell": ""}}, "latch_map.positive.cell"),
        ({"positive": {**_POS, "data": "a b"}}, "latch_map.positive.data"),
        ({"positive": {**_POS, "q": "D"}}, "distinct"),
    ],
)
def test_resolve_rejects_malformed_latch_map(latch_map, fragment):
    with pytest.raises(SynthesizeError) as excinfo:
        synthesize._resolve_latch_map(_request(latch_map))
    assert fragment in str(excinfo.value)


def test_unknown_cell_is_rejected(tmp_path):
    liberty = tmp_path / "t.lib"
    liberty.write_text("library (t) { cell (DLAT) { area : 1; } }\n")
    block = synthesize._validate_latch_map_against_liberty(
        {"positive": _POS}, str(liberty)
    )
    assert block["validated"] is True
    with pytest.raises(SynthesizeError, match="DLATN"):
        synthesize._validate_latch_map_against_liberty({"negative": _NEG}, str(liberty))


def test_techmap_text_maps_only_requested_polarities():
    text = synthesize._latch_techmap_text({"negative": _NEG})
    assert "module \\$_DLATCH_N_ (input E, input D, output Q);" in text
    assert "DLATN _TECHMAP_REPLACE_ (.GATE_N(E), .D(D), .Q(Q));" in text
    assert "$_DLATCH_P_" not in text


def _script_lines(tmp_path, **kwargs):
    script_path = tmp_path / "synth.ys"
    _write_script(
        script_path=str(script_path),
        sources=["/abs/design.v"],
        hdl_toplevel="top",
        liberty_path="/abs/lib.lib",
        stats_path="/abs/stats.json",
        netlist_path="/abs/out.v",
        **kwargs,
    )
    return script_path.read_text().splitlines()


def test_script_is_unchanged_without_latch_map(tmp_path):
    lines = _script_lines(tmp_path)
    assert not any("techmap" in line for line in lines)
    assert lines[3:5] == ["synth -top top", "dfflibmap -liberty /abs/lib.lib"]


def test_latch_techmap_runs_between_synth_and_dfflibmap(tmp_path):
    lines = _script_lines(tmp_path, latch_techmap_path="/abs/latch_map.v")
    assert lines.index("synth -top top") + 1 == lines.index(
        "techmap -map /abs/latch_map.v"
    )
    assert lines.index("techmap -map /abs/latch_map.v") + 1 == lines.index(
        "dfflibmap -liberty /abs/lib.lib"
    )


_LATCH_CELLS = {
    "positive": {
        "cell": "sky130_fd_sc_hd__dlxtp_1",
        "enable": "GATE",
        "data": "D",
        "q": "Q",
    },
    "negative": {
        "cell": "sky130_fd_sc_hd__dlxtn_1",
        "enable": "GATE_N",
        "data": "D",
        "q": "Q",
    },
}

requires_latch_engine = pytest.mark.skipif(
    _REAL_SKY130_VARIANT is None,
    reason="needs yosys and an installed sky130_fd_sc_hd liberty",
)


def _run(tmp_path, monkeypatch, latch_map, **extra):
    root, variant = _REAL_SKY130_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)
    (tmp_path / "clk_gate.v").write_text(_CLOCK_GATE_RTL)
    request = {
        "sources": ["clk_gate.v"],
        "hdl_toplevel": "clk_gate",
        "pdk": {"cell_library": "sky130_fd_sc_hd"},
    }
    if latch_map is not None:
        request["latch_map"] = latch_map
    request.update(extra)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    return synthesize.run_synthesize(str(request_path))


@requires_engine
@requires_latch_engine
def test_integration_clock_gate_maps_to_library_latch(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch, {"negative": _LATCH_CELLS["negative"]})
    counts = report["instance_counts_by_type"]
    assert not any("dlatch" in name.lower() for name in counts)
    assert counts["sky130_fd_sc_hd__dlxtn_1"] == 1
    assert report["structural"]["latches"] == 0
    assert report["structural"]["has_critical"] is False
    assert report["latch_map"]["validated"] is True
    script = _synth_script_path(tmp_path, report, "clk_gate")
    techmap = os.path.join(os.path.dirname(script), "clk_gate_latch_map.v")
    assert os.path.isfile(techmap)
    assert "techmap -map" in open(script).read()


@requires_engine
@requires_latch_engine
def test_integration_without_latch_map_response_has_no_latch_map(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch, None, structural={"expected_latches": 1})
    assert "latch_map" not in report
    assert any("dlatch" in n.lower() for n in report["instance_counts_by_type"])
    script = open(_synth_script_path(tmp_path, report, "clk_gate")).read()
    assert "techmap" not in script


@requires_engine
@requires_latch_engine
def test_integration_unmapped_polarity_stays_a_latch(tmp_path, monkeypatch):
    report = _run(
        tmp_path,
        monkeypatch,
        {"positive": _LATCH_CELLS["positive"]},
        structural={"expected_latches": 1},
    )
    assert report["structural"]["latches"] == 1


@requires_engine
@requires_latch_engine
def test_integration_unknown_library_cell_is_rejected(tmp_path, monkeypatch):
    bad = {"negative": {**_LATCH_CELLS["negative"], "cell": "no_such_latch"}}
    with pytest.raises(SynthesizeError, match="no_such_latch"):
        _run(tmp_path, monkeypatch, bad)
