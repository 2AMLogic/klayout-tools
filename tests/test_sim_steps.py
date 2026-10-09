"""Tests for `klt sim`'s ordered analysis steps (`analysis_steps[]`, issue #2482).

Several named solves inside one corner's deck, each optionally preceded by
`alter` source overrides, with step-scoped measurements and derived
measurements combining `<step>.<measurement>` results. Split out of
`tests/test_sim.py` (already ~11.7k lines) as a self-contained sibling,
following `tests/test_sim_save_mode.py`'s conventions: unit tests stub
`subprocess.run`, integration tests run the real `ngspice -b` and skip
without it.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_remote, sim_steps

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

#: A resistive divider: v(out) = vin * 2/3 with no load, so every number
#: below is exact by construction (3.0 V -> 2.0 V, 3.3 V -> 2.2 V).
_BODY = "VIN in 0 3.0\nR1 in out 1k\nR2 out 0 2k\nILOAD out 0 0\n"


@pytest.fixture(autouse=True)
def _bare_name_ngspice_resolution(monkeypatch):
    """Same stub as `tests/test_sim.py`'s identically-named fixture: the
    mocked-`subprocess.run` tests below need no real ngspice binary."""
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        if cmd == "ngspice":
            return cmd
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(paths_module.shutil, "which", fake_which)


def _write_body(tmp_path: Path, text: str = _BODY) -> Path:
    path = tmp_path / "body.spice"
    path.write_text(text)
    return path


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _two_point_request(**overrides) -> dict:
    """A line-sensitivity bench: two `op` solves at two input voltages, one
    derived figure over both, graded against limits."""
    request = {
        "netlist": "body.spice",
        "analysis_steps": [
            {
                "name": "lo",
                "analysis": {"kind": "op", "args": ""},
                "alter": {"VIN": 3.0},
                "measurements": [{"name": "vout", "expr": "v(out)", "unit": "V"}],
            },
            {
                "name": "hi",
                "analysis": {"kind": "op", "args": ""},
                "alter": {"VIN": 3.3},
                "measurements": [{"name": "vout", "expr": "v(out)", "unit": "V"}],
            },
        ],
        "measurements": [
            {
                "name": "line_sens",
                "expr": "(hi.vout - lo.vout) / 0.3",
                "unit": "V/V",
                "limits": {"min": 0.6, "max": 0.7},
            }
        ],
    }
    request.update(overrides)
    return request


def _resolve(request: dict):
    return sim_steps.resolve_analysis_steps(copy.deepcopy(request))


def _step_log(*, steps: int, failed: tuple[int, ...] = (), values: dict) -> str:
    """A synthetic ngspice log carrying the step markers and printed values
    a real run of this module's decks produces."""
    lines = ["Circuit: * klt"]
    for index in range(steps):
        lines.append(f"klt_step_begin {index} unknown{index + 1}")
        end_plot = f"unknown{index + 1}" if index in failed else f"op{index + 1}"
        lines.append(f"klt_step_end {index} {end_plot}")
    for key, value in values.items():
        lines.append(f"{key} = {value:e}")
    return "\n".join(lines) + "\n"


def _stub_ngspice(monkeypatch, log_text: str) -> list[str]:
    """Stub `subprocess.run`: write `log_text` as every corner's log and
    record the deck text each invocation was handed."""
    decks: list[str] = []

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        decks.append(Path(cmd[cmd.index("-b") + 1]).read_text())
        Path(cmd[cmd.index("-o") + 1]).write_text(log_text)
        return fake_completed("** ngspice-46\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return decks


# --------------------------------------------------------------------------- #
# Backward compatibility of the scalar form
# --------------------------------------------------------------------------- #


def test_scalar_analysis_resolves_to_the_requests_own_objects():
    request = {
        "netlist": "x",
        "analysis": {"kind": "op", "args": ""},
        "measurements": [{"name": "v", "expr": "v(out)"}],
    }
    analysis, specs = sim._resolve_analysis_and_measurements(request)
    assert analysis is request["analysis"]
    assert specs is request["measurements"]
    assert not sim_steps.is_steps_analysis(analysis)


def test_scalar_analysis_still_requires_kind_and_args():
    with pytest.raises(sim.SimError, match="requires 'kind' and 'args'"):
        sim._resolve_analysis_and_measurements({"analysis": {"kind": "op"}})


def test_scalar_run_report_gains_no_step_fields(tmp_path, monkeypatch):
    _write_body(tmp_path)
    _stub_ngspice(monkeypatch, "vmid = 2.000000e+00\n")
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "op", "args": ""},
        "measurements": [{"name": "vmid", "expr": "v(out)"}],
    }
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    corner = report["corners"][0]
    assert "steps" not in corner
    assert set(corner["measurements"][0]) == {
        "name",
        "value",
        "unit",
        "status",
        "margin",
    }
    assert "step" not in report["measurements"][0]
    assert "derived_from" not in report["measurements"][0]


