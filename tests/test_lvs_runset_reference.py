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
    assert "C1 a b sky130_fd_pr__model__cap_mim A=50P P=30U" in seen["text"]
    assert "XC1" not in seen["text"]
    # Issue #4817: the real sky130.lvs reader takes a positional ``0`` as a net.
    assert " 0 " not in seen["text"].split("C1", 1)[1].split("\n", 1)[0]
    assert not _unresolved(report)
    # Issue #5796: no passthrough -> the additive keys are present, empty/zero.
    assert report["reference_conversion"] == {
        "form": "subckt-call",
        "deck": "sky130",
        "device_map": None,
        "passthrough_callees": [],
        "passthrough_cards": 0,
    }
    assert report["schema_version"] == 3
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


# --- Issue #5402: deck DEVICE_LIST pass-through and top renaming ------------

RF_SPICE = (
    ".subckt TOP a b\nXL1 a b inductor W=10u L=5u\n"
    "XC1 a b sky130_fd_pr__cap_mim_m3_1 W=10u L=5u\n.ends TOP\n"
)


def _capture_reference(request):
    seen = {}
    real = lvs_runset._run_runset

    def spy(**kw):
        seen["text"] = open(kw["reference"]).read()
        seen["path"] = kw["reference"]
        return real(**kw)

    lvs_runset._run_runset = spy
    try:
        report = run_lvs(json.dumps(request))
    finally:
        lvs_runset._run_runset = real
    return report, seen


def _rf_request(tmp_path, device_list=("inductor",), **reference):
    request = _mim_request(tmp_path, form="subckt-call", deck="sky130", **reference)
    (tmp_path / "ref.sp").write_text(RF_SPICE)
    names = ", ".join(f'"{n}".upcase' for n in device_list)
    (tmp_path / "pdk.lvs").write_text(f"DEVICE_LIST = [{names}]\n")
    return request


def test_deck_device_list_subckt_passes_through(tmp_path, fake_klayout):
    _, seen = _capture_reference(_rf_request(tmp_path))
    assert "XL1 a b inductor W=10u L=5u" in seen["text"]  # untouched
    assert "C1 a b sky130_fd_pr__model__cap_mim" in seen["text"]  # curated wins


def test_curated_name_in_device_list_still_converts(tmp_path, fake_klayout):
    request = _rf_request(tmp_path, device_list=("sky130_fd_pr__cap_mim_m3_1",))
    with pytest.raises(LvsError, match="inductor"):
        run_lvs(json.dumps(request))  # inductor not listed here -> still loud
    request = _rf_request(
        tmp_path, device_list=("inductor", "sky130_fd_pr__cap_mim_m3_1")
    )
    _, seen = _capture_reference(request)
    assert "XC1" not in seen["text"]


def test_unlisted_unknown_subckt_still_refused(tmp_path, fake_klayout):
    request = _rf_request(tmp_path, device_list=("other_dev",))
    with pytest.raises(LvsError, match="could not convert subckt-call"):
        run_lvs(json.dumps(request))


def test_differing_tops_rename_reference_copy_only(tmp_path, fake_klayout):
    request = _rf_request(tmp_path, top="TOP")
    request["layout"]["top"] = "LAYTOP"
    (tmp_path / "ref.sp").write_text(RF_SPICE + ".subckt TOPPER x\n.ends TOPPER\n")
    report, seen = _capture_reference(request)
    assert ".subckt LAYTOP a b" in seen["text"]
    assert ".ends LAYTOP" in seen["text"]
    assert ".subckt TOPPER x" in seen["text"]  # not a substring match
    assert ".subckt TOP a b" in (tmp_path / "ref.sp").read_text()
    assert report["reference_top"] == "TOP"


def test_matching_or_absent_tops_leave_reference_unrenamed(tmp_path, fake_klayout):
    request = json.loads(_request(tmp_path, 1))
    request["layout"]["top"] = "TOP"
    request["reference"]["top"] = "TOP"
    _, seen = _capture_reference(request)
    assert seen["path"] == str(tmp_path / "ref.sp")  # no work copy
    request["reference"].pop("top")
    _, seen = _capture_reference(request)
    assert seen["path"] == str(tmp_path / "ref.sp")


# --- Issue #5796: the passthrough is observable -----------------------------


def test_passthrough_is_echoed_and_quiet_when_runset_has_reference_devices(
    tmp_path, fake_klayout
):
    """The #5402 case: the inductor card is still passed through untouched, the
    curated MIM card still converts (so the reference has a device), and there
    is no finding -- only the echo records what was hidden."""
    report, seen = _capture_reference(_rf_request(tmp_path))
    assert "XL1 a b inductor W=10u L=5u" in seen["text"]
    assert report["reference_conversion"] == {
        "form": "subckt-call",
        "deck": "sky130",
        "device_map": None,
        "passthrough_callees": ["inductor"],
        "passthrough_cards": 1,
    }
    assert report["coverage"]["recognized_device_classes"]["reference"]
    assert not _unresolved(report)
    assert report["schema_version"] == 3


def test_passthrough_that_leaves_no_reference_device_is_reported(
    tmp_path, fake_klayout
):
    request = _rf_request(tmp_path, device_list=("inductor", "varactor"))
    (tmp_path / "ref.sp").write_text(
        ".subckt TOP a b\nXL1 a b inductor W=10u L=5u\n"
        "XL2 a b inductor W=20u L=5u\nXV1 a b varactor W=1u\n.ends TOP\n"
    )
    report, seen = _capture_reference(request)
    assert "XL2 a b inductor W=20u L=5u" in seen["text"]  # still untouched
    assert report["reference_conversion"]["passthrough_callees"] == [
        "inductor",
        "varactor",
    ]
    assert report["reference_conversion"]["passthrough_cards"] == 3
    assert not report["coverage"]["recognized_device_classes"]["reference"]
    (finding,) = _unresolved(report)
    assert finding["severity"] == "error"
    assert finding["details"]["callees"] == ["inductor", "varactor"]
    assert finding["details"]["passthrough_cards"] == 3
    assert "reference.device_map" in finding["description"]
    assert report["status"] == "mismatch"
    assert report["category_error_counts"]["reference.unresolved_subckt_call"] == 1


def test_curated_conversion_alone_never_reports_passthrough(tmp_path, fake_klayout):
    """A deck whose DEVICE_LIST names no callee in the reference passes nothing
    through, so a curated-only reference carries the empty form and no finding."""
    request = _rf_request(tmp_path, device_list=("other_dev",))
    (tmp_path / "ref.sp").write_text(MIM_SPICE)
    report = run_lvs(json.dumps(request))
    assert report["reference_conversion"]["passthrough_callees"] == []
    assert report["reference_conversion"]["passthrough_cards"] == 0
    assert not _unresolved(report)


def test_passthrough_report_replays_under_check_rerun(tmp_path, fake_klayout):
    report = run_lvs(json.dumps(_rf_request(tmp_path)))
    committed = tmp_path / "report.json"
    committed.write_text(json.dumps(report))
    result = rerun_lvs_report(str(committed))
    assert not result.get("drift"), result


def test_report_committed_before_passthrough_keys_replays_without_drift(
    tmp_path, fake_klayout
):
    report = run_lvs(json.dumps(_rf_request(tmp_path)))
    for key in ("passthrough_callees", "passthrough_cards"):
        report["reference_conversion"].pop(key)
    committed = tmp_path / "report.json"
    committed.write_text(json.dumps(report))
    result = rerun_lvs_report(str(committed))
    assert not result.get("drift"), result
