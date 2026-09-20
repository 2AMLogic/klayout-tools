"""Tests for `klt synthesize`'s `arithmetic` request field (issue #1722).

Two tiers, matching `tests/test_synthesize.py`'s own convention:

- **Pure unit tests** (always run): request validation, the selection rule,
  the Yosys parameter decoder, the generated `.ys` script's pass ordering,
  and the delay-source preference. None of these needs Yosys.
- **Real-Yosys + real-liberty integration** (`@pytest.mark.skipif` when
  either is unavailable): the `$add` width probe, the `auto` sweep, and an
  explicitly-requested architecture, each end to end. Never required for CI
  -- they skip cleanly.
"""

from __future__ import annotations

import json
import os
import re
import shutil

import pytest

from klayout_tools import pdk as pdk_module
from klayout_tools import synthesize
from klayout_tools.synthesize import (
    DEFAULT_ADDER_LABEL,
    DEFAULT_ADDER_MIN_WIDTH,
    SynthesizeError,
    _candidate_measurement,
    _EngineOptions,
    _measure_candidate,
    _parse_yosys_param_int,
    _resolve_arithmetic,
    _select_arithmetic_candidate,
    _write_script,
    run_synthesize,
)

HAVE_YOSYS = shutil.which("yosys") is not None


# --------------------------------------------------------------------------- #
# request.arithmetic validation
# --------------------------------------------------------------------------- #


def test_absent_arithmetic_field_is_the_unchanged_default():
    assert _resolve_arithmetic({"sources": ["a.v"]}) is None


def test_arithmetic_auto_fills_in_the_documented_defaults():
    config = _resolve_arithmetic({"arithmetic": {"adders": "auto"}})
    assert config == {
        "adders": "auto",
        "min_width": DEFAULT_ADDER_MIN_WIDTH,
        "candidates": list(synthesize.ADDER_ARCHITECTURES),
        "verify_adders": True,
    }


def test_arithmetic_accepts_an_explicit_architecture_in_either_spelling():
    assert _resolve_arithmetic({"arithmetic": {"adders": "brent_kung"}})["adders"] == (
        "brent-kung"
    )
    assert _resolve_arithmetic({"arithmetic": {"adders": "kogge-stone"}})["adders"] == (
        "kogge-stone"
    )


def test_arithmetic_default_is_an_accepted_no_op_value():
    assert _resolve_arithmetic({"arithmetic": {"adders": "default"}})["adders"] == (
        DEFAULT_ADDER_LABEL
    )


@pytest.mark.parametrize(
    ("arithmetic", "fragment"),
    [
        ("not-an-object", "must be a JSON object"),
        ({}, "adders is required"),
        ({"adders": 16}, "must be a non-empty string"),
        ({"adders": "ladner-fischer"}, "unknown adder architecture"),
        ({"adders": "auto", "min_width": 1}, "min_width must be an integer >= 2"),
        ({"adders": "auto", "min_width": "8"}, "min_width must be an integer >= 2"),
        ({"adders": "auto", "candidates": []}, "non-empty array"),
        ({"adders": "auto", "candidates": ["nope"]}, "unknown adder architecture"),
        (
            {"adders": "sklansky", "candidates": ["ripple"]},
            "only applies to",
        ),
        ({"adders": "auto", "verify_adders": "yes"}, "must be a boolean"),
    ],
)
def test_arithmetic_rejects_malformed_requests(arithmetic, fragment):
    with pytest.raises(SynthesizeError) as excinfo:
        _resolve_arithmetic({"arithmetic": arithmetic})
    assert fragment in str(excinfo.value)


def test_arithmetic_candidates_are_deduplicated_and_normalised():
    config = _resolve_arithmetic(
        {
            "arithmetic": {
                "adders": "auto",
                "candidates": ["ripple", "brent_kung", "ripple"],
            }
        }
    )
    assert config["candidates"] == ["ripple", "brent-kung"]


# --------------------------------------------------------------------------- #
# Selection rule
# --------------------------------------------------------------------------- #