def test_load_request_accepts_analysis_steps_without_analysis(tmp_path):
    path = _write_request(tmp_path, _two_point_request())
    assert sim.load_request(str(path))["analysis_steps"]


def test_load_request_requires_analysis_or_analysis_steps(tmp_path):
    path = _write_request(tmp_path, {"netlist": "body.spice"})
    with pytest.raises(sim.SimError, match="missing required field: analysis"):
        sim.load_request(str(path))


# --------------------------------------------------------------------------- #
# Resolution: flattened measurements and provenance
# --------------------------------------------------------------------------- #


def test_resolve_flattens_step_and_derived_measurements_in_order():
    analysis, specs = _resolve(_two_point_request())
    assert analysis["kind"] == sim_steps.STEPS_KIND
    assert [s["name"] for s in analysis["steps"]] == ["lo", "hi"]
    assert [s["alter"] for s in analysis["steps"]] == [[["VIN", 3.0]], [["VIN", 3.3]]]
    assert [s["name"] for s in specs] == ["lo.vout", "hi.vout", "line_sens"]
    assert sim_steps.measurement_provenance(specs[0]) == {"step": "lo"}
    assert sim_steps.measurement_provenance(specs[2]) == {
        "derived_from": ["hi.vout", "lo.vout"]
    }
    # No step-scoped spec may carry `spice`: that key lands at file scope.
    assert all("spice" not in s for s in specs)


def test_resolve_does_not_mutate_the_request():
    request = _two_point_request()
    before = copy.deepcopy(request)
    sim_steps.resolve_analysis_steps(request)
    assert request == before


def test_one_step_sequence_and_step_without_measurements_resolve():
    request = {
        "analysis_steps": [
            {"name": "settle", "analysis": {"kind": "op", "args": ""}},
            {
                "name": "meas",
                "analysis": {"kind": "op", "args": ""},
                "measurements": [{"name": "v", "expr": "v(out)"}],
            },
        ]
    }
    _, specs = _resolve(request)
    assert [s["name"] for s in specs] == ["meas.v"]
    _, single = _resolve({"analysis_steps": request["analysis_steps"][1:]})
    assert [s["name"] for s in single] == ["meas.v"]


def test_step_expression_may_reference_an_earlier_step():
    request = _two_point_request()
    request["analysis_steps"][1]["measurements"].append(
        {"name": "dv", "expr": "vout - lo.vout"}
    )
    _, specs = _resolve(request)
    dv = next(s for s in specs if s["name"] == "hi.dv")
    assert dv["deck_lines"][0] == "let dv = vout - {$klt_plot_0}.vout"
    assert sim_steps.measurement_provenance(dv) == {
        "step": "hi",
        "derived_from": ["lo.vout"],
    }


def test_signal_arguments_with_dots_are_not_step_references():
    request = _two_point_request()
    request["analysis_steps"][0]["measurements"] = [
        {"name": "vout", "expr": "v(x1.out) + @m.xm1.m0[id] * 0 + 1.5e-3"}
    ]
    request["measurements"] = []
    _, specs = _resolve(request)
    assert specs[0]["deck_lines"][0] == (
        "let vout = v(x1.out) + @m.xm1.m0[id] * 0 + 1.5e-3"
    )


def test_derived_chain_references_an_earlier_derived_measurement():
    request = _two_point_request()
    request["measurements"].append({"name": "sens_pct", "expr": "line_sens * 100"})
    _, specs = _resolve(request)
    assert specs[-1]["derived_from"] == ["line_sens"]
    assert specs[-1]["deck_lines"] == [
        "let sens_pct = line_sens * 100",
        "print sens_pct",
    ]


def test_step_meas_card_becomes_a_control_meas_command():
    request = {
        "analysis_steps": [
            {
                "name": "sweep",
                "analysis": {"kind": "dc", "args": "VIN 0 3 0.1"},
                "measurements": [
                    {"name": "vmid", "spice": ".meas dc vmid find v(out) at=1.5"}
                ],
            }
        ]
    }
    _, specs = _resolve(request)
    assert specs[0]["deck_lines"] == [
        "meas dc vmid find v(out) at=1.5",
        "let sweep__vmid = vmid",
        "print sweep__vmid",
    ]


