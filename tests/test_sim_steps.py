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
from klayout_tools import sim, sim_remote, sim_steps

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

#: A resistive divider: v(out) = vin * 2/3 with no load, so every number
#: below is exact by construction (3.0 V -> 2.0 V, 3.3 V -> 2.2 V).
_BODY = "VIN in 0 3.0\nR1 in out 1k\nR2 out 0 2k\nILOAD out 0 0\n"


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

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
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


_PWL_BODY = _BODY + "VEN en 0 dc 0 pwl(0 0 1u 0 2u 3.3 1 3.3)\n"


def _pwl_request(value, step=0):
    request = _two_point_request()
    request["analysis_steps"][step]["alter"] = {"VEN": value}
    return request


def test_run_sim_accepts_pwl_alter_of_a_pwl_source(tmp_path, monkeypatch):
    _write_body(tmp_path, _PWL_BODY)
    decks = _stub_ngspice(monkeypatch, "")
    request = _pwl_request({"pwl": [0, 0, 1e-6, 0, 2e-6, 1.8, 1, 1.8]})
    sim.run_sim(str(_write_request(tmp_path, request)))
    assert decks
    assert "alter @VEN[pwl] = [ 0 0 1e-06 0 2e-06 1.8 1 1.8 ]" in decks[0]


def test_pwl_alter_is_case_insensitive_and_allows_repeated_time(tmp_path):
    analysis, specs = _resolve(
        {
            "analysis_steps": [
                {
                    "name": "a",
                    "analysis": {"kind": "op", "args": ""},
                    "alter": {"VEN": {"pwl": [0, 0, 1, 0, 1, 3, 2, 3]}},
                }
            ]
        }
    )
    sim_steps._check_alter_target(
        "a", "VEN", set(), {"ven": "pwl"}, analysis["steps"][0]["alter"][0][1]
    )
    lines = sim_steps.control_lines(analysis, specs)
    assert "alter @VEN[pwl] = [ 0 0 1 0 1 3 2 3 ]" in lines


@pytest.mark.parametrize(
    "value, match",
    [
        ({"pwl": [0, 0, 1]}, "even-length"),
        ({"pwl": [0, 0]}, "at least 2 points"),
        ({"pwl": [0, 0, 1, float("inf")]}, "finite"),
        ({"pwl": [0, 0, 1, True]}, "finite"),
        ({"pwl": "0 0 1 1"}, "even-length"),
        ({"pwl": [0, 0, 2, 1, 1, 1]}, "decreasing times"),
        ({"pwl": [0, 0, 1, 1], "extra": 1}, "exactly one key"),
    ],
)
def test_run_sim_refuses_malformed_pwl_alter(tmp_path, monkeypatch, value, match):
    _write_body(tmp_path, _PWL_BODY)
    decks = _stub_ngspice(monkeypatch, "")
    with pytest.raises(sim.SimError, match=match):
        sim.run_sim(str(_write_request(tmp_path, _pwl_request(value))))
    assert decks == []


@pytest.mark.parametrize(
    "extra, target, match",
    [
        ("VEN en 0 dc 1.0\n", "VEN", "no PWL waveform"),
        ("VEN en 0 PULSE(0 1 0 1n 1n 1u 2u)\n", "VEN", "PULSE waveform"),
        ("Rx a 0 1k\n", "Rx", "no PWL waveform"),
    ],
)
def test_run_sim_refuses_pwl_alter_of_a_non_pwl_target(
    tmp_path, monkeypatch, extra, target, match
):
    _write_body(tmp_path, _BODY + extra)
    decks = _stub_ngspice(monkeypatch, "")
    request = _two_point_request()
    request["analysis_steps"][0]["alter"] = {target: {"pwl": [0, 0, 1, 1]}}
    with pytest.raises(sim.SimError, match=match):
        sim.run_sim(str(_write_request(tmp_path, request)))
    assert decks == []