def _row(architecture, *, area, delay, disqualified=None, measured=True):
    return {
        "architecture": architecture,
        "prefix_cells": None,
        "logic_levels": None,
        "max_fanout": None,
        "adder_equivalence": [],
        "disqualified_reason": disqualified,
        "measured": (
            {
                "instance_count": 100,
                "area_um2": area,
                "delay_ns": delay,
                "delay_source": "abc_stime" if delay is not None else None,
                "meets_constraint": None,
            }
            if measured
            else None
        ),
    }


def test_selection_prefers_least_area_among_candidates_meeting_the_target():
    rows = [
        _row(DEFAULT_ADDER_LABEL, area=100.0, delay=12.0),
        _row("ripple", area=95.0, delay=11.0),
        _row("sklansky", area=90.0, delay=9.5),
        _row("kogge-stone", area=99.0, delay=8.0),
    ]
    winner, reason = _select_arithmetic_candidate(rows, 10.0)
    assert winner["architecture"] == "sklansky"
    assert reason is None


def test_selection_explains_itself_when_nothing_meets_the_target():
    rows = [
        _row(DEFAULT_ADDER_LABEL, area=100.0, delay=12.0),
        _row("kogge-stone", area=110.0, delay=11.0),
    ]
    winner, reason = _select_arithmetic_candidate(rows, 5.0)
    assert winner["architecture"] == "kogge-stone"
    assert "no candidate met constraints.clock_period_ns=5.0 ns" in reason
    assert "kogge-stone" in reason


def test_selection_without_a_target_picks_the_fastest():
    rows = [
        _row(DEFAULT_ADDER_LABEL, area=50.0, delay=12.0),
        _row("sklansky", area=90.0, delay=9.0),
    ]
    winner, reason = _select_arithmetic_candidate(rows, None)
    assert winner["architecture"] == "sklansky"
    assert reason is None


def test_selection_falls_back_to_area_when_no_delay_was_measured():
    rows = [
        _row(DEFAULT_ADDER_LABEL, area=100.0, delay=None),
        _row("brent-kung", area=90.0, delay=None),
    ]
    winner, reason = _select_arithmetic_candidate(rows, 10.0)
    assert winner["architecture"] == "brent-kung"
    assert "no candidate produced a delay measurement" in reason


def test_selection_skips_disqualified_and_unmeasurable_candidates():
    rows = [
        _row("ripple", area=10.0, delay=1.0, disqualified="not proven equivalent"),
        _row("sklansky", area=20.0, delay=2.0, measured=False),
        _row(DEFAULT_ADDER_LABEL, area=30.0, delay=3.0),
    ]
    winner, _ = _select_arithmetic_candidate(rows, None)
    assert winner["architecture"] == DEFAULT_ADDER_LABEL


def test_selection_returns_none_when_every_candidate_is_disqualified():
    rows = [_row("ripple", area=10.0, delay=1.0, disqualified="counterexample")]
    winner, reason = _select_arithmetic_candidate(rows, None)
    assert winner is None
    assert "disqualified" in reason


# --------------------------------------------------------------------------- #
# Measurement normalisation
# --------------------------------------------------------------------------- #


def test_measurement_prefers_the_native_sta_worst_path():
    measurement = _candidate_measurement(
        instance_count=10,
        area_um2=1.5,
        sta={"worst_path": {"delay_ns": 4.0}},
        abc_timing={"critical_path_ps": 9000.0},
        target_period_ns=5.0,
    )
    assert measurement["delay_ns"] == 4.0
    assert measurement["delay_source"] == "sta"
    assert measurement["meets_constraint"] is True


def test_measurement_falls_back_to_abc_stime():
    measurement = _candidate_measurement(
        instance_count=10,
        area_um2=1.5,
        sta=None,
        abc_timing={"critical_path_ps": 9000.0},
        target_period_ns=5.0,
    )
    assert measurement["delay_ns"] == 9.0
    assert measurement["delay_source"] == "abc_stime"
    assert measurement["meets_constraint"] is False