# --------------------------------------------------------------------------- #
# Validation -- everything fails before any corner is dispatched
# --------------------------------------------------------------------------- #


def _mutated(mutate) -> dict:
    request = _two_point_request()
    mutate(request)
    return request


_INVALID = [
    (
        "both analysis forms",
        lambda r: r.update(analysis={"kind": "op", "args": ""}),
        "both 'analysis' and 'analysis_steps'",
    ),
    ("empty sequence", lambda r: r.update(analysis_steps=[]), "non-empty array"),
    ("not an array", lambda r: r.update(analysis_steps={}), "non-empty array"),
    (
        "duplicate step names",
        lambda r: r["analysis_steps"][1].update(name="LO"),
        "reuses step name",
    ),
    (
        "step name with a dot",
        lambda r: r["analysis_steps"][0].update(name="lo.x"),
        r"name must match",
    ),
    (
        "unknown analysis kind",
        lambda r: r["analysis_steps"][0].update(analysis={"kind": "quit", "args": ""}),
        "not a supported step analysis",
    ),
    (
        "multi-line args",
        lambda r: r["analysis_steps"][0].update(
            analysis={"kind": "op", "args": "\nquit"}
        ),
        "single-line",
    ),
    (
        "missing analysis args",
        lambda r: r["analysis_steps"][0].update(analysis={"kind": "op"}),
        "requires 'kind' and 'args'",
    ),
    (
        "alter not an object",
        lambda r: r["analysis_steps"][0].update(alter=[["VIN", 3]]),
        "alter must be an object",
    ),
    (
        "alter targets a transistor",
        lambda r: r["analysis_steps"][0].update(alter={"M1": 1.0}),
        "not a top-level independent source",
    ),
    (
        "alter targets a hierarchical element",
        lambda r: r["analysis_steps"][0].update(alter={"x1.r1": 1.0}),
        "not a top-level independent source",
    ),
    (
        "alter value is a bool",
        lambda r: r["analysis_steps"][0].update(alter={"VIN": True}),
        "finite number",
    ),
    (
        "alter value is a string",
        lambda r: r["analysis_steps"][0].update(alter={"VIN": "3.3"}),
        "finite number",
    ),
    (
        "alter value is NaN",
        lambda r: r["analysis_steps"][0].update(alter={"VIN": float("nan")}),
        "finite number",
    ),
    (
        "duplicate step measurement names",
        lambda r: r["analysis_steps"][0]["measurements"].append(
            {"name": "VOUT", "expr": "v(in)"}
        ),
        "declared more than once",
    ),
    (
        "step measurement with both forms",
        lambda r: r["analysis_steps"][0]["measurements"][0].update(
            spice=".meas op vout find v(out)"
        ),
        "both 'spice' and 'expr'",
    ),
    (
        # The flattened spec carries `meas_card`, not `spice`, so this must
        # be checked inside the step resolver, not by run_sim's own pass.
        "meas op card on an op step",
        lambda r: r["analysis_steps"][0]["measurements"].append(
            {"name": "vop", "spice": ".meas op vop find v(out)"}
        ),
        "unsupported .meas analysis type 'op'",
    ),
    (
        "meas card type differs from the step's kind",
        lambda r: r["analysis_steps"][0]["measurements"].append(
            {"name": "vt", "spice": ".meas tran vt find v(out) at=1n"}
        ),
        "type must match",
    ),
    (
        "meas card name differs from the measurement name",
        lambda r: r["analysis_steps"][0].update(
            analysis={"kind": "dc", "args": "VIN 0 3 0.1"},
            measurements=[{"name": "va", "spice": ".meas dc vb find v(out) at=1"}],
        ),
        "card's name must equal",
    ),
    (
        "top-level .meas card",
        lambda r: r["measurements"].append(
            {"name": "x", "spice": ".meas dc x find v(out) at=1"}
        ),
        "derived expression",
    ),
    (
        "derived references an unknown step",
        lambda r: r["measurements"][0].update(expr="mid.vout - lo.vout"),
        "'mid' is not an earlier step",
    ),
    (
        "derived references an unknown step measurement",
        lambda r: r["measurements"][0].update(expr="hi.vref - lo.vout"),
        "declares no measurement 'vref'",
    ),
    (
        "step forward reference",
        lambda r: r["analysis_steps"][0]["measurements"].append(
            {"name": "d", "expr": "vout - hi.vout"}
        ),
        "'hi' is not an earlier step",
    ),
    (
        "step self reference",
        lambda r: r["analysis_steps"][1]["measurements"].append(
            {"name": "d", "expr": "hi.vout * 2"}
        ),
        "'hi' is not an earlier step",
    ),
    (
        "derived forward reference",
        lambda r: r["measurements"].insert(
            0, {"name": "pct", "expr": "line_sens * 100"}
        ),
        "not declared before it",
    ),
    (
        "derived self reference",
        lambda r: r["measurements"][0].update(expr="line_sens + hi.vout"),
        "not declared before it",
    ),
    (
        "derived uses a bare step-measurement name",
        lambda r: r["measurements"][0].update(expr="vout * 2"),
        "qualify it with its step",
    ),
    (
        "derived references nothing",
        lambda r: r["measurements"][0].update(expr="1 + 2"),
        "references no step result",
    ),
    (
        "duplicate derived names",
        lambda r: r["measurements"].append({"name": "LINE_SENS", "expr": "lo.vout"}),
        "declared more than once",
    ),
    (
        "derived name collides with a step log key",
        lambda r: r["measurements"].append({"name": "lo__vout", "expr": "lo.vout"}),
        "ambiguous",
    ),
]


