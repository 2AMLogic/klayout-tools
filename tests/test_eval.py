"""Tests for `klt eval` and the `klayout_tools.eval` library (issue #387).

Three tiers:

- **Descriptor validation** unit tests exercise malformed/missing-field
  descriptors and unknown check names -- pure dict/JSON checks, no engine
  involved.
- **`drc`/`layout-metrics` integration tests** run the real KLayout-backed
  `run_drc`/`layout_metrics_report` engines against small, programmatically
  generated layouts (mirroring `tests/test_drc.py`/`tests/test_layout_metrics.py`)
  -- no external PDK/spice dependency, so these always run everywhere.
- **`lvs`/`sim` gate-mapping unit tests** monkeypatch
  `klayout_tools.eval.run_lvs`/`run_sim` directly (mirroring
  `tests/test_sim.py`'s subprocess-stubbing convention) to exercise this
  module's own status/count/exit_code derivation without needing a real
  `netgen`/`ngspice` binary on the test machine.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.eval import EvalError, run_eval
from klayout_tools.lvs import LvsError
from klayout_tools.sim import SimError

# poly.width.1 (sky130 deck): minimum poly width is 150 dbu (0.15 um).
_POLY_WIDTH_THRESHOLD_DBU = 150


def _make_violation_layout() -> kdb.Layout:
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    top.shapes(poly).insert(kdb.Box(0, 0, 50, 2000))
    return layout


def _make_clean_layout() -> kdb.Layout:
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    top.shapes(poly).insert(kdb.Box(0, 0, 200, 2000))
    return layout


# --------------------------------------------------------------------------- #
# Descriptor validation
# --------------------------------------------------------------------------- #


def test_descriptor_must_be_json_object():
    with pytest.raises(EvalError, match="must be a JSON object"):
        run_eval("[1, 2, 3]")


def test_descriptor_requires_checks():
    with pytest.raises(EvalError, match="non-empty 'checks'"):
        run_eval(json.dumps({"gates": [], "objective": {}}))


def test_descriptor_requires_gates_array():
    descriptor = {"checks": {"drc": {}}, "objective": {}}
    with pytest.raises(EvalError, match="'gates' array"):
        run_eval(json.dumps(descriptor))


def test_descriptor_requires_objective():
    descriptor = {"checks": {"drc": {}}, "gates": []}
    with pytest.raises(EvalError, match="'objective' object"):
        run_eval(json.dumps(descriptor))


def test_descriptor_objective_missing_field():
    descriptor = {
        "checks": {"drc": {}},
        "gates": [],
        "objective": {"check": "drc", "metric": "violation_count"},
    }
    with pytest.raises(EvalError, match="missing required field 'polarity'"):
        run_eval(json.dumps(descriptor))


def test_descriptor_objective_bad_polarity():
    descriptor = {
        "checks": {"drc": {}},
        "gates": [],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "sideways",
        },
    }
    with pytest.raises(EvalError, match="'minimize' or 'maximize'"):
        run_eval(json.dumps(descriptor))


def test_unknown_gate_check_name():
    descriptor = {
        "checks": {"synthesis": {}},
        "gates": ["synthesis"],
        "objective": {"check": "synthesis", "metric": "x", "polarity": "minimize"},
    }
    with pytest.raises(EvalError, match="unknown check 'synthesis'"):
        run_eval(json.dumps(descriptor))


def test_gate_references_undeclared_check():
    descriptor = {
        "checks": {"drc": {"file": "x.gds", "deck": "sky130"}},
        "gates": ["lvs"],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "minimize",
        },
    }
    with pytest.raises(EvalError, match="not declared in descriptor 'checks'"):
        run_eval(json.dumps(descriptor))


def test_objective_references_unknown_check():
    descriptor = {
        "checks": {"drc": {"file": "x.gds", "deck": "sky130"}},
        "gates": [],
        "objective": {"check": "warp-drive", "metric": "x", "polarity": "minimize"},
    }
    with pytest.raises(EvalError, match="objective references unknown check"):
        run_eval(json.dumps(descriptor))


def test_missing_descriptor_file():
    with pytest.raises(EvalError, match="neither an existing file"):
        run_eval("{not valid json and not a file}")


def test_descriptor_from_missing_file_path_like_string(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(EvalError):
        run_eval(str(missing))


def test_descriptor_directory_path(tmp_path):
    with pytest.raises(EvalError, match="not a file"):
        run_eval(str(tmp_path))


# --------------------------------------------------------------------------- #
# Candidate substitution
# --------------------------------------------------------------------------- #


def test_candidate_placeholder_missing_value(tmp_path):
    descriptor = {
        "checks": {"drc": {"file": "{layout}", "deck": "sky130"}},
        "gates": ["drc"],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "minimize",
        },
    }
    with pytest.raises(EvalError, match="candidate key 'layout'"):
        run_eval(json.dumps(descriptor))


def test_candidate_placeholder_substituted(tmp_path):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    descriptor = {
        "checks": {"drc": {"file": "{layout}", "deck": "sky130"}},
        "gates": ["drc"],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "minimize",
        },
    }
    report = run_eval(json.dumps(descriptor), candidate={"layout": str(path)})
    assert report["valid"] is True
    assert report["candidate"] == {"layout": str(path)}


# --------------------------------------------------------------------------- #
# drc + layout-metrics integration (real KLayout engine, no PDK download)
# --------------------------------------------------------------------------- #


def test_valid_true_when_all_gates_pass(tmp_path):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    descriptor = {
        "checks": {
            "drc": {"file": str(path), "deck": "sky130"},
            "layout-metrics": {"block": str(tmp_path)},
        },
        "gates": ["drc"],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "polarity": "minimize",
        },
    }
    report = run_eval(json.dumps(descriptor))

    assert report["schema_version"] == 1
    assert report["valid"] is True
    assert report["gates"] == [
        {"check": "drc", "status": "pass", "exit_code": 0, "count": 0}
    ]
    assert report["objective"] == {
        "check": "layout-metrics",
        "name": "cell_count",
        "value": 1,
        "polarity": "minimize",
    }
    assert "drc" in report["metrics"]
    assert "layout-metrics" in report["metrics"]


def test_valid_false_when_a_gate_fails(tmp_path):
    path = tmp_path / "violation.gds"
    _make_violation_layout().write(str(path))
    descriptor = {
        "checks": {
            "drc": {"file": str(path), "deck": "sky130"},
            "layout-metrics": {"block": str(tmp_path)},
        },
        "gates": ["drc"],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "polarity": "minimize",
        },
    }
    report = run_eval(json.dumps(descriptor))

    assert report["valid"] is False
    assert report["gates"] == [
        {"check": "drc", "status": "fail", "exit_code": 3, "count": 1}
    ]
    # Objective is still reported when computable, even though valid is
    # false -- per the acceptance criteria, the caller must not use it to
    # rank this candidate, but the number itself is not withheld.
    assert report["objective"]["value"] == 1


def test_drc_check_raising_is_a_run_failure_not_a_gate_failure(tmp_path):
    """An unresolvable file (`DrcError`) must raise `EvalError` (exit 1
    territory), never be reported as a passing/failing gate -- an optimizer
    must never mistake a crash for a low score (issue #387's core ask)."""
    descriptor = {
        "checks": {"drc": {"file": str(tmp_path / "missing.gds"), "deck": "sky130"}},
        "gates": ["drc"],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "minimize",
        },
    }
    with pytest.raises(EvalError, match="drc check failed to run"):
        run_eval(json.dumps(descriptor))


