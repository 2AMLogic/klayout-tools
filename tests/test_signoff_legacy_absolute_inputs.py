"""Legacy absolute input paths can verify against files beside signoff evidence."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from klayout_tools import signoff as signoff_module
from klayout_tools.signoff import build_tier_report
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    GENERIC_PASS_ENVELOPE_WITH_PROVENANCE,
    _hash_of,
    _manifest,
    _write,
)

#: An absolute host path from some other checkout -- never present here.
_FOREIGN_ABS_LAYOUT = "/nonexistent/other-host/worktree-9876/out/block.gds"


def _drc_evidence_with_absolute_file(
    evidence_dir: Path, *, file_value: str, recorded_hash: str
) -> str:
    envelope = {
        **DRC_CLEAN_ENVELOPE,
        "file": file_value,
        "provenance": {
            **DRC_CLEAN_ENVELOPE["provenance"],
            "input": {"content_hash": recorded_hash, "role": "layout"},
        },
    }
    return _write(evidence_dir, "drc.json", envelope)


def test_citation_input_verified_true_for_absolute_path_basename_beside_evidence(
    tmp_path, monkeypatch
):
    """The issue's reproduction: the recorded absolute path is absent here,
    but the matching artifact is committed beside the envelope under the same
    basename. The grading cwd differs from the evidence dir, proving the
    fallback is anchored on the evidence file, not on cwd."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    layout_path = evidence_dir / "block.gds"
    layout_path.write_bytes(b"GDS-A")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "block.gds").write_bytes(b"a decoy in the grading cwd")
    monkeypatch.chdir(elsewhere)
    drc_path = _drc_evidence_with_absolute_file(
        evidence_dir,
        file_value=_FOREIGN_ABS_LAYOUT,
        recorded_hash=_hash_of(layout_path),
    )

    result = build_tier_report(_manifest(evidence={"3": drc_path}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"
    assert item_3["citation"]["input_verified"] is True


def test_citation_input_verified_false_for_absolute_path_basename_mismatch(tmp_path):
    """A same-named file beside the evidence with different bytes is read and
    disagrees: `False`, never `True` on its name alone."""
    layout_path = tmp_path / "block.gds"
    layout_path.write_bytes(b"GDS-B -- not what the report hashed")
    drc_path = _drc_evidence_with_absolute_file(
        tmp_path,
        file_value=_FOREIGN_ABS_LAYOUT,
        recorded_hash="sha256:" + hashlib.sha256(b"GDS-A").hexdigest(),
    )

    result = build_tier_report(_manifest(evidence={"3": drc_path}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"
    assert item_3["citation"]["input_verified"] is False


def test_citation_input_verified_original_absolute_path_match_wins(tmp_path):
    """When the recorded absolute path *does* exist and matches, it wins over
    a different-content file of the same basename beside the evidence."""
    original_dir = tmp_path / "original"
    original_dir.mkdir()
    original_layout = original_dir / "block.gds"
    original_layout.write_bytes(b"GDS-A")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "block.gds").write_bytes(b"GDS-B -- unrelated same-named file")
    drc_path = _drc_evidence_with_absolute_file(
        evidence_dir,
        file_value=str(original_layout),
        recorded_hash=_hash_of(original_layout),
    )

    result = build_tier_report(_manifest(evidence={"3": drc_path}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["citation"]["input_verified"] is True


@pytest.mark.parametrize("kind", ["drc", "extract", "erc"])
def test_absolute_path_basename_candidate_for_file_backed_kinds(tmp_path, kind):
    """The shared resolver serves every `file`-field kind identically: true on
    a nearby match, false on a nearby mismatch, null when nothing is nearby."""
    evidence_file = str(tmp_path / f"{kind}.json")
    envelope = {"file": _FOREIGN_ABS_LAYOUT}
    spec = {"kind": "file", "path": evidence_file}
    recorded = "sha256:" + hashlib.sha256(b"GDS-A").hexdigest()

    candidates = signoff_module._input_artifact_candidates(
        kind, envelope, spec, evidence_file
    )
    assert candidates == [_FOREIGN_ABS_LAYOUT, str(tmp_path / "block.gds")]

    assert (
        signoff_module._verify_input_artifact(
            kind, envelope, spec, recorded, evidence_file
        )
        is None
    )
    (tmp_path / "block.gds").write_bytes(b"GDS-B")
    assert (
        signoff_module._verify_input_artifact(
            kind, envelope, spec, recorded, evidence_file
        )
        is False
    )
    (tmp_path / "block.gds").write_bytes(b"GDS-A")
    assert (
        signoff_module._verify_input_artifact(
            kind, envelope, spec, recorded, evidence_file
        )
        is True
    )


def test_resolver_relative_and_mapping_shapes_unchanged(tmp_path, monkeypatch):
    """Non-regression for every other value shape: a relative string still
    yields its as-named and evidence-relative candidates (no basename
    candidate added); a repo-scoped mapping resolves against the repo root;
    an external/null mapping stays unresolvable; no evidence dir means no
    basename candidate either."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    evidence_dir = str(tmp_path / "evidence")
    resolve = signoff_module._resolve_input_artifact_value
    spec = {"kind": "file"}

    assert resolve("out/block.gds", spec, evidence_dir) == [
        "out/block.gds",
        os.path.join(evidence_dir, "out/block.gds"),
    ]
    assert resolve({"path": "out/block.gds", "scope": "repo"}, spec, evidence_dir) == [
        os.path.join(str(tmp_path), "out/block.gds")
    ]
    assert resolve({"path": None, "scope": "external"}, spec, evidence_dir) == []
    assert resolve(_FOREIGN_ABS_LAYOUT, spec, None) == [_FOREIGN_ABS_LAYOUT]


def test_generic_citation_absolute_opted_in_path_uses_basename_candidate(tmp_path):
    """A `generic` envelope's opt-in `provenance.input.path` shares the same
    resolution convention: an absolute string from another host verifies
    against the same-named artifact beside the evidence."""
    report_path = tmp_path / "2026-q3-report.md"
    report_path.write_text("Q3 characterization sweep: all spec rows within limits")
    envelope = {
        **GENERIC_PASS_ENVELOPE_WITH_PROVENANCE,
        "provenance": {
            **GENERIC_PASS_ENVELOPE_WITH_PROVENANCE["provenance"],
            "input": {
                "content_hash": _hash_of(report_path),
                "path": "/nonexistent/other-host/reports/2026-q3-report.md",
            },
        },
    }
    generic_path = _write(tmp_path, "characterization.json", envelope)

    result = build_tier_report(_manifest(evidence={"8": generic_path}))

    item_8 = next(item for item in result["items"] if item["id"] == 8)
    assert item_8["status"] == "met"
    assert item_8["citation"]["input_verified"] is True