@pytest.mark.parametrize(
    "mutate,match", [(m, e) for _, m, e in _INVALID], ids=[i for i, _, _ in _INVALID]
)
def test_invalid_step_requests_are_refused(mutate, match):
    with pytest.raises(sim.SimError, match=match):
        _resolve(_mutated(mutate))


def test_ambiguous_step_log_keys_are_refused():
    request = {
        "analysis_steps": [
            {
                "name": "a",
                "analysis": {"kind": "op", "args": ""},
                "measurements": [{"name": "b__c", "expr": "v(out)"}],
            },
            {
                "name": "a__b",
                "analysis": {"kind": "op", "args": ""},
                "measurements": [{"name": "c", "expr": "v(out)"}],
            },
        ]
    }
    with pytest.raises(sim.SimError, match="ambiguous: both are harvested"):
        _resolve(request)


@pytest.mark.parametrize(
    "request_overrides,match",
    [
        ({"corners": {"supply_v": {"VIN": [3.0]}}}, "also a corners.supply_v axis"),
        ({"engine": "xyce"}, "analysis_steps is not supported for engine 'xyce'"),
        ({"options": {"waveforms": True}}, "not supported with analysis_steps"),
        (
            {"analysis_steps": [{"name": "x", "analysis": {}}]},
            "requires 'kind' and 'args'",
        ),
    ],
)
def test_run_sim_refuses_before_any_corner_runs(
    tmp_path, monkeypatch, request_overrides, match
):
    _write_body(tmp_path)
    decks = _stub_ngspice(monkeypatch, "")
    request = _write_request(tmp_path, _two_point_request(**request_overrides))
    with pytest.raises(sim.SimError, match=match):
        sim.run_sim(str(request))
    assert decks == []


def test_run_sim_refuses_plot_dir_with_steps(tmp_path, monkeypatch):
    _write_body(tmp_path)
    decks = _stub_ngspice(monkeypatch, "")
    request = _write_request(tmp_path, _two_point_request())
    with pytest.raises(sim.SimError, match="--plot"):
        sim.run_sim(str(request), plot_dir=str(tmp_path / "plots"))
    assert decks == []


def test_run_sim_refuses_alter_of_a_waveform_source(tmp_path, monkeypatch):
    _write_body(tmp_path, _BODY + "VSTEP s 0 PULSE(0 1 0 1n 1n 1u 2u)\n")
    decks = _stub_ngspice(monkeypatch, "")
    request = _two_point_request()
    request["analysis_steps"][0]["alter"] = {"VSTEP": 0.5}
    with pytest.raises(sim.SimError, match="explicit PULSE waveform"):
        sim.run_sim(str(_write_request(tmp_path, request)))
    assert decks == []


# --------------------------------------------------------------------------- #
# Deck generation
# --------------------------------------------------------------------------- #