def test_check_invoked_once_and_shared_between_gate_and_objective(
    tmp_path, monkeypatch
):
    """A check named by both a gate and the objective is only invoked once
    -- confirmed via a call-count spy around the real `run_drc`."""
    import klayout_tools.eval as eval_mod

    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))

    calls = []
    original = eval_mod.run_drc

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(eval_mod, "run_drc", spy)

    descriptor = {
        "checks": {"drc": {"file": str(path), "deck": "sky130"}},
        "gates": ["drc"],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "minimize",
        },
    }
    report = run_eval(json.dumps(descriptor))

    assert len(calls) == 1
    assert report["objective"]["value"] == 0


# --------------------------------------------------------------------------- #
# layout-metrics as a gate (threshold-derived, no native exit code)
# --------------------------------------------------------------------------- #


def test_layout_metrics_gate_requires_threshold(tmp_path):
    descriptor = {
        "checks": {"layout-metrics": {"block": str(tmp_path)}},
        "gates": ["layout-metrics"],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "polarity": "minimize",
        },
    }
    with pytest.raises(EvalError, match="no native pass/fail outcome"):
        run_eval(json.dumps(descriptor))


def test_layout_metrics_gate_threshold_pass(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    layout.write(str(tmp_path / "layout.gds"))

    descriptor = {
        "checks": {
            "layout-metrics": {
                "block": str(tmp_path),
                "threshold": {"metric": "cell_count", "max": 10},
            }
        },
        "gates": ["layout-metrics"],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "polarity": "minimize",
        },
    }
    report = run_eval(json.dumps(descriptor))

    assert report["valid"] is True
    assert report["gates"] == [
        {"check": "layout-metrics", "status": "pass", "exit_code": None, "count": 0}
    ]