def test_measurement_never_fabricates_a_delay():
    measurement = _candidate_measurement(
        instance_count=10,
        area_um2=1.5,
        sta=None,
        abc_timing=None,
        target_period_ns=5.0,
    )
    assert measurement["delay_ns"] is None
    assert measurement["delay_source"] is None
    assert measurement["meets_constraint"] is None


# --------------------------------------------------------------------------- #
# Yosys parameter decoding
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (18, 18),
        ("00000000000000000000000000010010", 18),
        ("0", 0),
        ("xxxx", None),
        (True, None),
        (None, None),
        ([], None),
    ],
)
def test_parse_yosys_param_int(value, expected):
    assert _parse_yosys_param_int(value) == expected


# --------------------------------------------------------------------------- #
# One candidate's trial synthesis timing out (issue #1775): must disqualify
# only that candidate, never abort the rest of the `"auto"` sweep.
# --------------------------------------------------------------------------- #


def test_measure_candidate_disqualifies_on_yosys_timeout(tmp_path, monkeypatch):
    """A hung trial-synthesis `yosys -s` run raises `SynthesizeError` inside
    `_run_yosys` (issue #1775) -- `_measure_candidate` already catches any
    `SynthesizeError` from a failed trial and reports `None` (disqualified,
    not fatal), exactly as it does for a Yosys/ABC elaboration error."""

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["yosys", "-s"]
        raise synthesize.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    engine_options = _EngineOptions(
        liberty_path="/abs/lib.lib",
        cell_library="sky130_fd_sc_hd",
        delay_target_ps=None,
        dont_use_globs=(),
        tie_cells=None,
        constr_inputs=None,
    )
    result = _measure_candidate(
        label="kogge-stone",
        trial_dir=str(tmp_path / "trial"),
        resolved_sources=["/abs/design.v"],
        hdl_toplevel="top",
        engine_options=engine_options,
        adder_sources=(),
        adder_techmap_path=None,
    )
    assert result is None


# --------------------------------------------------------------------------- #
# Generated script: pass ordering
# --------------------------------------------------------------------------- #


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


def test_script_is_unchanged_without_the_arithmetic_field(tmp_path):
    lines = _script_lines(tmp_path)
    assert lines[:3] == [
        "read_verilog /abs/design.v",
        "hierarchy -check -top top",
        "synth -top top",
    ]
    assert not any("techmap -map" in line for line in lines)


def test_adder_techmap_runs_between_hierarchy_and_synth(tmp_path):
    """Position is load-bearing: after `hierarchy` (which would otherwise
    prune the freshly-read adder modules as unused) and before `synth`
    (whose `alumacc` step rewrites every `$add` into `$alu`)."""
    lines = _script_lines(
        tmp_path,
        adder_sources=("/abs/adder16.v", "/abs/adder18.v"),
        adder_techmap_path="/abs/add_techmap.v",
    )
    assert lines.index("hierarchy -check -top top") < lines.index("proc")
    assert lines.index("proc") < lines.index("read_verilog /abs/adder16.v")
    assert lines.index("read_verilog /abs/adder18.v") < lines.index(
        "techmap -map /abs/add_techmap.v"
    )
    assert lines.index("techmap -map /abs/add_techmap.v") < lines.index(
        "synth -top top"
    )


# --------------------------------------------------------------------------- #
# Integration: real Yosys + a real, host-resolved standard-cell liberty
# --------------------------------------------------------------------------- #


def _find_any_cell_library() -> tuple[str, str, str] | None:
    """Find any installed PDK variant shipping a standard-cell liberty.

    Unlike `tests/test_synthesize.py`'s sky130-specific helper, nothing here
    depends on a particular library's cells -- the assertions are about the
    *sweep* (a table of candidates, a selection, a proof), not about a
    specific area number -- so any `*_fd_sc_*` library with a `.lib` view
    will do. Returns `(root, variant, library)` or `None`.
    """
    try:
        result = pdk_module.list_pdks()
    except Exception:
        return None
    for install in result["installs"]:
        for variant in install["variants"]:
            libs_ref = os.path.join(install["root"], variant["name"], "libs.ref")
            if not os.path.isdir(libs_ref):
                continue
            for name in sorted(os.listdir(libs_ref)):
                if "_fd_sc_" not in name:
                    continue
                lib_dir = os.path.join(libs_ref, name, "lib")
                if not os.path.isdir(lib_dir):
                    continue
                if any(entry.endswith(".lib") for entry in os.listdir(lib_dir)):
                    return install["root"], variant["name"], name
    return None