def test_two_step_deck_orders_overrides_solves_capture_and_derived(tmp_path):
    _write_body(tmp_path)
    analysis, specs = _resolve(_two_point_request())
    deck = tmp_path / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=sim.CornerPoint(None, {"VDD": 1.8}, 27),
        analysis=analysis,
        measurements_spec=specs,
        raw_path=None,
    )
    lines = deck.read_text().splitlines()
    control = lines[lines.index(".control") : lines.index(".endc") + 1]
    assert control == [
        ".control",
        "save all",
        "alter VDD=1.8",
        "alter VIN=3.0",
        "setplot new",
        "echo klt_step_begin 0 $curplot",
        "op",
        "echo klt_step_end 0 $curplot",
        "set klt_plot_0 = $curplot",
        "let vout = v(out)",
        "let lo__vout = vout",
        "print lo__vout",
        "alter VIN=3.3",
        "setplot new",
        "echo klt_step_begin 1 $curplot",
        "op",
        "echo klt_step_end 1 $curplot",
        "set klt_plot_1 = $curplot",
        "let vout = v(out)",
        "let hi__vout = vout",
        "print hi__vout",
        "setplot new",
        "let line_sens = ({$klt_plot_1}.vout - {$klt_plot_0}.vout) / 0.3",
        "print line_sens",
        "quit",
        ".endc",
    ]
    # No file-scope `.meas` cards: every step-scoped card is a control command.
    assert not any(line.lower().startswith(".meas") for line in lines)


def test_repeated_analysis_kinds_and_negative_zero_overrides(tmp_path):
    request = {
        "analysis_steps": [
            {
                "name": f"p{i}",
                "analysis": {"kind": "op", "args": ""},
                "alter": {"ILOAD": value},
            }
            for i, value in enumerate((-1e-3, 0, 2.5e-3))
        ]
    }
    analysis, specs = _resolve(request)
    lines = sim_steps.control_lines(analysis, specs)
    assert [line for line in lines if line.startswith("alter")] == [
        "alter ILOAD=-0.001",
        "alter ILOAD=0",
        "alter ILOAD=0.0025",
    ]
    assert lines.count("op") == 3
    assert "setplot new" in lines


# --------------------------------------------------------------------------- #
# Step outcomes, grading and failure propagation
# --------------------------------------------------------------------------- #


def test_step_outcomes_from_markers():
    analysis, _ = _resolve(_two_point_request())
    assert sim_steps.step_outcomes(analysis, _step_log(steps=2, values={})) == {
        "lo": True,
        "hi": True,
    }
    assert sim_steps.step_outcomes(
        analysis, _step_log(steps=2, failed=(1,), values={})
    ) == {"lo": True, "hi": False}
    # A killed run never prints the second step's end marker.
    truncated = "klt_step_begin 0 unknown1\nklt_step_end 0 op1\nklt_step_begin 1 x\n"
    assert sim_steps.step_outcomes(analysis, truncated) == {"lo": True, "hi": False}


def _grade(request: dict, log_text: str, *, save_mode: str = "all"):
    analysis, specs = _resolve(request)
    return sim._corner_measurement_results(
        sim._parse_measurements(log_text),
        specs,
        analysis=analysis,
        log_text=log_text,
        is_xyce=False,
        save_mode=save_mode,
    )


def test_grading_uses_the_existing_vocabulary():
    log = _step_log(
        steps=2, values={"lo__vout": 2.0, "hi__vout": 2.2, "line_sens": 0.6666667}
    )
    results, diagnostics, extra = _grade(_two_point_request(), log)
    assert diagnostics == []
    assert extra == {
        "steps": [
            {"name": "lo", "kind": "op", "status": "ok"},
            {"name": "hi", "kind": "op", "status": "ok"},
        ]
    }
    by_name = {r["name"]: r for r in results}
    assert by_name["lo.vout"] == {
        "name": "lo.vout",
        "value": 2.0,
        "unit": "V",
        "status": "pass",
        "margin": None,
        "step": "lo",
    }
    derived = by_name["line_sens"]
    assert derived["status"] == "pass"
    assert derived["margin"] == pytest.approx(0.0333333)
    assert derived["derived_from"] == ["hi.vout", "lo.vout"]


def test_derived_limit_miss_fails_with_negative_margin():
    log = _step_log(
        steps=2, values={"lo__vout": 2.0, "hi__vout": 2.5, "line_sens": 1.6666667}
    )
    results, _, _ = _grade(_two_point_request(), log)
    derived = results[-1]
    assert derived["status"] == "fail"
    assert derived["margin"] == pytest.approx(0.7 - 1.6666667)