def test_layout_metrics_gate_threshold_fail(tmp_path):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    for i in range(5):
        top.insert(
            kdb.CellInstArray(layout.create_cell(f"SUB{i}").cell_index(), kdb.Trans())
        )
    layout.write(str(tmp_path / "layout.gds"))

    descriptor = {
        "checks": {
            "layout-metrics": {
                "block": str(tmp_path),
                "threshold": {"metric": "cell_count", "max": 3},
            }
        },
        "gates": ["layout-metrics"],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "polarity": "minimize",
        },
    }
    report = run_eval(json.dumps(descriptor))

    assert report["valid"] is False
    gate = report["gates"][0]
    assert gate["status"] == "fail"
    assert gate["exit_code"] is None
    assert gate["count"] == 1


def test_threshold_metric_not_numeric(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    layout.write(str(tmp_path / "layout.gds"))

    descriptor = {
        "checks": {
            "layout-metrics": {
                "block": str(tmp_path),
                "threshold": {"metric": "status", "max": 10},
            }
        },
        "gates": ["layout-metrics"],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "polarity": "minimize",
        },
    }
    with pytest.raises(EvalError, match="not numeric"):
        run_eval(json.dumps(descriptor))


def test_objective_metric_missing_reports_null_not_error(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    layout.write(str(tmp_path / "layout.gds"))

    descriptor = {
        "checks": {"layout-metrics": {"block": str(tmp_path)}},
        "gates": [],
        "objective": {
            "check": "layout-metrics",
            "metric": "does_not_exist",
            "polarity": "minimize",
        },
    }
    report = run_eval(json.dumps(descriptor))
    assert report["valid"] is True
    assert report["objective"]["value"] is None


# --------------------------------------------------------------------------- #
# lvs/sim gate mapping (monkeypatched -- no real netgen/ngspice needed)
# --------------------------------------------------------------------------- #


def test_lvs_gate_pass(monkeypatch):
    import klayout_tools.eval as eval_mod

    monkeypatch.setattr(
        eval_mod,
        "run_lvs",
        lambda request: {"status": "match", "mismatch_count": 0},
    )
    descriptor = {
        "checks": {"lvs": {"request": "req.json"}},
        "gates": ["lvs"],
        "objective": {
            "check": "lvs",
            "metric": "mismatch_count",
            "polarity": "minimize",
        },
    }
    report = run_eval(json.dumps(descriptor))
    assert report["valid"] is True
    assert report["gates"] == [
        {"check": "lvs", "status": "pass", "exit_code": 0, "count": 0}
    ]


def test_lvs_gate_fail(monkeypatch):
    import klayout_tools.eval as eval_mod

    monkeypatch.setattr(
        eval_mod,
        "run_lvs",
        lambda request: {"status": "mismatch", "mismatch_count": 2},
    )
    descriptor = {
        "checks": {"lvs": {"request": "req.json"}},
        "gates": ["lvs"],
        "objective": {
            "check": "lvs",
            "metric": "mismatch_count",
            "polarity": "minimize",
        },
    }
    report = run_eval(json.dumps(descriptor))
    assert report["valid"] is False
    assert report["gates"] == [
        {"check": "lvs", "status": "fail", "exit_code": 3, "count": 2}
    ]


def test_lvs_error_propagates_as_eval_error(monkeypatch):
    import klayout_tools.eval as eval_mod

    def boom(request):
        raise LvsError("unresolvable reference netlist")

    monkeypatch.setattr(eval_mod, "run_lvs", boom)
    descriptor = {
        "checks": {"lvs": {"request": "req.json"}},
        "gates": ["lvs"],
        "objective": {
            "check": "lvs",
            "metric": "mismatch_count",
            "polarity": "minimize",
        },
    }
    with pytest.raises(EvalError, match="lvs check failed to run"):
        run_eval(json.dumps(descriptor))


@pytest.mark.parametrize(
    "sim_report,expected_status,expected_exit_code,expected_count",
    [
        ({"status": "pass", "failed": 0, "errored": 0}, "pass", 0, 0),
        ({"status": "fail", "failed": 2, "errored": 0}, "fail", 3, 2),
        ({"status": "error", "failed": 0, "errored": 1}, "fail", 4, 1),
    ],
)
def test_sim_gate_mapping(
    monkeypatch, sim_report, expected_status, expected_exit_code, expected_count
):
    import klayout_tools.eval as eval_mod

    monkeypatch.setattr(eval_mod, "run_sim", lambda request, **kwargs: sim_report)
    descriptor = {
        "checks": {"sim": {"request": "req.json"}},
        "gates": ["sim"],
        "objective": {"check": "sim", "metric": "failed", "polarity": "minimize"},
    }
    report = run_eval(json.dumps(descriptor))
    assert report["gates"] == [
        {
            "check": "sim",
            "status": expected_status,
            "exit_code": expected_exit_code,
            "count": expected_count,
        }
    ]


def test_sim_error_propagates_as_eval_error(monkeypatch):
    import klayout_tools.eval as eval_mod

    def boom(request, **kwargs):
        raise SimError("unresolvable models library")

    monkeypatch.setattr(eval_mod, "run_sim", boom)
    descriptor = {
        "checks": {"sim": {"request": "req.json"}},
        "gates": ["sim"],
        "objective": {"check": "sim", "metric": "failed", "polarity": "minimize"},
    }
    with pytest.raises(EvalError, match="sim check failed to run"):
        run_eval(json.dumps(descriptor))


# --------------------------------------------------------------------------- #
# CLI: exit codes, --format json/text parity, --candidate flag
# --------------------------------------------------------------------------- #


def test_cli_exit_code_valid(tmp_path):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    descriptor = {
        "checks": {"drc": {"file": str(path), "deck": "sky130"}},
        "gates": ["drc"],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "minimize",
        },
    }
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor))

    assert main(["eval", str(descriptor_path), "--format", "json"]) == 0


