"""Module-contract tests for `klayout_tools._git_tracked` (issue #5857).

The never-raise probe (`build_identity._git`'s contract, extended to the
trackedness helper) and the one-`ls-files`-per-distinct-root-per-render
budget, driven through `build_tier_report` exactly as `klt signoff`
renders. The render-level regressions for the index-basis filter live in
`tests/test_signoff_input_index_basis.py`, which owns the
`_git`/`_git_checkout`/`_repo_scoped_generic` helpers reused here.

Lives beside `tests/test_signoff.py` rather than in it (that file is at
its size-ratchet ceiling), reusing its envelope fixtures.
"""

from __future__ import annotations

from klayout_tools import _git_tracked
from klayout_tools.signoff import build_tier_report
from test_signoff import _hash_of, _item, _manifest
from test_signoff_input_index_basis import (
    _git,
    _git_checkout,
    _repo_scoped_generic,
)


def test_git_unavailable_falls_back_to_filesystem_presence(tmp_path, monkeypatch):
    """`build_identity._git`'s never-raise contract, extended to the
    trackedness probe: with git effectively absent (every invocation
    fails), the helper must not raise, and the fallback keeps today's
    filesystem-presence behaviour rather than failing every citation."""

    repo = _git_checkout(tmp_path)
    scratch = repo / "scratch"
    scratch.mkdir()
    artifact = scratch / "cellA.gds"
    artifact.write_bytes(b"GDS-A")
    envelope_path, recorded = _repo_scoped_generic(
        repo, path="scratch/cellA.gds", content_hash=_hash_of(artifact), item_id=1
    )
    monkeypatch.setattr(_git_tracked, "_git_stdout", lambda cwd, *args: None)

    result = build_tier_report(
        _manifest(evidence={"1": {"file": envelope_path, "content_hash": recorded}})
    )

    item = _item(result, 1)
    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is True


def test_one_ls_files_invocation_per_distinct_root_per_render(tmp_path, monkeypatch):
    """The budget: however many citations a manifest carries, one bulk
    `git ls-files` per distinct root -- memoized, never per candidate."""

    real = _git_tracked._git_stdout
    ls_files_calls: list[str] = []

    def counting(cwd, *args):
        if "ls-files" in args:
            ls_files_calls.append(cwd)
        return real(cwd, *args)

    repo = _git_checkout(tmp_path)
    artifact = repo / "ota.gds"
    artifact.write_text("GDS-BYTES")
    envelope_path, digest = _repo_scoped_generic(
        repo, path="ota.gds", content_hash=_hash_of(artifact), item_id=2
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "evidence")

    monkeypatch.setattr(_git_tracked, "_git_stdout", counting)
    build_tier_report(
        _manifest(
            evidence={
                "2": {"file": envelope_path, "content_hash": digest},
                # A second citation of the same repo -- same root, and the
                # re-hash of the same artifact through a second envelope.
                "1": {"file": envelope_path, "content_hash": digest},
            }
        )
    )
    assert ls_files_calls == [str(repo)]

    other = _git_checkout(tmp_path, name="other")
    other_artifact = other / "ota.gds"
    other_artifact.write_text("GDS-OTHER")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "evidence")
    other_path, other_digest = _repo_scoped_generic(
        other, path="ota.gds", content_hash=_hash_of(other_artifact), item_id=9
    )

    build_tier_report(
        _manifest(
            evidence={
                "2": {"file": envelope_path, "content_hash": digest},
                "9": {"file": other_path, "content_hash": other_digest},
            }
        )
    )
    # `repo` answered from the memo; only the new distinct root paid again.
    assert sorted(ls_files_calls) == [str(other), str(repo)]