def test_negative_and_zero_values_are_graded_not_treated_as_missing():
    request = _two_point_request()
    request["measurements"][0]["limits"] = {"min": -1.0, "max": 0.0}
    log = _step_log(steps=2, values={"lo__vout": 0.0, "hi__vout": -0.5, "line_sens": 0})
    results, diagnostics, _ = _grade(request, log)
    assert diagnostics == []
    assert [r["value"] for r in results] == [0.0, -0.5, 0.0]
    assert results[-1]["status"] == "pass"
    assert results[-1]["margin"] == 0.0


def test_failed_intermediate_step_never_grades_its_dependents():
    request = _two_point_request()
    request["measurements"].append({"name": "pct", "expr": "line_sens * 100"})
    # Even a value printed under a failed step's key must not be read: the
    # step produced no plot of its own, so it cannot be this step's solve.
    log = _step_log(
        steps=2,
        failed=(1,),
        values={"lo__vout": 2.0, "hi__vout": 2.0, "line_sens": 0.0, "pct": 0.0},
    )
    results, diagnostics, extra = _grade(request, log)
    by_name = {r["name"]: r for r in results}
    # Independent earlier evidence stays inspectable.
    assert by_name["lo.vout"]["value"] == 2.0
    assert by_name["lo.vout"]["status"] == "pass"
    for name in ("hi.vout", "line_sens", "pct"):
        assert by_name[name]["value"] is None
        assert by_name[name]["status"] == "error"
    codes = [d["code"] for d in diagnostics]
    assert codes == [
        "step_failed",
        "derived_input_unavailable",
        "derived_input_unavailable",
    ]
    assert "'hi'" in diagnostics[0]["message"]
    assert "hi.vout" in diagnostics[0]["message"]
    assert "hi.vout" in diagnostics[1]["message"]
    assert "line_sens" in diagnostics[2]["message"]
    assert extra["steps"][1] == {"name": "hi", "kind": "op", "status": "error"}


def test_missing_step_value_is_a_measurement_error_and_blocks_dependents():
    log = _step_log(steps=2, values={"lo__vout": 2.0})
    results, diagnostics, extra = _grade(_two_point_request(), log)
    assert [d["code"] for d in diagnostics] == [
        "measurement",
        "derived_input_unavailable",
    ]
    assert "step 'hi' completed" in diagnostics[0]["message"]
    assert all(step["status"] == "ok" for step in extra["steps"])
    assert results[-1]["status"] == "error"


def test_failed_derived_expression_is_distinguished_from_a_failed_step():
    log = _step_log(steps=2, values={"lo__vout": 2.0, "hi__vout": 2.2})
    results, diagnostics, _ = _grade(_two_point_request(), log)
    assert [d["code"] for d in diagnostics] == ["measurement"]
    assert diagnostics[0]["message"].startswith(
        "derived measurement 'line_sens' produced no value: every input"
    )
    assert results[-1]["status"] == "error"
    assert [r["status"] for r in results[:2]] == ["pass", "pass"]


def test_netlist_save_mode_hint_is_appended_for_step_measurements():
    log = _step_log(steps=2, values={"hi__vout": 2.2})
    _, diagnostics, _ = _grade(_two_point_request(), log, save_mode="netlist")
    assert diagnostics[0]["message"].endswith(sim.SAVE_MODE_NETLIST_NO_VALUE_HINT)


def test_new_diagnostic_codes_are_selectable_in_fail_on_diagnostic():
    assert sim._validate_fail_on_diagnostic(
        ["step_failed", "derived_input_unavailable"]
    ) == ("step_failed", "derived_input_unavailable")


# --------------------------------------------------------------------------- #
# run_sim end to end (stubbed engine), rollup, checkpoint, backends
# --------------------------------------------------------------------------- #


def test_run_sim_report_carries_step_provenance(tmp_path, monkeypatch):
    _write_body(tmp_path)
    log = _step_log(
        steps=2, values={"lo__vout": 2.0, "hi__vout": 2.2, "line_sens": 0.6666667}
    )
    decks = _stub_ngspice(monkeypatch, log)
    request = _two_point_request(corners={"temperature_c": [27, 85]})
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    assert report["status"] == "pass"
    assert len(decks) == 2  # one deck per corner, every step inside it
    assert all(deck.count("\nop\n") == 2 for deck in decks)
    assert [m["name"] for m in report["measurements"]] == [
        "lo.vout",
        "hi.vout",
        "line_sens",
    ]
    assert report["measurements"][0]["step"] == "lo"
    assert report["measurements"][2]["derived_from"] == ["hi.vout", "lo.vout"]
    assert report["measurements"][2]["worst_case"]["value"] == pytest.approx(0.6666667)
    corner = report["corners"][0]
    assert [s["status"] for s in corner["steps"]] == ["ok", "ok"]
    assert report["coverage"]["measurements_declared"] == 3