def test_cli_exit_code_invalid(tmp_path):
    path = tmp_path / "violation.gds"
    _make_violation_layout().write(str(path))
    descriptor = {
        "checks": {"drc": {"file": str(path), "deck": "sky130"}},
        "gates": ["drc"],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "minimize",
        },
    }
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor))

    assert main(["eval", str(descriptor_path), "--format", "json"]) == 3


def test_cli_exit_code_failed_to_run(tmp_path):
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text("not valid json")

    assert main(["eval", str(descriptor_path), "--format", "json"]) == 1


def test_cli_json_contract(tmp_path, capsys):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    descriptor = {
        "checks": {"drc": {"file": str(path), "deck": "sky130"}},
        "gates": ["drc"],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "minimize",
        },
    }
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor))

    assert main(["eval", str(descriptor_path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data.keys()) == {
        "schema_version",
        "valid",
        "gates",
        "objective",
        "metrics",
        "candidate",
    }


def test_cli_error_json_on_stderr(tmp_path, capsys):
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text("not valid json")

    assert main(["eval", str(descriptor_path), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "eval"


def test_cli_text_format_not_json(tmp_path, capsys):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    descriptor = {
        "checks": {"drc": {"file": str(path), "deck": "sky130"}},
        "gates": ["drc"],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "minimize",
        },
    }
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor))

    assert main(["eval", str(descriptor_path)]) == 0
    out = capsys.readouterr().out
    assert "valid: True" in out
    assert "drc: pass" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_candidate_flag(tmp_path):
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    descriptor = {
        "checks": {"drc": {"file": "{layout}", "deck": "sky130"}},
        "gates": ["drc"],
        "objective": {
            "check": "drc",
            "metric": "violation_count",
            "polarity": "minimize",
        },
    }
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor))

    assert (
        main(
            [
                "eval",
                str(descriptor_path),
                "--candidate",
                f"layout={path}",
                "--format",
                "json",
            ]
        )
        == 0
    )


def test_cli_candidate_flag_bad_form(tmp_path):
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(
        json.dumps(
            {
                "checks": {"drc": {"file": "{layout}", "deck": "sky130"}},
                "gates": ["drc"],
                "objective": {
                    "check": "drc",
                    "metric": "violation_count",
                    "polarity": "minimize",
                },
            }
        )
    )
    assert (
        main(
            [
                "eval",
                str(descriptor_path),
                "--candidate",
                "not-key-value",
                "--format",
                "json",
            ]
        )
        == 1
    )


def test_cli_descriptor_relative_path_resolves_against_descriptor_dir(tmp_path):
    """A relative `file`/`block` path in a descriptor loaded from a file
    resolves against the descriptor file's own directory -- mirroring `klt
    lvs`/`klt sim`'s request-relative-path convention -- not the process
    cwd."""
    block_dir = tmp_path / "block"
    block_dir.mkdir()
    _make_clean_layout().write(str(block_dir / "layout.gds"))

    descriptor = {
        "checks": {
            "drc": {"file": "block/layout.gds", "deck": "sky130"},
            "layout-metrics": {"block": "block"},
        },
        "gates": ["drc"],
        "objective": {
            "check": "layout-metrics",
            "metric": "cell_count",
            "polarity": "minimize",
        },
    }
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor))

    report = run_eval(str(descriptor_path))
    assert report["valid"] is True
    assert report["objective"]["value"] == 1
