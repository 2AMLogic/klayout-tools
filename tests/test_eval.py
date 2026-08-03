"""Tests for `klt eval` and the `klayout_tools.eval` library.

Three tiers, mirroring `tests/test_drc.py`/`tests/test_lvs.py`:

- **Unit tests** exercise descriptor/candidate loading, `${name}` candidate
  substitution, gate composition (1-4 named checks), objective/metric
  extraction, and the `layout-metrics`-needs-a-threshold rule -- directly
  against `run_eval`, mostly with cheap `drc`/`layout-metrics` fixtures (no
  `ngspice` dependency).
- **Integration tests** run `klt eval` end to end against a known-clean
  sky130-deck fixture (`valid: true`) and one with a seeded DRC violation
  (`valid: false`, the failing gate cited by name), through both the library
  function and the `main()` CLI entry point.
- A **4-gate composition test** (`drc` + `lvs` + `sim` + `layout-metrics` in
  one descriptor) is `@_SKIP_NO_NGSPICE`-gated, mirroring `tests/test_sim.py`'s
  own real-`ngspice` integration tier.
"""

from __future__ import annotations

import json
import shutil

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.eval import EvalError, run_eval

HAVE_NGSPICE = shutil.which("ngspice") is not None
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

# poly.width.1 (sky130 deck): minimum poly width is 150 dbu (0.15 um).
_POLY_WIDTH_THRESHOLD_DBU = 150

_INVERTER_SPICE = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.65U L=0.15U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""

_INVERTER_SPICE_MISMATCH = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.90U L=0.15U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""


# --------------------------------------------------------------------------- #
# fixture helpers
# --------------------------------------------------------------------------- #


def _make_layout(clean: bool) -> kdb.Layout:
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    width = 200 if clean else 50  # 200 >= 150 threshold; 50 < 150
    top.shapes(poly).insert(kdb.Box(0, 0, width, 2000))
    return layout


def _write_block(tmp_path, name: str, clean: bool):
    """A block directory (`klt layout-metrics` shape) containing one
    `layout.gds`, so `layout-metrics` checks have something to report on."""
    block_dir = tmp_path / name
    block_dir.mkdir()
    _make_layout(clean).write(str(block_dir / "layout.gds"))
    return block_dir


def _write_descriptor(tmp_path, descriptor: dict, name: str = "descriptor.json"):
    path = tmp_path / name
    path.write_text(json.dumps(descriptor))
    return str(path)


_MINIMAL_DESCRIPTOR = {
    "gates": [{"check": "drc", "args": {"file": "${layout}", "deck": "sky130"}}],
    "objective": {
        "check": "layout-metrics",
        "metric": "cell_count",
        "polarity": "minimize",
        "args": {"block": "${block}"},
    },
}


def _candidate_for(block_dir) -> str:
    return json.dumps(
        {"layout": str(block_dir / "layout.gds"), "block": str(block_dir)}
    )


# --------------------------------------------------------------------------- #
# descriptor / candidate loading
# --------------------------------------------------------------------------- #


def test_descriptor_missing_file_raises(tmp_path):
    with pytest.raises(EvalError, match="descriptor"):
        run_eval(str(tmp_path / "nope.json"))


def test_descriptor_malformed_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("this is not json {{{")
    with pytest.raises(EvalError, match="not valid JSON"):
        run_eval(str(path))


def test_descriptor_non_object_raises(tmp_path):
    path = tmp_path / "array.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(EvalError, match="JSON object"):
        run_eval(str(path))


def test_descriptor_missing_gates_raises(tmp_path):
    descriptor = {"objective": {"check": "layout-metrics", "metric": "cell_count"}}
    path = _write_descriptor(tmp_path, descriptor)
    with pytest.raises(EvalError, match="gates"):
        run_eval(path)


def test_descriptor_missing_objective_raises(tmp_path):
    descriptor = {
        "gates": [{"check": "drc", "args": {"file": "x.gds", "deck": "sky130"}}]
    }
    path = _write_descriptor(tmp_path, descriptor)
    with pytest.raises(EvalError, match="objective"):
        run_eval(path)