def test_run_sim_failed_step_makes_the_corner_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    _stub_ngspice(monkeypatch, _step_log(steps=2, failed=(1,), values={"lo__vout": 2}))
    report = sim.run_sim(str(_write_request(tmp_path, _two_point_request())))
    assert report["status"] == "error"
    assert report["errored"] == 1
    assert report["diagnostic_counts"]["by_code"] == {
        "step_failed": 1,
        "derived_input_unavailable": 1,
    }
    assert report["measurements"][0]["status"] == "pass"
    assert report["measurements"][2]["status"] == "error"


def test_checkpoint_fingerprint_covers_the_ordered_step_definition(tmp_path):
    body = _write_body(tmp_path)

    def fingerprint(request):
        analysis, specs = _resolve(request)
        return sim._checkpoint_fingerprint(
            netlist_path=str(body),
            models_lib=None,
            analysis=analysis,
            measurements_spec=specs,
            corner_points=[sim.CornerPoint(None, {}, 27)],
            timeout_s=10.0,
            engine="ngspice",
        )

    base = fingerprint(_two_point_request())
    assert fingerprint(_two_point_request()) == base
    changed_alter = _two_point_request()
    changed_alter["analysis_steps"][0]["alter"] = {"VIN": 2.9}
    reordered = _two_point_request()
    reordered["analysis_steps"].reverse()
    reordered["measurements"] = []
    changed_args = _two_point_request()
    changed_args["analysis_steps"][1]["analysis"]["args"] = " "
    for variant in (changed_alter, reordered, changed_args):
        assert fingerprint(variant) != base


def test_resume_reuses_a_completed_step_corner(tmp_path, monkeypatch):
    _write_body(tmp_path)
    log = _step_log(
        steps=2, values={"lo__vout": 2.0, "hi__vout": 2.2, "line_sens": 0.6666667}
    )
    decks = _stub_ngspice(monkeypatch, log)
    body = tmp_path / "body.spice"
    request = _two_point_request(corners={"temperature_c": [27, 85]})
    path = _write_request(tmp_path, request)
    artifacts = tmp_path / "artifacts"
    analysis, specs = _resolve(request)
    points = sim._expand_corners(request["corners"], [])
    fingerprint = sim._checkpoint_fingerprint(
        netlist_path=str(body),
        models_lib=None,
        analysis=analysis,
        measurements_spec=specs,
        corner_points=points,
        timeout_s=sim.DEFAULT_TIMEOUT_S,
        engine="ngspice",
    )
    # An interrupted earlier run of this same step request completed corner 0.
    recorded = {
        "corner_id": points[0].corner_id,
        "process": None,
        "supply_v": {},
        "temperature_c": 27,
        "status": "pass",
        "runtime_s": 0.0,
        "measurements": [
            {"name": s["name"], "value": 1.0, "unit": None, "status": "pass"}
            | {"margin": None}
            for s in specs
        ],
        "diagnostics": [],
        "artifacts": {"log": None, "raw": None, "waveform": None, "deck": None},
        "monte_carlo": None,
        "marker": "from-checkpoint",
    }
    checkpoint = sim._Checkpoint(sim._checkpoint_path(str(artifacts)), fingerprint)
    checkpoint.record(points[0].corner_id, recorded, "46")

    report = sim.run_sim(str(path), artifacts_dir=str(artifacts), resume=True)
    assert report["environment"]["resume"]["resumed_corners"] == 1
    assert len(decks) == 1  # only the unfinished corner was re-run
    assert report["corners"][0]["marker"] == "from-checkpoint"
    assert [s["status"] for s in report["corners"][1]["steps"]] == ["ok", "ok"]


def test_local_parallel_backend_runs_step_sequences(tmp_path, monkeypatch):
    _write_body(tmp_path)
    log = _step_log(
        steps=2, values={"lo__vout": 2.0, "hi__vout": 2.2, "line_sens": 0.6666667}
    )
    _stub_ngspice(monkeypatch, log)
    request = _two_point_request(
        backend="local-parallel", corners={"temperature_c": [0, 27, 85]}
    )
    report = sim.run_sim(str(_write_request(tmp_path, request)), max_workers=2)
    assert report["passed"] == 3
    assert all("steps" in corner for corner in report["corners"])


