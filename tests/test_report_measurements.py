"""Tests for the `measurements` kind of `klt report` (issue #5839).

Fixtures are hand-built JSON envelope dicts matching the documented shapes
of `klt sim`'s report (docs/cli/sim.md, schema_version=3 -- the
`measurements[]` rollup + `corner_count`) and `klt yield`'s report
(docs/cli/yield.md, schema_version=1 -- `measurements[]` + `campaign`),
mirroring the "no external corpus needed" convention of
tests/test_report.py; the two committed example envelopes are exercised
end-to-end separately in tests/test_sim_examples.py-style manual checks.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.report import ReportError, build_report

#: The committed `klt sim` example, condensed to the fields this renderer
#: reads (examples/sim-remote/matrix-local.report.json).
SIM_REPORT_ENVELOPE = {
    "schema_version": 3,
    "netlist": {"path": "examples/sim-remote/ring_tb.spice", "scope": "repo"},
    "status": "pass",
    "corner_count": 5,
    "passed": 5,
    "failed": 0,
    "errored": 0,
    "measurements": [
        {
            "name": "t_rise_400",
            "unit": "s",
            "limits": None,
            "status": "pass",
            "worst_case": {
                "corner_id": "tt/1.800V/27C",
                "value": 2.3640661938534276e-06,
                "margin": None,
            },
        },
        {
            "name": "ring_freq_hz",
            "unit": "Hz",
            "limits": {"min": 1000000.0, "max": 2000000000.0},
            "status": "pass",
            "worst_case": {
                "corner_id": "ss/1.800V/27C",
                "value": 123500000.0,
                "margin": 122500000.0,
            },
        },
    ],
}

#: The committed `klt yield` example, condensed (examples/kb/
#: pfd-charge-pump-tri-state/design-centering-validation/
#: baseline-yield-report.json). A Monte Carlo rollup entry has no
#: `worst_case` and carries an empirical yield figure instead.
YIELD_REPORT_ENVELOPE = {
    "schema_version": 1,
    "samples": "raw-campaign/baseline/sample-set.json",
    "status": "fail",
    "measurement_count": 2,
    "measurements": [
        {
            "name": "i_mismatch_pct",
            "unit": "%",
            "n": 32,
            "limits": {"min": -3.0, "max": 3.0, "target_yield": 0.9},
            "yield": {
                "empirical": {
                    "method": "clopper-pearson",
                    "estimate": 0.40625,
                    "confidence": 0.9,
                    "confidence_interval": {
                        "low": 0.2596619649420151,
                        "high": 0.5665126270105416,
                    },
                    "n": 32,
                },
            },
            "status": "fail",
        },
        {
            "name": "i_dead",
            "unit": "A",
            "n": 32,
            "errored": 32,
            "limits": None,
            "status": "error",
        },
    ],
}

DRC_ENVELOPE = {
    "schema_version": 1,
    "file": "design.gds",
    "deck": "sky130",
    "status": "violations",
    "violation_count": 1,
    "violations": [
        {
            "rule": "poly.width.1",
            "description": "minimum poly width",
            "layer": "poly.drawing",
            "cell": "TOP",
            "bbox": {"left": 0, "bottom": 0, "right": 100, "top": 2000},
        }
    ],
}

SIM_HEADERS = "| Measurement | Unit | Limits | Worst | Margin | Corner | Status |"
SIM_AND_YIELD_HEADERS = (
    "| Measurement | Unit | Limits | Worst | Margin | Corner | Status "
    "| Yield | Yield CI |"
)


def _write(tmp_path, name: str, payload: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


# --------------------------------------------------------------------------- #
# Classification: one kind, both producers
# --------------------------------------------------------------------------- #


def test_sim_report_classifies_as_measurements(tmp_path):
    path = _write(tmp_path, "sim.json", SIM_REPORT_ENVELOPE)

    section = build_report([path])["sections"][0]

    assert section["kind"] == "measurements"
    assert section["status"] == "pass"
    assert section["item_count"] == 2
    assert section["source"] == path


def test_yield_report_classifies_as_measurements(tmp_path):
    path = _write(tmp_path, "yield.json", YIELD_REPORT_ENVELOPE)

    section = build_report([path])["sections"][0]

    assert section["kind"] == "measurements"
    assert section["status"] == "fail"
    assert section["item_count"] == 2


def test_measurements_that_is_not_a_list_is_a_hard_error(tmp_path):
    path = _write(tmp_path, "bad.json", {"schema_version": 3, "measurements": {"n": 1}})

    with pytest.raises(ReportError, match="unrecognized shape"):
        build_report([path])

    path = _write(tmp_path, "bad2.json", {"schema_version": 1, "measurements": "no"})

    with pytest.raises(ReportError, match="unrecognized shape"):
        build_report([path])


# --------------------------------------------------------------------------- #
# Rendered table: verbatim quoting, absent cells
# --------------------------------------------------------------------------- #


def test_sim_report_renders_measurement_table(tmp_path):
    path = _write(tmp_path, "sim.json", SIM_REPORT_ENVELOPE)

    report = build_report([path])

    assert SIM_HEADERS in report["markdown"]
    assert "| Yield |" not in report["markdown"]
    # limits: null -> Limits *and* Margin read as absent (the rollup's
    # worst_case is not an extreme there), never as zero.
    assert (
        "| t_rise_400 | s | - | 2.3640661938534276e-06 | - | tt/1.800V/27C | pass |"
        in report["markdown"]
    )
    # limits {min, max} -> bounds rendered .6g; value/margin quoted verbatim
    # (repr, exactly what klt sim's own text summary prints).
    assert (
        "| ring_freq_hz | Hz | 1e+06–2e+09 | 123500000.0 | 122500000.0 "
        "| ss/1.800V/27C | pass |" in report["markdown"]
    )
    # The plain-text rendering carries the same table, space-aligned, and
    # gains no Yield columns for a sim rollup.
    assert "2.3640661938534276e-06" in report["text"]
    assert "Yield" not in report["text"]


def test_yield_report_adds_yield_columns(tmp_path):
    path = _write(tmp_path, "yield.json", YIELD_REPORT_ENVELOPE)

    report = build_report([path])

    assert SIM_AND_YIELD_HEADERS in report["markdown"]
    # estimate .6f, CI bounds .6f with the confidence level named -- the
    # figures klt yield itself prints.
    assert (
        "| i_mismatch_pct | % | -3–3 | - | - | - | fail "
        "| 0.406250 | [0.259662, 0.566513] @ 90% |" in report["markdown"]
    )
    # A Monte Carlo rollup entry carries no worst_case: Worst/Margin/Corner
    # read as absent, and an entry without a yield block renders `-` in the
    # yield columns rather than zero.
    assert "| i_dead | A | - | - | - | - | error | - | - |" in report["markdown"]
    assert "Yield" in report["text"]
    assert "0.406250" in report["text"]


def test_null_worst_value_and_absent_status_render_absent_not_zero(tmp_path):
    envelope = {
        **SIM_REPORT_ENVELOPE,
        "measurements": [
            {
                "name": "startup_time",
                "unit": "s",
                "limits": {"min": 0.0, "max": 1e-3},
                "status": "absent",
                "worst_case": {
                    "corner_id": "tt/1.800V/27C",
                    "value": None,
                    "margin": None,
                },
            }
        ],
    }
    path = _write(tmp_path, "sim.json", envelope)

    report = build_report([path])

    assert (
        "| startup_time | s | 0–0.001 | - | - | tt/1.800V/27C | absent |"
        in report["markdown"]
    )


def test_inconclusive_status_is_rendered(tmp_path):
    envelope = {
        **SIM_REPORT_ENVELOPE,
        "measurements": [
            {
                **SIM_REPORT_ENVELOPE["measurements"][0],
                "name": "v_out",
                "status": "inconclusive",
            }
        ],
    }
    path = _write(tmp_path, "sim.json", envelope)

    report = build_report([path])

    assert (
        "| v_out | s | - | 2.3640661938534276e-06 | - | tt/1.800V/27C | inconclusive |"
        in report["markdown"]
    )


def test_non_dict_entries_are_skipped_not_fatal(tmp_path):
    envelope = {
        **SIM_REPORT_ENVELOPE,
        "measurements": [SIM_REPORT_ENVELOPE["measurements"][0], "oops", 42],
    }
    path = _write(tmp_path, "sim.json", envelope)

    section = build_report([path])["sections"][0]

    assert section["item_count"] == 1


def test_empty_measurements_renders_message_not_error(tmp_path):
    path = _write(
        tmp_path,
        "sim.json",
        {"schema_version": 3, "status": "pass", "corner_count": 0, "measurements": []},
    )

    report = build_report([path])

    assert report["sections"][0]["item_count"] == 0
    assert "No measurements found." in report["markdown"]
    assert "No measurements found." in report["text"]


def test_netlist_is_named_in_title_when_present(tmp_path):
    path = _write(tmp_path, "sim.json", SIM_REPORT_ENVELOPE)

    section = build_report([path])["sections"][0]

    assert section["title"] == "Measurements: examples/sim-remote/ring_tb.spice"
    # info_lines render into the markdown/text bodies (the sections[]
    # summary carries only kind/source/title/status/item_count).
    assert (
        "- Netlist: examples/sim-remote/ring_tb.spice"
        in build_report([path])["markdown"]
    )


# --------------------------------------------------------------------------- #
# Combination + CLI surface
# --------------------------------------------------------------------------- #


def test_mixed_invocation_keeps_section_order(tmp_path):
    drc = _write(tmp_path, "drc.json", DRC_ENVELOPE)
    sim = _write(tmp_path, "sim.json", SIM_REPORT_ENVELOPE)

    report = build_report([drc, sim])

    assert [s["kind"] for s in report["sections"]] == ["drc", "measurements"]
    assert report["markdown"].index("DRC Report") < report["markdown"].index(
        "Measurements"
    )


def test_cli_all_three_formats(tmp_path, capsys):
    path = _write(tmp_path, "sim.json", SIM_REPORT_ENVELOPE)

    for fmt in ("text", "json", "github-summary"):
        exit_code = main(["report", path, "--format", fmt])
        assert exit_code == 0, fmt

    out = capsys.readouterr().out.splitlines()
    # The run printed all three renderings back to back; each carries the
    # table. (Interleaved, so assert on the joined output.)
    joined = "\n".join(out)
    assert "## Measurements: examples/sim-remote/ring_tb.spice" in joined
    assert SIM_HEADERS in joined
    assert "Measurements: examples/sim-remote/ring_tb.spice" in joined


def test_cli_unrecognized_shape_exits_one_with_json_error(tmp_path, capsys):
    path = _write(tmp_path, "bad.json", {"schema_version": 1, "foo": 1})

    exit_code = main(["report", path, "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "measurements[]" in err["error"]["message"]