def test_descriptor_inline_json_string():
    block_dir_descriptor = json.dumps(
        {
            "gates": [
                {
                    "check": "layout-metrics",
                    "args": {"block": "${block}"},
                    "threshold": {"metric": "cell_count", "max": 100},
                }
            ],
            "objective": {
                "check": "layout-metrics",
                "metric": "cell_count",
                "args": {"block": "${block}"},
            },
        }
    )
    # Descriptor parses even without a resolvable candidate yet -- resolution
    # happens per-check, not at load time.
    with pytest.raises(EvalError, match="candidate"):
        run_eval(block_dir_descriptor)


def test_candidate_missing_key_raises(tmp_path):
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)
    # candidate provides "layout" but not "block" (the objective's arg).
    candidate = json.dumps({"layout": str(block_dir / "layout.gds")})
    with pytest.raises(EvalError, match="candidate key"):
        run_eval(descriptor_path, candidate)


def test_candidate_non_object_raises(tmp_path):
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)
    with pytest.raises(EvalError, match="candidate must be a JSON object"):
        run_eval(descriptor_path, "[1, 2]")


def test_unknown_check_raises(tmp_path):
    descriptor = {
        "gates": [{"check": "not-a-real-check", "args": {}}],
        "objective": {"check": "layout-metrics", "metric": "cell_count", "args": {}},
    }
    path = _write_descriptor(tmp_path, descriptor)
    with pytest.raises(EvalError, match="unknown check"):
        run_eval(path)


# --------------------------------------------------------------------------- #
# gate composition
# --------------------------------------------------------------------------- #


def test_single_gate_clean_is_valid(tmp_path):
    block_dir = _write_block(tmp_path, "clean_block", clean=True)
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)

    report = run_eval(descriptor_path, _candidate_for(block_dir))

    assert report["schema_version"] == 1
    assert report["valid"] is True
    (gate,) = report["gates"]
    assert gate == {
        "check": "drc",
        "name": "drc",
        "status": "pass",
        "exit_code": 0,
        "count": 0,
    }


def test_single_gate_violation_is_invalid_and_cites_gate(tmp_path):
    block_dir = _write_block(tmp_path, "violation_block", clean=False)
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)

    report = run_eval(descriptor_path, _candidate_for(block_dir))

    assert report["valid"] is False
    (gate,) = report["gates"]
    assert gate["check"] == "drc"
    assert gate["name"] == "drc"
    assert gate["status"] == "fail"
    assert gate["exit_code"] == 3
    assert gate["count"] == 1


def test_two_gates_drc_and_layout_metrics_threshold(tmp_path):
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor = {
        "gates": [
            {"check": "drc", "args": {"file": "${layout}", "deck": "sky130"}},
            {
                "check": "layout-metrics",
                "name": "cell_count_ceiling",
                "args": {"block": "${block}"},
                "threshold": {"metric": "cell_count", "max": 5},
            },
        ],
        "objective": {
            "check": "layout-metrics",
            "metric": "layer_count",
            "polarity": "maximize",
            "args": {"block": "${block}"},
        },
    }
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path, _candidate_for(block_dir))

    assert report["valid"] is True
    assert len(report["gates"]) == 2
    drc_gate, metrics_gate = report["gates"]
    assert drc_gate["check"] == "drc"
    assert metrics_gate["check"] == "layout-metrics"
    assert metrics_gate["name"] == "cell_count_ceiling"
    assert metrics_gate["status"] == "pass"
    assert metrics_gate["exit_code"] == 0
    assert metrics_gate["count"] == 1  # cell_count == 1, threshold extracted value
    assert report["objective"] == {
        "name": "layer_count",
        "value": 1,
        "polarity": "maximize",
    }


def test_layout_metrics_gate_without_threshold_raises(tmp_path):
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor = {
        "gates": [{"check": "layout-metrics", "args": {"block": "${block}"}}],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "args": {"block": "${block}"},
        },
    }
    path = _write_descriptor(tmp_path, descriptor)

    with pytest.raises(EvalError, match="threshold"):
        run_eval(path, _candidate_for(block_dir))