def test_run_sim_scalar_alter_of_a_pwl_source_points_to_pwl_form(tmp_path, monkeypatch):
    _write_body(tmp_path, _PWL_BODY)
    decks = _stub_ngspice(monkeypatch, "")
    with pytest.raises(sim.SimError, match=r'explicit PWL waveform.*\{"pwl"'):
        sim.run_sim(str(_write_request(tmp_path, _pwl_request(1.8))))
    assert decks == []


def test_pwl_alter_still_conflicts_with_supply_axis():
    with pytest.raises(sim.SimError, match="corners.supply_v"):
        sim_steps._check_alter_target(
            "a", "VEN", {"ven"}, {"ven": "pwl"}, {"pwl": [0, 0, 1, 1]}
        )


def test_run_sim_refuses_alter_of_a_param_only_name(tmp_path, monkeypatch):
    """Issue #2867: `alter <param>=...` is a silent no-op in ngspice."""
    _write_body(tmp_path, _BODY + ".param vbias=0.5\nRb out 0 {vbias*1meg}\n")
    decks = _stub_ngspice(monkeypatch, "")
    request = _two_point_request()
    request["analysis_steps"][1]["alter"] = {"vbias": 0.7}
    with pytest.raises(sim.SimError, match=r"step 'hi'.*'vbias' names a \.param"):
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


def _ac_request(alter_dm: dict, alter_cm: dict | None = None) -> dict:
    step = {"analysis": {"kind": "ac", "args": "dec 1 1k 10k"}}
    return {
        "netlist": "body.spice",
        "analysis_steps": [
            {"name": "dm", "alter": alter_dm, **step},
            {"name": "cm", "alter": alter_cm or {}, **step},
        ],
    }


def test_ac_alter_emits_ac_magnitude_cards_in_declaration_order():
    request = _ac_request({"VDM.ac": 1, "VCM.ac": 0, "VDM": 0.5}, {"VDM.ac": 0})
    analysis, specs = _resolve(request)
    lines = sim_steps.control_lines(analysis, specs)
    assert [line for line in lines if line.startswith("alter")] == [
        "alter VDM ac = 1",
        "alter VCM ac = 0",
        "alter VDM=0.5",
        "alter VDM ac = 0",
    ]


@pytest.mark.parametrize(
    ("alter", "match"),
    [
        ({"R1.ac": 1}, "V or I source"),
        ({"V1.foo": 1}, r"only supported key suffix is '\.ac'"),
        ({"V1.ac": float("inf")}, "finite number"),
        ({"V1.ac": {"pwl": [0, 0, 1, 1]}}, "not a pwl object"),
        ({".ac": 1}, "not a top-level independent source"),
        ({"V1.ac": 1, "v1.AC": 2}, "is repeated"),
    ],
)
def test_ac_alter_refusals(alter, match):
    with pytest.raises(sim.SimError, match=match):
        _resolve(_ac_request(alter))


def test_ac_alter_changes_the_checkpoint_definition():
    a, _ = _resolve(_ac_request({"V1.ac": 1}))
    b, _ = _resolve(_ac_request({"V1.ac": 2}))
    assert json.dumps(a["steps"], sort_keys=True) != json.dumps(
        b["steps"], sort_keys=True
    )


def test_run_sim_ac_alter_skips_dc_only_rules_but_needs_an_element(
    tmp_path, monkeypatch
):
    _write_body(tmp_path, _BODY + "VSTEP s 0 PULSE(0 1 0 1n 1n 1u 2u)\n")
    decks = _stub_ngspice(monkeypatch, "")
    request = _ac_request({"VSTEP.ac": 1})
    request["corners"] = {"supply_v": {"VIN": [3.0]}}
    request["analysis_steps"][1]["alter"] = {"VIN.ac": 0}
    sim.run_sim(str(_write_request(tmp_path, request)))  # not refused
    assert "alter VSTEP ac = 1" in decks[0]
    decks.clear()
    request["analysis_steps"][0]["alter"] = {"VNOPE.ac": 1}
    with pytest.raises(sim.SimError, match="names no element"):
        sim.run_sim(str(_write_request(tmp_path, request)))
    assert decks == []