_ANY_CELL_LIBRARY = _find_any_cell_library()

requires_engine = pytest.mark.skipif(
    not HAVE_YOSYS or _ANY_CELL_LIBRARY is None,
    reason="needs a yosys binary and a host-resolved standard-cell liberty",
)

#: A purely combinational design with one wide (16-bit) and one narrow
#: (4-bit) `+`, so a single run exercises both the substitution and the
#: `min_width` fail-over -- and, being combinational, can be gated by
#: `klt equiv` end to end.
_ADDER_RTL = """
module adders (
    input  [15:0] wide_a,
    input  [15:0] wide_b,
    input  [3:0]  narrow_a,
    input  [3:0]  narrow_b,
    output [15:0] wide_y,
    output [3:0]  narrow_y
);
  assign wide_y = wide_a + wide_b;
  assign narrow_y = narrow_a + narrow_b;
endmodule
"""


def _write_request(tmp_path, arithmetic, **extra):
    (tmp_path / "adders.v").write_text(_ADDER_RTL)
    request = {
        "schema": "klt.synthesize.request/1",
        "sources": ["adders.v"],
        "hdl_toplevel": "adders",
        "pdk": {"cell_library": _ANY_CELL_LIBRARY[2]},
    }
    if arithmetic is not None:
        request["arithmetic"] = arithmetic
    request.update(extra)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request, indent=2))
    return str(request_path)


def _synth_script_path(tmp_path, report, hdl_toplevel: str = "adders") -> str:
    """The real absolute path `run_synthesize` wrote its `.ys` script to.

    Issue #1844 normalized the response's own `script_path` field to the
    `{path, scope}` shape (and `tmp_path` is not a git repo, so it reports
    `scope: "external"`, `path: None` -- correctly omitting the absolute
    path). This reconstructs the real filesystem path directly from
    `run_synthesize`'s own documented convention (`synthesize.py`'s module
    docstring): `.klt/synthesize/<run_id>/synth_<hdl_toplevel>.ys`, next to the
    request file.
    """
    run_id = report.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id
    ):
        raise ValueError("invalid synthesis run_id")
    return str(tmp_path / ".klt" / "synthesize" / run_id / f"synth_{hdl_toplevel}.ys")


@requires_engine
def test_integration_arithmetic_absent_leaves_the_response_unchanged(tmp_path):
    report = run_synthesize(
        _write_request(tmp_path, None),
        pdk_variant=_ANY_CELL_LIBRARY[1],
        pdk_root=_ANY_CELL_LIBRARY[0],
    )
    assert report["arithmetic"] is None