def test_drc_gate_with_threshold_override(tmp_path):
    """A `drc` gate can override its default any-violation-fails semantics
    with an explicit violation-count ceiling."""
    block_dir = _write_block(tmp_path, "violation_block", clean=False)
    descriptor = {
        "gates": [
            {
                "check": "drc",
                "args": {"file": "${layout}", "deck": "sky130"},
                "threshold": {"metric": "violation_count", "max": 5},
            }
        ],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "args": {"block": "${block}"},
        },
    }
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path, _candidate_for(block_dir))

    assert (
        report["valid"] is True
    )  # 1 violation <= max 5, threshold overrides drc's own fail
    (gate,) = report["gates"]
    assert gate["status"] == "pass"
    assert gate["exit_code"] == 0
    assert gate["count"] == 1


# --------------------------------------------------------------------------- #
# objective extraction / polarity
# --------------------------------------------------------------------------- #


def test_objective_defaults_to_minimize(tmp_path):
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor = dict(_MINIMAL_DESCRIPTOR)
    descriptor["objective"] = {
        "check": "layout-metrics",
        "metric": "cell_count",
        "args": {"block": "${block}"},
    }
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path, _candidate_for(block_dir))
    assert report["objective"]["polarity"] == "minimize"


def test_objective_maximize_polarity(tmp_path):
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor = dict(_MINIMAL_DESCRIPTOR)
    descriptor["objective"] = {
        "check": "layout-metrics",
        "metric": "cell_count",
        "polarity": "maximize",
        "name": "cell_count",
        "args": {"block": "${block}"},
    }
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path, _candidate_for(block_dir))
    assert report["objective"] == {
        "name": "cell_count",
        "value": 1,
        "polarity": "maximize",
    }


def test_objective_invalid_polarity_raises(tmp_path):
    descriptor = dict(_MINIMAL_DESCRIPTOR)
    descriptor["objective"] = {
        "check": "layout-metrics",
        "metric": "cell_count",
        "polarity": "sideways",
        "args": {"block": "x"},
    }
    path = _write_descriptor(tmp_path, descriptor)
    with pytest.raises(EvalError, match="polarity"):
        run_eval(path)


def test_objective_dotted_metric_path_extraction(tmp_path):
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor = dict(_MINIMAL_DESCRIPTOR)
    descriptor["objective"] = {
        "check": "drc",
        "metric": "coverage.deck_layers.0",
        "args": {"file": "${layout}", "deck": "sky130"},
    }
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path, _candidate_for(block_dir))
    assert isinstance(report["objective"]["value"], str)


def test_objective_bad_metric_path_raises(tmp_path):
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor = dict(_MINIMAL_DESCRIPTOR)
    descriptor["objective"] = {
        "check": "layout-metrics",
        "metric": "no_such_field",
        "args": {"block": "${block}"},
    }
    path = _write_descriptor(tmp_path, descriptor)

    with pytest.raises(EvalError, match="no_such_field"):
        run_eval(path, _candidate_for(block_dir))


# --------------------------------------------------------------------------- #
# `metrics` bag
# --------------------------------------------------------------------------- #


def test_metrics_bag_scalar_extraction(tmp_path):
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor = dict(_MINIMAL_DESCRIPTOR)
    descriptor["metrics"] = [
        {
            "name": "layers",
            "check": "layout-metrics",
            "metric": "layer_count",
            "args": {"block": "${block}"},
        }
    ]
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path, _candidate_for(block_dir))
    assert report["metrics"] == {"layers": 1}


def test_metrics_bag_whole_report_when_metric_omitted(tmp_path):
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor = dict(_MINIMAL_DESCRIPTOR)
    descriptor["metrics"] = [
        {
            "name": "layout_metrics",
            "check": "layout-metrics",
            "args": {"block": "${block}"},
        }
    ]
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path, _candidate_for(block_dir))
    assert report["metrics"]["layout_metrics"]["cell_count"] == 1
    assert report["metrics"]["layout_metrics"]["slug"] == "block"


def test_metrics_bag_empty_when_not_declared(tmp_path):
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)

    report = run_eval(descriptor_path, _candidate_for(block_dir))
    assert report["metrics"] == {}