@_SKIP_NO_NGSPICE
def test_real_ngspice_ac_alter_is_cumulative(tmp_path):
    _write_body(tmp_path, "V1 in 0 0 AC 1\nR1 in out 1k\nR2 out 0 1k\n")
    request = {
        "netlist": "body.spice",
        "analysis_steps": [
            {
                "name": f"s{i}",
                "alter": alter,
                "analysis": {"kind": "ac", "args": "dec 1 1k 10k"},
                "measurements": [{"name": "g", "expr": "mag(v(out))[0]"}],
            }
            for i, alter in enumerate(({"V1.ac": 2}, {}))
        ],
    }
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    assert report["status"] == "pass", report["corners"][0]["diagnostics"]
    values = {m["name"]: m["value"] for m in report["corners"][0]["measurements"]}
    assert values["s0.g"] == pytest.approx(1.0, rel=1e-3)
    assert values["s1.g"] == pytest.approx(1.0, rel=1e-3)


def _reset_request(**overrides) -> dict:
    """Step 1 loads the divider; step 2 is `reset` and does not re-alter."""
    request = {
        "netlist": "body.spice",
        "analysis_steps": [
            {
                "name": "loaded",
                "analysis": {"kind": "op", "args": ""},
                "alter": {"ILOAD": 1e-3},
                "measurements": [{"name": "vout", "expr": "v(out)"}],
            },
            {
                "name": "fresh",
                "reset": True,
                "analysis": {"kind": "op", "args": ""},
                "measurements": [{"name": "vout", "expr": "v(out)"}],
            },
        ],
        "measurements": [{"name": "drop", "expr": "fresh.vout - loaded.vout"}],
    }
    request.update(overrides)
    return request


@pytest.mark.parametrize("bad", ["yes", 1, 0, [], {}])
def test_non_boolean_reset_is_refused_by_name(bad):
    request = _reset_request()
    request["analysis_steps"][1]["reset"] = bad
    with pytest.raises(sim.SimError, match=r"step 'fresh': reset must be a boolean"):
        _resolve(request)


def test_reset_false_resolves_identically_to_no_reset():
    plain = _reset_request()
    del plain["analysis_steps"][1]["reset"]
    explicit = _reset_request()
    explicit["analysis_steps"][1]["reset"] = False
    assert _resolve(explicit) == _resolve(plain)


def test_reset_is_part_of_the_resolved_step_definition():
    analysis, _ = _resolve(_reset_request())
    assert "reset" not in analysis["steps"][0]
    assert analysis["steps"][1]["reset"] is True


def test_reset_step_deck_reemits_save_and_supply_alters(tmp_path):
    _write_body(tmp_path)
    analysis, specs = _resolve(_reset_request())
    deck = tmp_path / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=sim.CornerPoint(None, {"VIN": 3.3}, 27),
        analysis=analysis,
        measurements_spec=specs,
        raw_path=None,
    )
    lines = deck.read_text().splitlines()
    control = lines[lines.index(".control") : lines.index(".endc") + 1]
    assert control == [
        ".control",
        "save all",
        "alter VIN=3.3",
        "alter ILOAD=0.001",
        "setplot new",
        "echo klt_step_begin 0 $curplot",
        "op",
        "echo klt_step_end 0 $curplot",
        "set klt_plot_0 = $curplot",
        "let vout = v(out)",
        "let loaded__vout = vout",
        "print loaded__vout",
        "reset",
        "save all",
        "alter VIN=3.3",
        "setplot new",
        "echo klt_step_begin 1 $curplot",
        "op",
        "echo klt_step_end 1 $curplot",
        "set klt_plot_1 = $curplot",
        "let vout = v(out)",
        "let fresh__vout = vout",
        "print fresh__vout",
        "setplot new",
        "let drop = {$klt_plot_1}.vout - {$klt_plot_0}.vout",
        "print drop",
        "quit",
        ".endc",
    ]


