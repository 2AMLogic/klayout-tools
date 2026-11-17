"""Yield samples use the git index, including unresolved-path diagnostics (#6139).

Separate from test_signoff_input_index_basis.py to respect its size ratchet.
"""

from __future__ import annotations

import json
import sys

import pytest

from klayout_tools.signoff import build_tier_report
from test_signoff import YIELD_PASS_ENVELOPE, _hash_of, _item, _manifest, _write
from test_signoff_input_index_basis import _git, _git_checkout


@pytest.mark.parametrize("gitignore", ["scratch/\n", ""])
@pytest.mark.parametrize("command_backed", [False, True])
def test_untracked_yield_samples_render_identically_present_and_absent(
    tmp_path, monkeypatch, gitignore, command_backed
):
    repo = _git_checkout(tmp_path, gitignore=gitignore)
    monkeypatch.chdir(repo)
    samples = repo / "scratch" / "samples.json"
    samples.parent.mkdir()
    samples.write_text('{"measurements": []}\n')
    envelope = {**YIELD_PASS_ENVELOPE, "samples": "scratch/samples.json"}
    report = _write(repo, "yield.json", envelope)
    _git(repo, "add", "yield.json")
    _git(repo, "commit", "-qm", "yield evidence without samples")
    evidence = (
        {
            "command": [sys.executable, "-c", f"print({json.dumps(envelope)!r})"],
            "cwd": str(repo),
        }
        if command_backed
        else report
    )
    manifest = _manifest(evidence={"6": evidence})
    if command_backed:
        monkeypatch.chdir(tmp_path)  # Only the producing command runs in repo.

    present = build_tier_report(manifest)
    samples.unlink()
    absent = build_tier_report(manifest)

    assert json.dumps(present, sort_keys=True) == json.dumps(absent, sort_keys=True)
    citation = _item(present, 6)["citation"]
    assert citation["content_hash"] is None
    assert citation["input_verified"] is None
    assert citation["content_hash_unresolved"] == {
        "samples": "scratch/samples.json",
        "searched": [],
    }


@pytest.mark.parametrize("tracked_location", ["report", "cwd", "both"])
def test_yield_samples_only_hash_tracked_candidates(
    tmp_path, monkeypatch, tracked_location
):
    repo = _git_checkout(tmp_path)
    monkeypatch.chdir(repo)
    report_dir = repo / "reports"
    report_dir.mkdir()
    report_samples = report_dir / "samples.json"
    report_samples.write_text("report-relative samples\n")
    cwd_samples = repo / "samples.json"
    cwd_samples.write_text("cwd-relative samples\n")
    report = _write(
        report_dir, "yield.json", {**YIELD_PASS_ENVELOPE, "samples": "samples.json"}
    )
    selected = cwd_samples if tracked_location == "cwd" else report_samples
    _git(repo, "add", str(selected))
    if tracked_location == "both":
        _git(repo, "add", str(cwd_samples))
    _git(repo, "add", "reports/yield.json")
    _git(repo, "commit", "-qm", "tracked samples and yield evidence")

    citation = _item(build_tier_report(_manifest(evidence={"6": report})), 6)[
        "citation"
    ]

    assert citation["content_hash"] == _hash_of(selected)
    assert citation["input_verified"] is True
    assert "content_hash_unresolved" not in citation

    # Tracked-but-missing candidates remain in the post-filter diagnostic.
    report_samples.unlink()
    cwd_samples.unlink()
    missing = _item(build_tier_report(_manifest(evidence={"6": report})), 6)["citation"]
    assert missing["content_hash_unresolved"]["searched"] == (
        [str(report_samples), "samples.json"]
        if tracked_location == "both"
        else ["samples.json"]
        if tracked_location == "cwd"
        else [str(report_samples)]
    )


@pytest.mark.parametrize("explicit_root", [False, True])
def test_yield_samples_in_gitless_tree_still_hash(tmp_path, monkeypatch, explicit_root):
    monkeypatch.chdir(tmp_path)
    samples = tmp_path / "samples.json"
    samples.write_text('{"measurements": []}\n')
    report = _write(
        tmp_path, "yield.json", {**YIELD_PASS_ENVELOPE, "samples": "samples.json"}
    )

    result = build_tier_report(
        _manifest(evidence={"6": report}),
        repo_root=str(tmp_path) if explicit_root else None,
    )

    citation = _item(result, 6)["citation"]
    assert citation["content_hash"] == _hash_of(samples)
    assert citation["input_verified"] is True
    assert "content_hash_unresolved" not in citation


def test_tracked_yield_samples_resolve_from_command_cwd(tmp_path, monkeypatch):
    repo = _git_checkout(tmp_path)
    samples = repo / "samples.json"
    samples.write_text('{"measurements": []}\n')
    _git(repo, "add", "samples.json")
    _git(repo, "commit", "-qm", "samples")
    monkeypatch.chdir(tmp_path)
    envelope = {**YIELD_PASS_ENVELOPE, "samples": "samples.json"}

    result = build_tier_report(
        _manifest(
            evidence={
                "6": {
                    "command": [
                        sys.executable,
                        "-c",
                        f"print({json.dumps(envelope)!r})",
                    ],
                    "cwd": str(repo),
                }
            }
        )
    )

    citation = _item(result, 6)["citation"]
    assert citation["content_hash"] == _hash_of(samples)
    assert citation["input_verified"] is True