@requires_engine
def test_integration_auto_sweep_reports_a_per_architecture_table(tmp_path):
    """Issue #1722 acceptance criterion 2, in miniature: `adders: "auto"`
    measures every candidate plus Yosys's own expansion and reports the
    delay/area table it selected from."""
    report = run_synthesize(
        _write_request(
            tmp_path,
            {"adders": "auto", "candidates": ["ripple", "kogge-stone"]},
        ),
        pdk_variant=_ANY_CELL_LIBRARY[1],
        pdk_root=_ANY_CELL_LIBRARY[0],
    )
    arithmetic = report["arithmetic"]
    assert arithmetic["status"] == "ok"
    assert arithmetic["mode"] == "auto"
    # Only the 16-bit add is wide enough for the default min_width of 8.
    assert arithmetic["adder_widths"] == [16]

    rows = {row["architecture"]: row for row in arithmetic["candidates"]}
    assert set(rows) == {DEFAULT_ADDER_LABEL, "ripple", "kogge-stone"}
    for name, row in rows.items():
        assert row["disqualified_reason"] is None, name
        assert row["measured"] is not None, name
        assert row["measured"]["instance_count"] > 0, name
        assert row["measured"]["area_um2"] > 0, name
        # Issue #1844: `candidates[].measured.netlist_path`/`script_path`
        # are the same `{path, scope}` shape as the top-level fields, not
        # a raw (potentially absolute) path string -- `tmp_path` has no
        # `.git` ancestor here, so both resolve `scope: "external"`.
        assert row["measured"]["netlist_path"]["scope"] == "external"
        assert row["measured"]["netlist_path"]["path"] is None
        assert row["measured"]["script_path"]["scope"] == "external"
        assert row["measured"]["script_path"]["path"] is None
    # Every substituted adder was proven equivalent to `a + b + cin` first.
    for name in ("ripple", "kogge-stone"):
        assert rows[name]["adder_equivalence"] == [
            {"width": 16, "status": "equivalent", "detail": None}
        ]
    # The structural metrics really do differ between the two.
    assert rows["ripple"]["logic_levels"] > rows["kogge-stone"]["logic_levels"]
    assert rows["ripple"]["prefix_cells"] < rows["kogge-stone"]["prefix_cells"]

    assert arithmetic["selected_architecture"] in rows
    assert (
        arithmetic["selected_measured"]["instance_count"] == (report["instance_count"])
    )


@requires_engine
def test_integration_explicit_architecture_substitutes_and_stays_equivalent(tmp_path):
    """An explicitly-named architecture runs no trials at all, substitutes
    directly, and the resulting netlist still passes the whole-design
    `klt equiv` gate against its source RTL."""
    report = run_synthesize(
        _write_request(tmp_path, {"adders": "kogge-stone"}),
        pdk_variant=_ANY_CELL_LIBRARY[1],
        pdk_root=_ANY_CELL_LIBRARY[0],
        verify_equivalence=True,
    )
    arithmetic = report["arithmetic"]
    assert arithmetic["mode"] == "explicit"
    assert arithmetic["selected_architecture"] == "kogge-stone"
    assert len(arithmetic["candidates"]) == 1
    assert arithmetic["candidates"][0]["measured"] is None
    assert arithmetic["selected_measured"] is not None
    assert report["equivalence"]["status"] == "equivalent"

    script = open(_synth_script_path(tmp_path, report), encoding="utf-8").read()
    assert "techmap -map" in script
    assert "klt_add_kogge_stone_16.v" in script


@requires_engine
def test_integration_min_width_can_rule_every_adder_out(tmp_path):
    report = run_synthesize(
        _write_request(tmp_path, {"adders": "auto", "min_width": 32}),
        pdk_variant=_ANY_CELL_LIBRARY[1],
        pdk_root=_ANY_CELL_LIBRARY[0],
    )
    arithmetic = report["arithmetic"]
    assert arithmetic["status"] == "no-wide-adders"
    assert arithmetic["adder_widths"] == []
    assert arithmetic["selected_architecture"] == DEFAULT_ADDER_LABEL
    assert "no $add cell of width >= 32" in arithmetic["reason"]


@requires_engine
def test_integration_adders_default_is_an_explicit_no_op(tmp_path):
    report = run_synthesize(
        _write_request(tmp_path, {"adders": "default"}),
        pdk_variant=_ANY_CELL_LIBRARY[1],
        pdk_root=_ANY_CELL_LIBRARY[0],
    )
    arithmetic = report["arithmetic"]
    assert arithmetic["status"] == "not-requested"
    assert arithmetic["candidates"] == []
    script = open(_synth_script_path(tmp_path, report), encoding="utf-8").read()
    assert "techmap -map" not in script


@requires_engine
def test_integration_probe_finds_the_elaborated_add_widths(tmp_path):
    (tmp_path / "adders.v").write_text(_ADDER_RTL)
    widths = synthesize._probe_add_widths(
        resolved_sources=[str(tmp_path / "adders.v")],
        hdl_toplevel="adders",
        output_dir=str(tmp_path / "probe"),
        min_width=2,
    )
    assert widths == [4, 16]