def test_reset_prefix_omits_save_all_in_netlist_save_mode():
    analysis, specs = _resolve(_reset_request())
    lines = sim._corner_deck_control_lines(
        point=sim.CornerPoint(None, {}, 27),
        analysis=analysis,
        measurements_spec=specs,
        raw_path=None,
        osdi_preload=(),
        save_mode="netlist",
    )
    assert "save all" not in lines
    assert lines[lines.index("reset") + 1] == "setplot new"


def test_deck_without_reset_has_no_reset_command():
    analysis, specs = _resolve(_two_point_request())
    lines = sim_steps.control_lines(analysis, specs, reset_prefix=["save all"])
    assert "reset" not in lines
    assert lines == sim_steps.control_lines(analysis, specs)


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


def test_optional_true_is_refused_in_step_and_derived_measurements():
    """Issue #2925: `optional` is scalar-analysis only; refused, not ignored."""
    step_request = _two_point_request()
    step_request["analysis_steps"][0]["measurements"][0]["optional"] = True
    with pytest.raises(sim.SimError, match="'optional' is not supported"):
        _resolve(step_request)
    derived_request = _two_point_request()
    derived_request["measurements"][0]["optional"] = True
    with pytest.raises(sim.SimError, match="'optional' is not supported"):
        _resolve(derived_request)
    # `false` is the default and stays accepted.
    ok = _two_point_request()
    ok["measurements"][0]["optional"] = False
    _resolve(ok)


# --------------------------------------------------------------------------- #
# `<step>.spectrum.<vector>`: a noise step's spectrum plot (issue #3284)
# --------------------------------------------------------------------------- #

_NOISE_BODY = "V1 in 0 dc 0 ac 1\nR1 in out 1k\nR2 out 0 2k\n"


def _noise_request(**overrides) -> dict:
    """Two `noise` solves with a different `R2`, one spectrum point of each
    plus a derived ratio -- the thermal noise of the divider differs."""
    step = {
        "analysis": {"kind": "noise", "args": "v(out) V1 dec 3 10 1e5"},
        "measurements": [{"name": "tot", "expr": "onoise_total"}],
    }
    request = {
        "netlist": "body.spice",
        "analysis_steps": [
            {"name": "a", "alter": {"R2": 2000}, **copy.deepcopy(step)},
            {"name": "b", "alter": {"R2": 8000}, **copy.deepcopy(step)},
        ],
        "measurements": [
            {"name": "a_spot", "expr": "a.spectrum.onoise_spectrum[1]"},
            {"name": "b_spot", "expr": "b.spectrum.onoise_spectrum[1]"},
        ],
    }
    request.update(overrides)
    return request


def test_spectrum_reference_rewrites_to_the_steps_own_spectrum_plot():
    analysis, specs = _resolve(_noise_request())
    a_spot = next(s for s in specs if s["name"] == "a_spot")
    b_spot = next(s for s in specs if s["name"] == "b_spot")
    assert a_spot["deck_lines"][0] == (
        "let a_spot = {$klt_plot_0_spectrum}.onoise_spectrum[1]"
    )
    assert b_spot["deck_lines"][0] == (
        "let b_spot = {$klt_plot_1_spectrum}.onoise_spectrum[1]"
    )
    assert a_spot["spectrum_steps"] == ["a"]
    # Provenance fields are unchanged: a spectrum read is not a step result.
    assert sim_steps.measurement_provenance(a_spot) == {"derived_from": []}
    lines = sim_steps.control_lines(analysis, specs)
    assert lines.count("set klt_plot_0_spectrum = $klt_pa0") == 1
    assert lines.count("set klt_plot_1_spectrum = $klt_pa1") == 1
    assert lines.index("set klt_plot_0 = $curplot") < lines.index(
        "set klt_plot_0_spectrum = $klt_pa0"
    )