def test_remote_request_document_forwards_the_step_sequence_verbatim():
    request = _two_point_request(remote={"region": "us-east-1"})
    document = sim_remote._build_remote_request(
        request, timeout_s=10.0, keep_artifacts=False, want_waveforms=False
    )
    assert document["analysis_steps"] == request["analysis_steps"]
    assert document["measurements"] == request["measurements"]
    assert "analysis" not in document
    assert document["backend"] == "local-parallel"


def test_unrun_corner_report_carries_step_provenance():
    _, specs = _resolve(_two_point_request())
    report = sim_remote._unrun_corner_report(
        sim.CornerPoint(None, {}, 27), specs, "budget_exceeded"
    )
    assert [m.get("step") for m in report["measurements"]] == ["lo", "hi", None]
    assert report["measurements"][2]["derived_from"] == ["hi.vout", "lo.vout"]


# --------------------------------------------------------------------------- #
# Integration: the real ngspice binary
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_real_ngspice_two_point_line_sensitivity(tmp_path):
    _write_body(tmp_path)
    request = _two_point_request(
        corners={"temperature_c": [27, 85]},
        options={"keep_artifacts": True},
    )
    request["analysis_steps"].append(
        {
            "name": "sweep",
            "analysis": {"kind": "dc", "args": "VIN 0 3 0.1"},
            "measurements": [
                {"name": "vmid", "spice": ".meas dc vmid find v(out) at=1.5"}
            ],
        }
    )
    request["measurements"].append({"name": "pct", "expr": "line_sens * 100"})
    report = sim.run_sim(
        str(_write_request(tmp_path, request)), artifacts_dir=str(tmp_path / "a")
    )
    assert report["status"] == "pass", report["corners"][0]["diagnostics"]
    for corner in report["corners"]:
        values = {m["name"]: m["value"] for m in corner["measurements"]}
        assert values["lo.vout"] == pytest.approx(2.0)
        assert values["hi.vout"] == pytest.approx(2.2)
        assert values["sweep.vmid"] == pytest.approx(1.0)
        # Computed inside ngspice from the full-precision step vectors.
        assert values["line_sens"] == pytest.approx(2 / 3, rel=1e-6)
        assert values["pct"] == pytest.approx(200 / 3, rel=1e-6)
        assert [s["status"] for s in corner["steps"]] == ["ok", "ok", "ok"]
    derived = report["measurements"][3]
    assert derived["name"] == "line_sens"
    assert derived["status"] == "pass"
    assert derived["worst_case"]["margin"] == pytest.approx(0.7 - 2 / 3, rel=1e-5)


@_SKIP_NO_NGSPICE
def test_real_ngspice_failed_intermediate_step(tmp_path):
    _write_body(tmp_path)
    request = _two_point_request()
    # `VNOPE` is not in the circuit: ngspice aborts that dc analysis and
    # produces no plot; the later step must still solve normally.
    request["analysis_steps"].insert(
        1,
        {
            "name": "bad",
            "analysis": {"kind": "dc", "args": "VNOPE 0 1 0.1"},
            "measurements": [{"name": "v", "expr": "v(out)[0]"}],
        },
    )
    request["measurements"].append({"name": "via_bad", "expr": "bad.v - lo.vout"})
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    corner = report["corners"][0]
    assert corner["status"] == "error"
    assert [s["status"] for s in corner["steps"]] == ["ok", "error", "ok"]
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert values["lo.vout"] == pytest.approx(2.0)
    assert values["hi.vout"] == pytest.approx(2.2)
    assert values["line_sens"] == pytest.approx(2 / 3, rel=1e-6)
    assert values["bad.v"] is None
    assert values["via_bad"] is None
    codes = [d["code"] for d in corner["diagnostics"]]
    assert "step_failed" in codes
    assert "derived_input_unavailable" in codes


@_SKIP_NO_NGSPICE
def test_real_ngspice_failed_derived_expression(tmp_path):
    _write_body(tmp_path)
    request = _two_point_request()
    request["measurements"].append(
        {"name": "bad_ratio", "expr": "hi.vout / (lo.vout - lo.vout)"}
    )
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    corner = report["corners"][0]
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert values["line_sens"] == pytest.approx(2 / 3, rel=1e-6)
    assert values["bad_ratio"] is None
    failing = [d for d in corner["diagnostics"] if "bad_ratio" in d["message"]]
    assert [d["code"] for d in failing] == ["measurement"]
    assert all(step["status"] == "ok" for step in corner["steps"])
