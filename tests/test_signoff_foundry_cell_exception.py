"""T1 item 3 disclosed foundry-cell DRC exceptions (issue #4530)."""

import json

import klayout.db as kdb

from klayout_tools._provenance import cell_geometry_digest, sha256_file
from klayout_tools.signoff import build_tier_report

RULES = {"met4.2": 3, "met5.1": 2}


def _gds(path, *, pad_width=1000, with_top=False):
    layout = kdb.Layout()
    layout.dbu = 0.001
    layer = layout.layer(71, 20)
    pad = layout.create_cell("PAD")
    pad.shapes(layer).insert(kdb.Box(0, 0, pad_width, 1000))
    if with_top:
        top = layout.create_cell("TOP")
        top.insert(kdb.CellInstArray(pad.cell_index(), kdb.Trans()))
    layout.write(str(path))
    return str(path)


def _drc(layout, rule_counts, status="violations", **extra):
    return {
        "schema_version": 1,
        "file": layout,
        "deck": "sky130",
        "status": status,
        "violation_count": sum(rule_counts.values()),
        "rule_counts": rule_counts,
        "violations": [],
        "metrics": {"drc__error__count": sum(rule_counts.values())},
        "provenance": {
            "klt_version": "0.2.0",
            "klayout_version": "0.30.10",
            "pdk": None,
            "deck": {"name": "sky130", "content_hash": "sha256:deck"},
            "input": {"content_hash": f"sha256:{sha256_file(layout)}"},
        },
        **extra,
    }


def _setup(tmp_path, *, block_counts=None, bare_counts=None, rules=None):
    block = _gds(tmp_path / "block.gds", with_top=True)
    bare = _gds(tmp_path / "bare.gds")
    report = tmp_path / "block-drc.json"
    report.write_text(json.dumps(_drc(block, block_counts or RULES)))
    bare_report = tmp_path / "bare-drc.json"
    bare_report.write_text(json.dumps(_drc(bare, bare_counts or RULES)))
    exc = {
        "cell": "PAD",
        "cell_hash": cell_geometry_digest(bare, "PAD"),
        "rules": rules or RULES,
        "bare_cell_layout": bare,
        "bare_cell_report": str(bare_report),
    }
    return report, exc


def _item3(report, exceptions):
    manifest = {
        "block": "b",
        "kind": "analog",
        "evidence": {"3": {"file": str(report), "foundry_cell_exceptions": exceptions}},
    }
    return next(i for i in build_tier_report(manifest)["items"] if i["id"] == 3)


def test_cell_digest_is_hierarchy_independent(tmp_path):
    block = _gds(tmp_path / "a.gds", with_top=True)
    bare = _gds(tmp_path / "b.gds")
    assert cell_geometry_digest(block, "PAD") == cell_geometry_digest(bare, "PAD")
    assert cell_geometry_digest(block, "NOPE") is None


def test_valid_exception_is_met_and_listed(tmp_path):
    report, exc = _setup(tmp_path)
    item = _item3(report, [exc])
    assert item["status"] == "met", item
    listed = item["citation"]["foundry_cell_exceptions"]
    assert listed == [
        {
            "cell": "PAD",
            "cell_hash": exc["cell_hash"],
            "rules": RULES,
            "bare_cell_report": exc["bare_cell_report"],
        }
    ]


def test_without_exception_violations_stay_unmet(tmp_path):
    report, _ = _setup(tmp_path)
    manifest = {"block": "b", "kind": "analog", "evidence": {"3": str(report)}}
    item = next(i for i in build_tier_report(manifest)["items"] if i["id"] == 3)
    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"


def test_bare_cell_counts_differ_is_unmet(tmp_path):
    report, exc = _setup(tmp_path, bare_counts={"met4.2": 3, "met5.1": 1})
    item = _item3(report, [exc])
    assert item["status"] == "unmet"
    lapsed = item["detail"]["foundry_cell_exceptions"]["lapsed"]
    assert "bare_cell_counts_differ" in lapsed


def test_cell_hash_change_is_unmet(tmp_path):
    report, exc = _setup(tmp_path)
    _gds(tmp_path / "block.gds", pad_width=1200, with_top=True)  # cell edited
    # keep the report's input hash current so only the cell hash can lapse
    block_report = json.loads(report.read_text())
    block_report["provenance"]["input"]["content_hash"] = "sha256:" + sha256_file(
        str(tmp_path / "block.gds")
    )
    report.write_text(json.dumps(block_report))
    item = _item3(report, [exc])
    assert item["status"] == "unmet"
    assert "cell_hash_mismatch" in item["detail"]["foundry_cell_exceptions"]["lapsed"]


def test_violation_outside_excepted_cell_is_unmet(tmp_path):
    report, exc = _setup(tmp_path, block_counts={"met4.2": 3, "met5.1": 2, "m1.1": 1})
    item = _item3(report, [exc])
    assert item["status"] == "unmet"
    assert "block_counts_differ" in item["detail"]["foundry_cell_exceptions"]["lapsed"]


def test_violation_attributed_to_other_cell_is_unmet(tmp_path):
    report, exc = _setup(tmp_path)
    data = json.loads(report.read_text())
    data["violations"] = [{"rule": "met4.2", "source_cell": "MY_BLOCK"}]
    report.write_text(json.dumps(data))
    item = _item3(report, [exc])
    assert item["status"] == "unmet"
    assert (
        "violation_outside_excepted_cell"
        in item["detail"]["foundry_cell_exceptions"]["lapsed"]
    )


def test_stale_bare_cell_report_is_unmet(tmp_path):
    report, exc = _setup(tmp_path)
    bare_report = json.loads(open(exc["bare_cell_report"]).read())
    bare_report["provenance"]["input"]["content_hash"] = "sha256:stale"
    open(exc["bare_cell_report"], "w").write(json.dumps(bare_report))
    item = _item3(report, [exc])
    assert item["status"] == "unmet"
    assert (
        "bare_cell_report_stale" in item["detail"]["foundry_cell_exceptions"]["lapsed"]
    )


def test_clean_report_ignores_exceptions(tmp_path):
    report, exc = _setup(tmp_path)
    report.write_text(json.dumps(_drc(str(tmp_path / "block.gds"), {}, status="clean")))
    assert _item3(report, [exc])["status"] == "met"
    assert "foundry_cell_exceptions" not in _item3(report, [exc])["citation"]


def test_malformed_exception_is_invalid_evidence(tmp_path):
    report, exc = _setup(tmp_path)
    del exc["cell_hash"]
    item = _item3(report, [exc])
    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"