def test_spectrum_capture_only_for_referenced_noise_steps():
    request = _noise_request()
    request["measurements"] = [request["measurements"][1]]
    analysis, specs = _resolve(request)
    lines = sim_steps.control_lines(analysis, specs)
    assert "set klt_plot_1_spectrum = $klt_pa1" in lines
    assert not any("klt_plot_0_spectrum" in line for line in lines)


def test_decks_without_spectrum_references_are_unchanged():
    request = _noise_request()
    request["measurements"] = [{"name": "t", "expr": "a.tot - b.tot"}]
    analysis, specs = _resolve(request)
    assert not any(
        "spectrum" in line for line in sim_steps.control_lines(analysis, specs)
    )


def test_step_named_spectrum_and_measurement_named_spectrum_do_not_collide():
    request = _noise_request()
    request["analysis_steps"][0]["measurements"].append(
        {"name": "spectrum", "expr": "onoise_total"}
    )
    request["measurements"].append({"name": "m", "expr": "a.spectrum * 2"})
    _, specs = _resolve(request)
    m = next(s for s in specs if s["name"] == "m")
    assert m["derived_from"] == ["a.spectrum"]
    assert "spectrum_steps" not in m


@pytest.mark.parametrize(
    "expr, match",
    [
        ("lo.spectrum.onoise_spectrum[1]", r"step 'lo' runs 'op', not 'noise'"),
        ("a.spectrum.vout[1]", r"'vout' is not a noise spectrum vector of step 'a'"),
        ("a.other.onoise_spectrum[1]", r"only three-part step reference"),
        ("zz.spectrum.onoise_spectrum[1]", r"'zz' is not an earlier step"),
    ],
)
def test_invalid_spectrum_references_are_refused(expr, match):
    request = _noise_request()
    request["analysis_steps"].insert(
        0,
        {
            "name": "lo",
            "analysis": {"kind": "op", "args": ""},
            "measurements": [{"name": "vout", "expr": "v(out)"}],
        },
    )
    request["measurements"] = [{"name": "bad", "expr": expr}]
    with pytest.raises(sim.SimError, match=match):
        _resolve(request)


# --------------------------------------------------------------------------- #
# Derived sweep windows and unreported intermediates (issue #3055)
# --------------------------------------------------------------------------- #


def _coarse_fine_request(**fine_overrides) -> dict:
    """A coarse DC sweep locates the last grid point where v(out) < 1 V
    (1.0 V, since v(out) = 2/3 * vin); a fine sweep is centred on it."""
    fine = {
        "name": "fine",
        "analysis": {
            "kind": "dc",
            "args": "VIN ${coarse.edge - 0.5} ${coarse.edge + 0.5} 0.25",
        },
        "measurements": [
            {"name": "first", "expr": "v(in)[0]"},
            {"name": "last", "expr": "vecmax(v(in))"},
        ],
    }
    fine.update(fine_overrides)
    return {
        "netlist": "body.spice",
        "analysis_steps": [
            {
                "name": "coarse",
                "analysis": {"kind": "dc", "args": "VIN 0 6 0.5"},
                "measurements": [
                    {
                        "name": "edge",
                        "expr": "vecmax(v(in) * (v(out) lt 1.0))",
                    }
                ],
            },
            fine,
        ],
        "measurements": [{"name": "span", "expr": "fine.last - fine.first"}],
    }


def test_arg_reference_becomes_a_scalar_let_read_with_dollar_ampersand():
    analysis, specs = _resolve(_coarse_fine_request())
    lines = sim_steps.control_lines(analysis, specs)
    fine = lines[lines.index("echo klt_step_begin 1 $curplot") - 3 :]
    assert fine[:5] == [
        "setplot new",
        "let klt_arg_1_0 = {$klt_plot_0}.edge - 0.5",
        "let klt_arg_1_1 = {$klt_plot_0}.edge + 0.5",
        "echo klt_step_begin 1 $curplot",
        "dc VIN $&klt_arg_1_0 $&klt_arg_1_1 0.25",
    ]
    assert analysis["steps"][1]["arg_inputs"] == ["coarse.edge"]
    # The report-facing args stay the request's own text.
    assert analysis["steps"][1]["args"].startswith("VIN ${coarse.edge")


