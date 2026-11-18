"""`klt pex` ``delta[]`` row-label fields (issue #5681).

``spec_row`` is a display label whose form depends on how many testbenches the
run was given; ``testbench`` and ``measurement`` do not. Split out of
``test_pex.py`` to keep that file under its size ceiling (the multi-testbench
``spec_row`` prefix test moved here with them, content unchanged).
"""

# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)

from __future__ import annotations

import pytest

import klayout_tools.pex as pex_module
from klayout_tools.pex import _build_delta_rows, _unextracted_delta_rows, run_pex
from klayout_tools.sim import SimError
from test_pex import (
    _measurement,
    _sim_report,
    _two_corner_schematic_report,
    _write_harness,
    _write_request,
    _write_schematic_dut,
    _write_testbench,
    measure_reference,  # noqa: F401  (pytest fixture)
    resistor_layout,  # noqa: F401  (pytest fixture)
)


def test_build_delta_rows_multi_testbench_prefixes_spec_row():
    schematic = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    extracted = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.1)]}]
    )
    rows = _build_delta_rows(
        spec_row_prefix="gain-tb",
        schematic_report=schematic,
        extracted_report=extracted,
    )
    assert rows[0]["spec_row"] == "gain-tb.vout"


@pytest.mark.parametrize(
    ("prefix", "expected_spec_row"),
    [
        (None, "td.a"),
        ("gain-tb", "gain-tb.td.a"),
    ],
    ids=["single-testbench-bare", "multi-testbench-prefixed"],
)
def test_build_delta_rows_label_fields_are_invocation_independent(
    prefix, expected_spec_row
):
    """`testbench` / `measurement` identify a row the same way whether or not
    `spec_row` carries the prefix. The measurement name holds a `.` on purpose:
    recovering it by stripping a maybe-present `<stem>.` prefix from `spec_row`
    would be ambiguous."""
    corner = "tt/1.800V/27C"
    (row,) = _build_delta_rows(
        testbench="gain-tb",
        spec_row_prefix=prefix,
        schematic_report=_sim_report(
            [
                {
                    "corner_id": corner,
                    "measurements": [{**_measurement(1.0), "name": "td.a"}],
                }
            ]
        ),
        extracted_report=_sim_report(
            [
                {
                    "corner_id": corner,
                    "measurements": [{**_measurement(1.1), "name": "td.a"}],
                }
            ]
        ),
    )
    assert row["spec_row"] == expected_spec_row
    assert row["testbench"] == "gain-tb"
    assert row["measurement"] == "td.a"


def test_unextracted_delta_rows_carry_label_fields():
    """The extraction-failed path labels its rows too."""
    schematic = _sim_report([{"corner_id": "tt", "measurements": [_measurement(1.0)]}])
    (row,) = _unextracted_delta_rows(
        testbench="gain-tb", spec_row_prefix="gain-tb", schematic_report=schematic
    )
    assert row["extracted_value"] is None
    assert row["spec_row"] == "gain-tb.vout"
    assert row["testbench"] == "gain-tb"
    assert row["measurement"] == "vout"


def _label_keys(report, testbench):
    return [
        (row["testbench"], row["measurement"], row["corner_id"])
        for row in report["delta"]
        if row["testbench"] == testbench
    ]


def test_run_pex_delta_labels_do_not_depend_on_testbench_count(
    tmp_path, resistor_layout, monkeypatch
):
    """The issue's core criterion: the same bench yields the same `testbench`
    / `measurement` whether it is the only testbench or one of several -- only
    `spec_row` differs. Run outside any repo, so every request path is
    redacted (`path: null`, scope external); the stem must still be present."""
    monkeypatch.setattr(
        pex_module, "run_sim", lambda *_a, **_k: _two_corner_schematic_report()
    )
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    gain = _write_request(tmp_path / "gain.json", tb)
    offset = _write_request(tmp_path / "offset.json", tb)

    alone = run_pex(resistor_layout, [str(gain)], "sky130")
    together = run_pex(resistor_layout, [str(gain), str(offset)], "sky130")

    assert alone["testbenches"][0]["request"] == {"path": None, "scope": "external"}
    assert _label_keys(alone, "gain") == _label_keys(together, "gain")
    assert len(_label_keys(alone, "gain")) == 2
    assert {row["spec_row"] for row in alone["delta"]} == {"vout"}
    assert {row["spec_row"] for row in together["delta"]} == {
        "gain.vout",
        "offset.vout",
    }
    assert all(row["measurement"] == "vout" for row in alone["delta"])
    for row in together["delta"]:
        assert row["spec_row"] == f"{row['testbench']}.{row['measurement']}"
    # Labels, not graded data: the verdict fields do not move.
    assert alone["status"] == together["status"]
    assert alone["coverage"]["delta_rows"] == 2
    assert together["coverage"]["delta_rows"] == 4


def test_run_pex_error_rows_carry_label_fields(tmp_path, resistor_layout, monkeypatch):
    """Rows built by `_unextracted_delta_rows` inside a real `run_pex` (the
    extracted side raised) carry the same two fields."""
    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _two_corner_schematic_report()
        raise SimError("no capacity", code="batch_no_capacity")

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert all(row["extracted_value"] is None for row in report["delta"])
    assert {(row["testbench"], row["measurement"]) for row in report["delta"]} == {
        ("request", "vout")
    }
    assert {row["spec_row"] for row in report["delta"]} == {"vout"}


def test_run_pex_measure_command_rows_have_null_testbench(
    tmp_path, resistor_layout, measure_reference
):
    """`--measure-command` has no testbench file: the key is present and
    `null` (not omitted), and `measurement` is the document's own name."""
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path),
        reference_netlist=measure_reference,
    )

    assert report["delta"]
    for row in report["delta"]:
        assert "testbench" in row
        assert row["testbench"] is None
        assert row["measurement"] == row["spec_row"] == "t_cross"
