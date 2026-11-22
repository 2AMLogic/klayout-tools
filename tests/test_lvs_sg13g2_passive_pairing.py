"""SG13G2 passive pairing diagnostics on the public comparison paths."""

import json
from pathlib import Path

import pytest

from helpers import lvs_runset_fixtures
from klayout_tools.lvs import LvsError, rerun_lvs_report, run_lvs
from klayout_tools.netlist_normalize import convert_reference_netlist


def _source(body):
    return f".SUBCKT TOP A B SUB\n{body}\n.ENDS TOP\n"


def _request(tmp_path, layout, reference):
    left, right = tmp_path / "layout.sp", tmp_path / "ref.sp"
    left.write_text(_source(layout))
    right.write_text(_source(reference))
    return {
        "layout": {"netlist": str(left), "top": "TOP"},
        "reference": {
            "netlist": str(right),
            "top": "TOP",
            "form": "subckt-call",
            "deck": "sg13g2",
        },
    }


_RES = "R1 A B SUB 0 rppd"
_CALL = "X1 A B SUB rppd w=1u l=1u"
_CAP = "C1 A B 0 cap_cmim"


@pytest.mark.parametrize(
    ("layout", "reference", "verdict", "state", "differences"),
    [
        (_RES, _CALL, "match", "all_devices_paired", []),
        (
            _RES,
            "X1 A B rppd w=1u l=1u",
            "mismatch",
            "discrepancy_observed",
            ["terminal_list"],
        ),
        (
            _CAP,
            "C1 A B 0 $[cap_cmim]",
            "mismatch",
            "discrepancy_observed",
            ["class_model"],
        ),
        (_RES + "\nR2 A B SUB 0 rppd", _CALL, "mismatch", "insufficient_evidence", []),
        (
            _RES + "\nR2 B B SUB 0 rppd",
            _CALL.replace("B SUB", "B A") + "\nX2 B B A rppd w=1u l=1u",
            "mismatch",
            "insufficient_evidence",
            [],
        ),
    ],
)
def test_compared_passive_signatures(
    tmp_path, layout, reference, verdict, state, differences
):
    report = run_lvs(json.dumps(_request(tmp_path, layout, reference)))
    assert report["status"] == verdict
    diagnosis = report["sg13g2_passive_pairing"]
    assert diagnosis["status"] == state
    assert [
        d for row in diagnosis["findings"] for d in row["differences"]
    ] == differences
    if differences == ["terminal_list"]:
        (row,) = diagnosis["findings"]
        assert row["layout_terminals"] == ["A", "B", "W"]
        assert row["reference_terminals"] == ["A", "B"]
    if differences == ["class_model"]:
        (row,) = diagnosis["findings"]
        assert row["reference_model"] == "$[CAP_CMIM]"


def test_multiplied_and_explicit_parallel_mim_units(tmp_path):
    layout = "\n".join(f"C{i} A B 0 cap_cmim" for i in range(12))
    explicit = "\n".join(f"X{i} A B cap_cmim w=1u l=1u" for i in range(12))
    for reference in ("Xc A B cap_cmim w=1u l=1u m=12", explicit):
        text = convert_reference_netlist(_source(reference), deck="sg13g2").text
        assert (
            len([line for line in text.splitlines() if line[:1].upper() == "C"]) == 12
        )
        report = run_lvs(json.dumps(_request(tmp_path, layout, reference)))
        assert report["status"] == "match"
        assert report["counts"]["devices"] == {
            "layout": 12,
            "reference": 12,
            "matched": 12,
        }
    with pytest.raises(LvsError, match="(?i)mult|m="):
        run_lvs(json.dumps(_request(tmp_path, _RES, _CALL + " m=12")))


@pytest.mark.parametrize("kept", [2, 12])
def test_runset_distinguishes_converted_and_compared_counts(
    tmp_path, monkeypatch, kept
):
    request = json.loads(lvs_runset_fixtures.request(tmp_path, 4))
    request["reference"].update(form="subckt-call", deck="sg13g2")
    ref = Path(request["reference"]["netlist"])
    lvs_runset_fixtures.install_fake_klayout(monkeypatch, tmp_path)
    write_db = lvs_runset_fixtures.write_lvsdb
    converted = []

    def reader(gds, schematic, out, extract, compare):
        # A runset sees twelve emitted units. Model a delegate retaining a
        # subset before serializing its actual cross-reference.
        lines = [
            line
            for line in Path(schematic).read_text().splitlines()
            if line[:1].upper() == "C"
        ]
        converted.append(lines)
        readback = tmp_path / "delegate.sp"
        cards = [line.replace(" cap_cmim", " 0 cap_cmim") for line in lines[:kept]]
        readback.write_text(_source("\n".join(cards)))
        write_db(gds, str(readback), out, extract, compare)

    monkeypatch.setattr(lvs_runset_fixtures, "write_lvsdb", reader)
    for body in (
        "Xc A B cap_cmim w=1u l=1u m=12",
        "\n".join(f"X{i} A B cap_cmim w=1u l=1u" for i in range(12)),
    ):
        ref.write_text(_source(body))
        report = run_lvs(json.dumps(request))
        assert len(converted[-1]) == 12
        assert report["device_census"]["reference"] == {"CAP_CMIM": kept}
        assert report["status"] == "mismatch"
        assert report["sg13g2_passive_pairing"] == {
            "status": "insufficient_evidence",
            "findings": [],
        }


def test_historical_report_and_present_diagnosis_replay(tmp_path):
    request = _request(tmp_path, _RES, _CALL)
    report = run_lvs(json.dumps(request))
    path = tmp_path / "report.json"
    inputs = dict(
        layout_path=request["layout"]["netlist"],
        reference_path=request["reference"]["netlist"],
    )
    report["sg13g2_passive_pairing"]["status"] = "insufficient_evidence"
    path.write_text(json.dumps(report))
    assert rerun_lvs_report(str(path), **inputs)["status"] == "drifted"
    del report["sg13g2_passive_pairing"]
    path.write_text(json.dumps(report))
    assert rerun_lvs_report(str(path), **inputs)["status"] == "match"