def test_steps_without_placeholders_keep_their_exact_shape():
    analysis, _ = _resolve(_two_point_request())
    for step in analysis["steps"]:
        assert set(step) == {"name", "kind", "args", "alter"}
    assert set(analysis) == {"kind", "args", "steps"}


@pytest.mark.parametrize(
    "args,match",
    [
        ("VIN ${fine.x} 1 0.1", "not an earlier step"),
        ("VIN ${later.x} 1 0.1", "not an earlier step"),
        ("VIN ${coarse.nope} 1 0.1", "declares no measurement"),
        ("VIN ${1 + 2} 1 0.1", "references no earlier step"),
        ("VIN ${coarse.edge 1 0.1", "unterminated"),
    ],
)
def test_bad_arg_references_are_refused_naming_step_and_reference(args, match):
    request = _coarse_fine_request()
    request["analysis_steps"][1]["analysis"]["args"] = args
    request["analysis_steps"].append(
        {"name": "later", "analysis": {"kind": "op", "args": ""}}
    )
    with pytest.raises(sim.SimError, match=match) as excinfo:
        _resolve(request)
    assert "fine" in str(excinfo.value)


def test_arg_reference_to_an_intermediate_is_refused():
    request = _coarse_fine_request()
    request["analysis_steps"][0]["measurements"][0]["intermediate"] = True
    with pytest.raises(sim.SimError, match="intermediate"):
        _resolve(request)


def test_unavailable_window_input_fails_the_step_without_a_stale_run():
    request = _coarse_fine_request()
    # The upstream measurement printed nothing; ngspice still "ran" the fine
    # step (markers show a new plot), but Python must refuse its result.
    log = _step_log(steps=2, values={"fine__first": 1.0, "fine__last": 2.0})
    results, diagnostics, extra = _grade(request, log)
    by_name = {r["name"]: r for r in results}
    assert by_name["fine.first"]["value"] is None
    assert by_name["fine.first"]["status"] == "error"
    assert by_name["span"]["status"] == "error"
    assert [d["code"] for d in diagnostics].count("derived_input_unavailable") >= 1
    unavailable = next(d for d in diagnostics if "was not run against" in d["message"])
    assert "fine" in unavailable["message"] and "coarse.edge" in unavailable["message"]
    assert [s["status"] for s in extra["steps"]] == ["ok", "error"]


def test_failed_upstream_step_blocks_the_dependent_window():
    log = _step_log(
        steps=2, failed=(0,), values={"coarse__edge": 1.0, "fine__first": 1.0}
    )
    _, diagnostics, extra = _grade(_coarse_fine_request(), log)
    assert [s["status"] for s in extra["steps"]] == ["error", "error"]
    assert [d["code"] for d in diagnostics][:2] == [
        "step_failed",
        "derived_input_unavailable",
    ]


def _intermediate_request() -> dict:
    return {
        "netlist": "body.spice",
        "analysis_steps": [
            {
                "name": "sweep",
                "analysis": {"kind": "dc", "args": "VIN 0 3 0.5"},
                "measurements": [
                    {"name": "slope", "expr": "v(out)", "intermediate": True},
                    {"name": "top", "expr": "vecmax(slope)"},
                ],
            }
        ],
        "measurements": [
            {"name": "gain", "expr": "sweep.top / 3", "intermediate": True},
            {"name": "twice", "expr": "gain * 2", "limits": {"min": 1.0}},
        ],
    }


