"""Signoff consumes a multi-library simulation grid using its shared netlist."""

from __future__ import annotations

import copy
import json

import pytest

from klayout_tools import sim, sim_merge
from klayout_tools.cli import main
from klayout_tools.signoff import build_signoff, build_tier_report
from klayout_tools.signoff_evidence_pin import common_input_hash
from klayout_tools.signoff_repin import repin_manifest
from test_sim_merge import _plan, _write, _write_grid_request
from test_sim_merge import stub_ngspice as stub_ngspice

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


@pytest.fixture
def grid(tmp_path, stub_ngspice):
    (tmp_path / ".git").mkdir()
    members = {}
    for process in ("tt", "ss"):
        path = _write_grid_request(tmp_path, process + ".json", [process])
        request = json.loads(path.read_text())
        library = tmp_path / (process + ".lib")
        library.write_text(f".lib {process}\n.param test_corner=1\n.endl {process}\n")
        request["models"]["lib"] = library.name
        request["options"] = {"measurement_cases": ["out"]}
        request["measurements"][0].update(
            name="v{case}",
            spice=".meas tran v{case} FIND v({case}) AT=1u",
            spec_row="output",
            limits={"min": 0.88, "max": 1.2},
        )
        path.write_text(json.dumps(request))
        members[process] = sim.run_sim(
            str(path), backend="local", artifacts_dir=str(tmp_path / process)
        )
    return _plan(tmp_path, members), members


def _merge(tmp_path, plan, members):
    paths = [
        _write(tmp_path, name + ".report", report) for name, report in members.items()
    ]
    return sim_merge.merge_reports(plan, paths[::-1])


def _manifest(path, digest):
    return {
        "block": "buffer",
        "kind": "analog",
        "require_derived_rows": True,
        "spec_rows": {"5": [{"id": "output"}]},
        "evidence": {"5": [{"file": path, "rows": ["output"], "content_hash": digest}]},
    }


def _item5(tmp_path, manifest):
    result = build_tier_report(manifest, repo_root=tmp_path)
    return next(item for item in result["items"] if item["id"] == 5)


def test_shared_netlist_grades_the_grid_and_repins(tmp_path, grid):
    plan, members = grid
    merged = _merge(tmp_path, plan, members)
    measurement = merged["measurements"][0]
    assert measurement["spec_row"] == "output"
    assert measurement["case"] == "out"
    assert measurement["status"] == "pass"
    assert [measurement[k]["value"] for k in ("min", "max", "worst_case")] == [
        0.9,
        1.0,
        0.9,
    ]
    assert merged["coverage"]["nothing_checked"] is False
    assert merged["coverage"]["measurements_with_limits"] == 1
    assert [row["process"] for row in merged["corners"]] == ["tt", "tt", "ss", "ss"]
    digest = members["tt"]["provenance"]["input"]["content_hash"]
    assert merged["provenance"]["input"]["content_hash"] == [digest, digest]
    assert len(merged["provenance"]["deck"]) == 2
    path = _write(tmp_path, "merged.json", merged)
    manifest = _manifest(path, digest)
    item = _item5(tmp_path, manifest)
    assert item["status"] == "met"
    assert item["row_coverage"]["row_sources"] == {"output": "derived"}
    assert item["citation"]["input_verified"] is True
    assert build_signoff([path, str(tmp_path / "ss.report")])["provenance_consistency"][
        "ok"
    ]
    manifest["evidence"]["5"][0]["content_hash"] = "old"
    assert _item5(tmp_path, manifest)["reason"] == "stale_evidence"
    mp = _write(tmp_path, "manifest.json", manifest)
    assert repin_manifest(mp, repo_root=str(tmp_path))["changed_count"] == 1
    assert (
        json.loads((tmp_path / "manifest.json").read_text())["evidence"]["5"][0][
            "content_hash"
        ]
        == digest
    )


@pytest.mark.parametrize("foreign", ["different", None])
def test_mixed_input_hashes_are_unverifiable(tmp_path, grid, foreign):
    plan, members = grid
    members["ss"]["provenance"]["input"]["content_hash"] = foreign
    merged = _merge(tmp_path, plan, members)
    digest = members["tt"]["provenance"]["input"]["content_hash"]
    path = _write(tmp_path, "merged.json", merged)
    item = _item5(tmp_path, _manifest(path, digest))
    assert item["reason"] == "unverifiable_provenance"
    assert "mixed or missing per-shard" in item["detail"]["content_hash_pin_note"]
    for paths in ([path], [path, str(tmp_path / "tt.report")]):
        consistency = build_signoff(paths)["provenance_consistency"]
        assert not consistency["ok"]
        (mismatch,) = consistency["mismatches"]
        assert mismatch["role"] == "netlist"
        assert json.loads(json.dumps(mismatch))["values"][0]["value"] == [
            digest,
            foreign,
        ]


@pytest.mark.parametrize("field", ["spec_row", "case"])
@pytest.mark.parametrize("remove", [True, False])
def test_inconsistent_measurement_provenance_refused(
    tmp_path, capsys, grid, field, remove
):
    plan, members = grid
    changed = copy.deepcopy(members["ss"])
    if remove:
        del changed["measurements"][0][field]
    else:
        changed["measurements"][0][field] = "other"
    left = _write(tmp_path, "tt.report", members["tt"])
    right = _write(tmp_path, "ss.report", changed)
    assert main(["sim-merge", plan, right, left, "--format", "json"]) == 10
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "refused"
    assert result["refusal"]["code"] == "inconsistent_shards"


@pytest.mark.parametrize("missing", ["tt", "ss"])
def test_missing_provenance_keeps_the_missing_shards_slot(tmp_path, grid, missing):
    plan, members = grid
    del members[missing]["provenance"]
    other = "ss" if missing == "tt" else "tt"
    digest = members[other]["provenance"]["input"]["content_hash"]
    merged = _merge(tmp_path, plan, members)
    assert merged["provenance"]["input"]["content_hash"] == [
        None if process == missing else digest for process in members
    ]
    path = _write(tmp_path, "merged.json", merged)
    assert (
        _item5(tmp_path, _manifest(path, digest))["reason"] == "unverifiable_provenance"
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("sha256:a", "sha256:a"),
        ("", ""),
        (None, None),
        (["sha256:a", "sha256:a"], "sha256:a"),
        (["", ""], ""),
        (["sha256:a", "sha256:b"], None),
        (["sha256:a", None], None),
        ([None, None], None),
        ([], None),
    ],
)
def test_common_input_hash_only_a_list_reduces_to_none(value, expected):
    """A scalar -- ``""`` included -- is echoed back verbatim (#6403); only an
    empty or non-uniform shard list collapses to ``None``."""
    assert common_input_hash(value) == expected


def test_empty_string_digest_is_not_a_provenance_mismatch(tmp_path, grid):
    """``_check_input_hashes`` must not read a consistent ``""`` digest -- one
    report, or the same ``""`` on every shard -- as a mismatch with itself."""
    plan, members = grid
    for report in members.values():
        report["provenance"]["input"]["content_hash"] = ""
    single = _write(tmp_path, "single.json", members["tt"])
    merged = _write(tmp_path, "merged.json", _merge(tmp_path, plan, members))
    for paths in ([single], [merged], [single, merged]):
        consistency = build_signoff(paths)["provenance_consistency"]
        assert consistency["mismatches"] == []
        assert consistency["ok"]
