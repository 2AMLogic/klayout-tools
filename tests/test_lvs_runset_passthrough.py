"""Runset DEVICE_LIST passthrough reporting and replay (issue #5796)."""

import json

import test_lvs_runset_reference as ref
from klayout_tools.lvs import rerun_lvs_report, run_lvs

fake_klayout = ref.fake_klayout
tmp_path = ref.tmp_path


def test_passthrough_is_echoed_and_quiet_when_runset_has_reference_devices(
    tmp_path, fake_klayout
):
    """The #5402 case: the inductor card is still passed through untouched, the
    curated MIM card still converts (so the reference has a device), and there
    is no finding -- only the echo records what was hidden."""
    report, seen = ref._capture_reference(ref._rf_request(tmp_path))
    assert "XL1 a b inductor W=10u L=5u" in seen["text"]
    assert report["reference_conversion"] == {
        "form": "subckt-call",
        "deck": "sky130",
        "device_map": None,
        "passthrough_callees": ["inductor"],
        "passthrough_cards": 1,
    }
    assert report["coverage"]["recognized_device_classes"]["reference"]
    assert not ref._unresolved(report)
    assert report["schema_version"] == 3


def test_passthrough_that_leaves_no_reference_device_is_reported(
    tmp_path, fake_klayout
):
    request = ref._rf_request(tmp_path, device_list=("inductor", "varactor"))
    (tmp_path / "ref.sp").write_text(
        ".subckt TOP a b\nXL1 a b inductor W=10u L=5u\n"
        "XL2 a b inductor W=20u L=5u\nXV1 a b varactor W=1u\n.ends TOP\n"
    )
    report, seen = ref._capture_reference(request)
    assert "XL2 a b inductor W=20u L=5u" in seen["text"]  # still untouched
    assert report["reference_conversion"]["passthrough_callees"] == [
        "inductor",
        "varactor",
    ]
    assert report["reference_conversion"]["passthrough_cards"] == 3
    assert not report["coverage"]["recognized_device_classes"]["reference"]
    (finding,) = ref._unresolved(report)
    assert finding["severity"] == "error"
    assert finding["details"]["callees"] == ["inductor", "varactor"]
    assert finding["details"]["passthrough_cards"] == 3
    assert "reference.device_map" in finding["description"]
    assert report["status"] == "mismatch"
    assert report["category_error_counts"]["reference.unresolved_subckt_call"] == 1


def test_curated_conversion_alone_never_reports_passthrough(tmp_path, fake_klayout):
    """A deck whose DEVICE_LIST names no callee in the reference passes nothing
    through, so a curated-only reference carries the empty form and no finding."""
    request = ref._rf_request(tmp_path, device_list=("other_dev",))
    (tmp_path / "ref.sp").write_text(ref.MIM_SPICE)
    report = run_lvs(json.dumps(request))
    assert report["reference_conversion"]["passthrough_callees"] == []
    assert report["reference_conversion"]["passthrough_cards"] == 0
    assert not ref._unresolved(report)


def test_passthrough_report_replays_under_check_rerun(tmp_path, fake_klayout):
    report = run_lvs(json.dumps(ref._rf_request(tmp_path)))
    committed = tmp_path / "report.json"
    committed.write_text(json.dumps(report))
    result = rerun_lvs_report(str(committed))
    assert not result.get("drift"), result


def test_report_committed_before_passthrough_keys_replays_without_drift(
    tmp_path, fake_klayout
):
    report = run_lvs(json.dumps(ref._rf_request(tmp_path)))
    for key in ("passthrough_callees", "passthrough_cards"):
        report["reference_conversion"].pop(key)
    committed = tmp_path / "report.json"
    committed.write_text(json.dumps(report))
    result = rerun_lvs_report(str(committed))
    assert not result.get("drift"), result


def test_sg13g2_preserved_calls_are_excluded_from_the_passthrough_count(
    tmp_path, fake_klayout
):
    request = ref._sg13g2_request(
        tmp_path,
        ".subckt TOP c b e s\nXQ9 c b e s npn13G2v Nx=1\n"
        "Xchild c b e s npn13G2\n.ends TOP\n"
        ".subckt npn13G2 c b e s\n.ends npn13G2\n",
    )
    report, seen = ref._capture_reference(request)
    assert "Xchild c b e s npn13G2" in seen["text"]
    assert report["reference_conversion"]["passthrough_callees"] == []
    assert report["reference_conversion"]["passthrough_cards"] == 0
    (finding,) = ref._unresolved(report)
    assert finding["instance"]["reference"].upper() == "Q9"