def test_intermediate_is_let_only_and_not_a_reported_measurement():
    analysis, specs = _resolve(_intermediate_request())
    assert [s["name"] for s in specs] == ["sweep.top", "twice"]
    lines = sim_steps.control_lines(analysis, specs)
    assert "let slope = v(out)" in lines
    assert "let gain = {$klt_plot_0}.top / 3" in lines
    assert not any(
        line.startswith("print") and ("slope" in line or "gain" in line)
        for line in lines
    )
    assert lines.index("let slope = v(out)") < lines.index("let top = vecmax(slope)")
    assert specs[1]["derived_from"] == ["sweep.top"]


def test_intermediate_is_graded_nowhere():
    log = _step_log(steps=1, values={"sweep__top": 2.0, "twice": 1.3333333})
    results, diagnostics, _ = _grade(_intermediate_request(), log)
    assert [r["name"] for r in results] == ["sweep.top", "twice"]
    assert diagnostics == []
    assert results[1]["status"] == "pass"


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda m: m.update(limits={"min": 0}), "cannot declare 'limits'"),
        (lambda m: m.update(plausible_range={"min": 0, "max": 1}), "plausible_range"),
        (lambda m: m.update(intermediate="yes"), "boolean"),
        (lambda m: (m.pop("expr"), m.update(spice="meas dc x max v(out)")), "'expr'"),
    ],
)
def test_invalid_intermediates_are_refused(mutate, match):
    request = _intermediate_request()
    mutate(request["analysis_steps"][0]["measurements"][0])
    with pytest.raises(sim.SimError, match=match):
        _resolve(request)


def test_spectrum_reference_is_a_valid_sole_input_of_a_derived_measurement():
    # No `<step>.<meas>` reference at all: the spectrum read alone qualifies.
    request = _noise_request()
    request["measurements"] = [{"name": "s", "expr": "b.spectrum.inoise_spectrum[0]"}]
    _, specs = _resolve(request)
    assert specs[-1]["spectrum_steps"] == ["b"]


def test_failed_noise_step_never_grades_its_spectrum_dependents():
    request = _noise_request()
    log = _step_log(
        steps=2,
        failed=(1,),
        values={"a__tot": 1.0, "a_spot": 1.0, "b_spot": 9.0},
    )
    results, diagnostics, _ = _grade(request, log)
    by_name = {r["name"]: r for r in results}
    assert by_name["a_spot"]["value"] == 1.0
    assert by_name["b_spot"]["value"] is None
    assert by_name["b_spot"]["status"] == "error"
    unavailable = [d for d in diagnostics if d["code"] == "derived_input_unavailable"]
    assert len(unavailable) == 1
    assert "b_spot" in unavailable[0]["message"]
    assert "b.spectrum" in unavailable[0]["message"]


@_SKIP_NO_NGSPICE
def test_real_ngspice_two_noise_steps_read_their_own_spectrum(tmp_path):
    _write_body(tmp_path, _NOISE_BODY)
    request = _noise_request()
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    corner = report["corners"][0]
    assert corner["status"] == "pass", corner["diagnostics"]
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert values["a_spot"] is not None
    assert values["b_spot"] is not None
    # Flat thermal noise of R1 || R2 (27 C): each step reads its own solve,
    # never the previous step's.
    for step, r2 in (("a", 2000.0), ("b", 8000.0)):
        r_par = 1000.0 * r2 / (1000.0 + r2)
        expected = (4 * 1.380649e-23 * 300.15 * r_par) ** 0.5
        assert values[f"{step}_spot"] == pytest.approx(expected, rel=1e-3)


@_SKIP_NO_NGSPICE
def test_real_ngspice_reset_step_drops_earlier_alter_keeps_supply(tmp_path):
    """Single corner, run locally: the `ILOAD` alter of step 1 must not leak
    into the `reset` step 2, while the corner's `supply_v` still applies and
    the derived expression still reads both steps' plots."""
    _write_body(tmp_path)
    request = _reset_request(corners={"supply_v": {"VIN": [3.3]}})
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    corner = report["corners"][0]
    assert report["status"] == "pass", corner["diagnostics"]
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    # 3.3 V divider, no load: 2.2 V. With 1 mA drawn from `out`: 3.3 * 2/3 -
    # 1e-3 * (1k || 2k) = 2.2 - 0.6667.
    assert values["loaded.vout"] == pytest.approx(2.2 - 2 / 3, rel=1e-6)
    assert values["fresh.vout"] == pytest.approx(2.2, rel=1e-6)
    assert values["drop"] == pytest.approx(2 / 3, rel=1e-6)
    assert [s["status"] for s in corner["steps"]] == ["ok", "ok"]