def test_repeated_check_is_cached_not_rerun(tmp_path, monkeypatch):
    """The same check+args (e.g. a gate and the objective both naming
    `layout-metrics` against the same block) is invoked once, not twice."""
    block_dir = _write_block(tmp_path, "block", clean=True)
    descriptor = {
        "gates": [
            {
                "check": "layout-metrics",
                "args": {"block": "${block}"},
                "threshold": {"metric": "cell_count", "max": 100},
            }
        ],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "args": {"block": "${block}"},
        },
    }
    path = _write_descriptor(tmp_path, descriptor)

    calls = []
    import klayout_tools.eval as eval_module

    original = eval_module.layout_metrics_report

    def _counting(*a, **kw):
        calls.append((a, kw))
        return original(*a, **kw)

    monkeypatch.setattr(eval_module, "layout_metrics_report", _counting)

    run_eval(path, _candidate_for(block_dir))
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# lvs gate
# --------------------------------------------------------------------------- #


def test_lvs_gate_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "layout.spice").write_text(_INVERTER_SPICE)
    (tmp_path / "ref.spice").write_text(_INVERTER_SPICE)

    descriptor = {
        "gates": [
            {
                "check": "lvs",
                "args": {
                    "request": {
                        "layout": {"netlist": "layout.spice", "top": "inv"},
                        "reference": {"netlist": "ref.spice", "top": "inv"},
                    }
                },
            }
        ],
        "objective": {
            "check": "layout-metrics",
            "metric": "status",
            "args": {"block": str(tmp_path)},
        },
    }
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path)
    (gate,) = report["gates"]
    assert gate["check"] == "lvs"
    assert gate["status"] == "pass"
    assert gate["exit_code"] == 0
    assert gate["count"] == 0


def test_lvs_gate_mismatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "layout.spice").write_text(_INVERTER_SPICE)
    (tmp_path / "ref.spice").write_text(_INVERTER_SPICE_MISMATCH)

    descriptor = {
        "gates": [
            {
                "check": "lvs",
                "args": {
                    "request": {
                        "layout": {"netlist": "layout.spice", "top": "inv"},
                        "reference": {"netlist": "ref.spice", "top": "inv"},
                    }
                },
            }
        ],
        "objective": {
            "check": "layout-metrics",
            "metric": "status",
            "args": {"block": str(tmp_path)},
        },
    }
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path)
    (gate,) = report["gates"]
    assert gate["check"] == "lvs"
    assert gate["status"] == "fail"
    assert gate["exit_code"] == 3
    assert report["valid"] is False


# --------------------------------------------------------------------------- #
# sim gate (real ngspice, gated -- and the 4-gate composition test)
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_sim_gate_pass(tmp_path):
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
    }
    (tmp_path / "request.json").write_text(json.dumps(request))

    descriptor = {
        "gates": [{"check": "sim", "args": {"request": "request.json"}}],
        "objective": {
            "check": "layout-metrics",
            "metric": "status",
            "args": {"block": str(tmp_path)},
        },
    }
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path)
    (gate,) = report["gates"]
    assert gate["check"] == "sim"
    assert gate["status"] == "pass"
    assert gate["exit_code"] == 0


@_SKIP_NO_NGSPICE
def test_four_gate_composition(tmp_path, monkeypatch):
    """`gates` naming all four checks (`drc`, `lvs`, `sim`,
    `layout-metrics`) in one descriptor -- the test plan's "gate composition
    with 1-4 named checks" upper bound."""
    monkeypatch.chdir(tmp_path)
    block_dir = _write_block(tmp_path, "block", clean=True)

    (tmp_path / "layout.spice").write_text(_INVERTER_SPICE)
    (tmp_path / "ref.spice").write_text(_INVERTER_SPICE)
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    (tmp_path / "sim_request.json").write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "measurements": [
                    {
                        "name": "vout",
                        "spice": ".meas tran vout FIND v(out) AT=1u",
                        "limits": {"min": 0.5},
                    }
                ],
            }
        )
    )

    descriptor = {
        "gates": [
            {"check": "drc", "args": {"file": "${layout}", "deck": "sky130"}},
            {
                "check": "lvs",
                "args": {
                    "request": {
                        "layout": {"netlist": "layout.spice", "top": "inv"},
                        "reference": {"netlist": "ref.spice", "top": "inv"},
                    }
                },
            },
            {"check": "sim", "args": {"request": "sim_request.json"}},
            {
                "check": "layout-metrics",
                "args": {"block": "${block}"},
                "threshold": {"metric": "cell_count", "max": 10},
            },
        ],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "polarity": "minimize",
            "args": {"block": "${block}"},
        },
    }
    path = _write_descriptor(tmp_path, descriptor)

    report = run_eval(path, _candidate_for(block_dir))

    assert report["valid"] is True
    assert [g["check"] for g in report["gates"]] == [
        "drc",
        "lvs",
        "sim",
        "layout-metrics",
    ]
    assert all(g["status"] == "pass" for g in report["gates"])


