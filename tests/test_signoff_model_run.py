"""Issue #4859: the ``model-run`` evidence kind (T1 item 5 only)."""

from __future__ import annotations

import hashlib
import json

from klayout_tools.signoff import build_tier_report

LIM = {"min": 1.0}


def _meas(name, row=None, *, limits=LIM, status="pass"):
    m = {"name": name, "status": status, "limits": limits}
    if row:
        m["spec_row"] = row
    return m


def _env(measurements=None, *, status="pass", points=None, **extra):
    env = {
        "schema_version": 1,
        "kind": "model-run",
        "status": status,
        "method": "linear small-signal model, ideal supplies",
        "measurements": measurements
        if measurements is not None
        else [_meas("gain", "gain")],
    }
    if points is not None:
        env["corners"] = points
    env.update(extra)
    return env


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _item(item_id, evidence, spec_rows=None, kind="analog"):
    manifest = {"block": "b", "kind": kind, "evidence": evidence}
    if spec_rows is not None:
        manifest["spec_rows"] = {str(item_id): spec_rows}
    return next(i for i in build_tier_report(manifest)["items"] if i["id"] == item_id)


def test_met_derives_rows_and_discloses_method(tmp_path):
    f = _write(tmp_path, "m.json", _env())
    item = _item(5, {"5": [{"file": f, "rows": ["gain"]}]}, [{"id": "gain"}])
    assert item["status"] == "met"
    assert item["row_coverage"]["row_sources"] == {"gain": "derived"}
    cit = (
        item["citation"][0] if isinstance(item["citation"], list) else item["citation"]
    )
    assert cit["kind"] == "model-run"
    assert cit["executed_by_klt"] is False
    assert "linear" in cit["method"]


def test_uncovered_row_is_partial_coverage(tmp_path):
    f = _write(tmp_path, "m.json", _env())
    item = _item(
        5, {"5": [{"file": f, "rows": ["gain", "bw"]}]}, [{"id": "gain"}, {"id": "bw"}]
    )
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["uncovered_rows"] == ["bw"]


def test_failing_point_is_check_failed(tmp_path):
    pts = [
        {"corner_id": "tt", "status": "pass"},
        {"corner_id": "ss", "status": "fail"},
    ]
    f = _write(tmp_path, "m.json", _env(points=pts))
    item = _item(5, {"5": [{"file": f, "rows": ["gain"]}]}, [{"id": "gain"}])
    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"


def test_no_limits_is_refused(tmp_path):
    f = _write(tmp_path, "m.json", _env([_meas("gain", "gain", limits=None)]))
    item = _item(5, {"5": f})
    assert item["status"] == "unmet"
    assert item["reason"] == "no_limits_checked"


def test_other_items_reject_model_run(tmp_path):
    f = _write(tmp_path, "m.json", _env())
    assert _item(3, {"3": f})["reason"] == "wrong_kind"


def test_stale_pinned_hash(tmp_path):
    record = tmp_path / "model.md"
    record.write_text("v1")
    good = "sha256:" + hashlib.sha256(b"v1").hexdigest()
    env = _env(provenance={"input": {"path": "model.md", "content_hash": "sha256:bad"}})
    f = _write(tmp_path, "m.json", env)
    item = _item(5, {"5": f})
    assert item["citation"]["input_verified"] is False
    env["provenance"]["input"]["content_hash"] = good
    f = _write(tmp_path, "m.json", env)
    item = _item(5, {"5": f})
    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is True


def test_digital_model_run_alone_needs_timing_margin(tmp_path):
    f = _write(tmp_path, "m.json", _env())
    item = _item(5, {"5": f}, kind="digital")
    assert item["status"] == "unmet"
    assert item["reason"] == "timing_margin_missing"
