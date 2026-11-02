"""Issue #4805: ``klayout-runset`` accepts ``reference.form: "subckt-call"``
and converts the reference before the runset sees it. Split from
``test_lvs_klayout_runset`` (file-size ratchet); reuses its fake-klayout rig."""

from __future__ import annotations

import json

import pytest

import test_lvs_klayout_runset as base
from klayout_tools import lvs_runset
from klayout_tools.lvs import LvsError, rerun_lvs_report, run_lvs

fake_klayout = base.fake_klayout  # pytest collects the fixture by module-level name
_request = base._request
_unresolved = base._unresolved


MIM_SPICE = (
    ".subckt TOP\nM1 c b a d NMOS L=0.2u W=0.4u\n"
    "XC1 a b sky130_fd_pr__cap_mim_m3_1 W=10u L=5u\n.ends\n"
)


def _mim_request(tmp_path, **reference):
    request = json.loads(_request(tmp_path, 1))
    (tmp_path / "ref.sp").write_text(MIM_SPICE)
    request["reference"].update(reference)
    return request


def test_subckt_call_converts_mim_for_the_runset(tmp_path, fake_klayout):
    request = _mim_request(tmp_path, form="subckt-call", deck="sky130")
    seen = {}
    real = lvs_runset._run_runset

    def spy(**kw):
        seen["text"] = open(kw["reference"]).read()
        return real(**kw)

    lvs_runset._run_runset = spy
    try:
        report = run_lvs(json.dumps(request))
    finally:
        lvs_runset._run_runset = real
    # W=10u L=5u -> A=50 um^2 = 50P, P=2*(10+5)=30 um = 30U (numeric, not assumed).
    assert "C1 a b 0 sky130_fd_pr__model__cap_mim A=50P P=30U" in seen["text"]
    assert "XC1" not in seen["text"]
    assert not _unresolved(report)
    assert report["reference_conversion"] == {
        "form": "subckt-call",
        "deck": "sky130",
        "device_map": None,
    }
    # The caller's file is untouched and still the echoed reference.
    assert "XC1" in (tmp_path / "ref.sp").read_text()
    assert report["reference"] == str(tmp_path / "ref.sp")


def test_without_form_mim_is_still_unresolved(tmp_path, fake_klayout):
    report = run_lvs(json.dumps(_mim_request(tmp_path)))
    assert len(_unresolved(report)) == 1
    assert "reference_conversion" not in report


def test_other_reference_keys_still_refused(tmp_path, fake_klayout):
    for extra in (
        {"deck": "sky130"},
        {"device_bulk": {"x": "y"}},
        {"form": "subckt-call", "device_bulk": {"x": "y"}},
    ):
        with pytest.raises(LvsError, match="not supported by engine"):
            run_lvs(json.dumps(_mim_request(tmp_path, **extra)))
    with pytest.raises(LvsError, match="gate-level-verilog"):
        run_lvs(json.dumps(_mim_request(tmp_path, form="gate-level-verilog")))


def test_subckt_call_unknown_device_is_an_error(tmp_path, fake_klayout):
    request = _mim_request(tmp_path, form="subckt-call", deck="sky130")
    (tmp_path / "ref.sp").write_text(
        MIM_SPICE.replace("sky130_fd_pr__cap_mim_m3_1", "no_such_dev")
    )
    with pytest.raises(LvsError, match="could not convert subckt-call"):
        run_lvs(json.dumps(request))


def test_subckt_call_report_replays_under_check_rerun(tmp_path, fake_klayout):
    request = _mim_request(tmp_path, form="subckt-call", deck="sky130")
    report = run_lvs(json.dumps(request))
    committed = tmp_path / "report.json"
    committed.write_text(json.dumps(report))
    result = rerun_lvs_report(str(committed))
    assert result["status"] == "match", result
    assert not result.get("drift"), result