# --------------------------------------------------------------------------- #
# CLI: exit codes, --format parity
# --------------------------------------------------------------------------- #


def test_exit_code_valid(tmp_path):
    block_dir = _write_block(tmp_path, "clean_block", clean=True)
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)

    assert (
        main(
            [
                "eval",
                descriptor_path,
                "--candidate",
                _candidate_for(block_dir),
                "--format",
                "json",
            ]
        )
        == 0
    )


def test_exit_code_invalid(tmp_path):
    block_dir = _write_block(tmp_path, "violation_block", clean=False)
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)

    assert (
        main(
            [
                "eval",
                descriptor_path,
                "--candidate",
                _candidate_for(block_dir),
                "--format",
                "json",
            ]
        )
        == 3
    )


def test_exit_code_broken_invocation_missing_descriptor(tmp_path, capsys):
    assert main(["eval", str(tmp_path / "nope.json"), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "eval"


def test_exit_code_broken_invocation_unresolvable_candidate(tmp_path, capsys):
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)
    assert (
        main(
            [
                "eval",
                descriptor_path,
                "--candidate",
                "{}",
                "--format",
                "json",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "candidate" in captured.err


def test_exit_code_never_confuses_crash_with_invalid(tmp_path):
    """The acceptance criterion this issue exists for: exit 1 (crash) and
    exit 3 (ran, invalid) must never collide."""
    block_dir = _write_block(tmp_path, "violation_block", clean=False)
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)

    invalid_exit = main(
        [
            "eval",
            descriptor_path,
            "--candidate",
            _candidate_for(block_dir),
            "--format",
            "json",
        ]
    )
    crash_exit = main(["eval", str(tmp_path / "nope.json"), "--format", "json"])

    assert invalid_exit == 3
    assert crash_exit == 1
    assert invalid_exit != crash_exit


def test_json_format_contract(tmp_path, capsys):
    block_dir = _write_block(tmp_path, "clean_block", clean=True)
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)

    assert (
        main(
            [
                "eval",
                descriptor_path,
                "--candidate",
                _candidate_for(block_dir),
                "--format",
                "json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "valid",
        "gates",
        "objective",
        "metrics",
    }
    assert data["schema_version"] == 1
    assert data["valid"] is True
    assert isinstance(data["gates"], list)
    assert set(data["objective"].keys()) == {"name", "value", "polarity"}
    assert isinstance(data["metrics"], dict)


def test_default_format_is_text(tmp_path, capsys):
    block_dir = _write_block(tmp_path, "clean_block", clean=True)
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)

    assert (
        main(["eval", descriptor_path, "--candidate", _candidate_for(block_dir)]) == 0
    )
    out = capsys.readouterr().out
    assert "valid: True" in out
    assert "gates:" in out
    assert "objective:" in out
    # Not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_text_format_reports_invalid_gate(tmp_path, capsys):
    block_dir = _write_block(tmp_path, "violation_block", clean=False)
    descriptor_path = _write_descriptor(tmp_path, _MINIMAL_DESCRIPTOR)

    assert (
        main(["eval", descriptor_path, "--candidate", _candidate_for(block_dir)]) == 3
    )
    out = capsys.readouterr().out
    assert "valid: False" in out
    assert "drc" in out
    assert "status=fail" in out