def test_intermediate_is_refused_on_the_scalar_analysis_form():
    request = {
        "analysis": {"kind": "op", "args": ""},
        "measurements": [{"name": "v", "expr": "v(out)", "intermediate": True}],
    }
    with pytest.raises(sim.SimError, match="only supported with analysis_steps"):
        sim._resolve_analysis_and_measurements(request)


def test_remote_forwards_placeholders_and_intermediates_verbatim():
    request = _coarse_fine_request()
    request["analysis_steps"][0]["measurements"].append(
        {"name": "tmp", "expr": "v(out)", "intermediate": True}
    )
    forwarded = json.loads(json.dumps(request))
    assert forwarded["analysis_steps"][1]["analysis"]["args"].startswith("VIN ${")
    # The worker re-resolves the same document into the same deck text.
    a1, s1 = _resolve(forwarded)
    a2, s2 = _resolve(request)
    assert sim_steps.control_lines(a1, s1) == sim_steps.control_lines(a2, s2)


@_SKIP_NO_NGSPICE
def test_real_ngspice_fine_sweep_is_centred_on_the_coarse_result(tmp_path):
    _write_body(tmp_path)
    report = sim.run_sim(str(_write_request(tmp_path, _coarse_fine_request())))
    corner = report["corners"][0]
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert values["coarse.edge"] == pytest.approx(1.0)
    assert values["fine.first"] == pytest.approx(0.5)
    assert values["fine.last"] == pytest.approx(1.5)
    assert values["span"] == pytest.approx(1.0)
    assert [s["status"] for s in corner["steps"]] == ["ok", "ok"]


@_SKIP_NO_NGSPICE
def test_real_ngspice_intermediate_feeds_later_exprs_unreported(tmp_path):
    _write_body(tmp_path)
    report = sim.run_sim(str(_write_request(tmp_path, _intermediate_request())))
    corner = report["corners"][0]
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert sorted(values) == ["sweep.top", "twice"]
    assert values["sweep.top"] == pytest.approx(2.0)
    assert values["twice"] == pytest.approx(4 / 3)
    assert not any("slope" in d["message"] for d in corner["diagnostics"])


_TRAN = '(kind "tran")'


@pytest.mark.parametrize(
    "key,value,tail",
    [
        ("crossings", {}, f"{_TRAN} for a threshold-crossing series"),
        ("energy", {}, f"{_TRAN} for a source-energy measurement"),
        ("stress", {}, f"{_TRAN} for a windowed node-pair voltage stress"),
        ("search", {}, "for a bisection/threshold-search measurement"),
        ("optional", True, "for a measurement that may legitimately be absent"),
        ("cycle_mean", {}, f"{_TRAN} for a cycle-aligned mean"),
        ("eye", {}, f"{_TRAN} for an eye measurement"),
        ("noise", {}, '(kind "noise") for a noise-spectrum measurement'),
    ],
)
def test_refuse_unsupported_message_is_stable(key, value, tail):
    # The per-key text is part of the JSON error contract.
    with pytest.raises(sim.SimError) as exc:
        sim_steps._refuse_unsupported({"name": "m", key: value})
    assert str(exc.value) == (
        f"measurement 'm': '{key}' is not supported with analysis_steps -- "
        f"use the scalar analysis {tail}"
    )


def test_refuse_unsupported_ignores_optional_false_and_absent_keys():
    sim_steps._refuse_unsupported({"name": "m", "optional": False})
    sim_steps._refuse_unsupported({"name": "m", "expr": "v(out)"})
