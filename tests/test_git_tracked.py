"""Module-contract tests for `klayout_tools._git_tracked` (issue #5857).

The never-raise probe (`build_identity._git`'s contract, extended to the
trackedness helper) and the one-`ls-files`-per-distinct-root-per-render
budget, driven through `build_tier_report` exactly as `klt signoff`
renders. The render-level regressions for the plain untracked-file filter
live in `tests/test_signoff_input_index_basis.py`, which owns the
`_git`/`_git_checkout`/`_repo_scoped_generic` helpers reused here; the
#7195 regressions below (untracked symlinks, untracked `dir/..`
components, unrepresentable names -- the input side of #6139's contract)
also live here, that file being at its ratchet ceiling.

Lives beside `tests/test_signoff.py` rather than in it (that file is at
its size-ratchet ceiling), reusing its envelope fixtures.
"""

from __future__ import annotations

import json
import shutil

from klayout_tools import _git_tracked
from klayout_tools.signoff import _resolve_input_artifact_value, build_tier_report
from klayout_tools.signoff_attest_canonical_yaml import rehash_checks
from test_signoff import DRC_CLEAN_ENVELOPE, _hash_of, _item, _manifest, _write
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


# --------------------------------------------------------------------------- #
# Issue #7195: the input-artifact side of #6139's contract -- a candidate
# reached through an untracked symlink or an untracked `dir/..` component
# is as untracked as a local copy of the same bytes, and an unrepresentable
# name never raises.
# --------------------------------------------------------------------------- #


def _anchored_manifest(repo, item_id: int, *, path: str, content: bytes | str):
    """An artifact-anchored `generic` citation of `path` over a tracked
    `content` blob -- the T1 1/2/9/10 shape whose verdict flips with
    `input_verified`."""
    artifact = repo / "ota.gds"
    artifact.write_text(content)
    envelope_path, digest = _repo_scoped_generic(
        repo, path=path, content_hash=_hash_of(artifact), item_id=item_id
    )
    _git(repo, "add", str(artifact))
    return (
        _manifest(
            evidence={str(item_id): {"file": envelope_path, "content_hash": digest}}
        ),
        digest,
    )


def test_gitignored_symlink_to_tracked_input_renders_like_a_fresh_clone(tmp_path):
    """The issue's own case: a purely local, gitignored symlink to a
    tracked artifact verified `true` on the host that had the link and
    `null` in a clone -- the #5857 non-determinism, back through a link."""
    repo = _git_checkout(tmp_path)
    scratch = repo / "scratch"
    scratch.mkdir()
    (scratch / "link.gds").symlink_to("../ota.gds")
    manifest, _ = _anchored_manifest(
        repo, 2, path="scratch/link.gds", content="GDS-BYTES"
    )
    _git(repo, "commit", "-qm", "tracked target, gitignored link")

    present = build_tier_report(manifest)
    item = _item(present, 2)
    assert item["status"] == "unmet"
    assert item["reason"] == "unverifiable_provenance"
    assert item["citation"] is None

    (scratch / "link.gds").unlink()
    absent = build_tier_report(manifest)

    assert json.dumps(absent, sort_keys=True) == json.dumps(present, sort_keys=True)


def test_drc_input_through_gitignored_symlink_renders_like_a_fresh_clone(tmp_path):
    """The disclosure-only side (a `drc` item-3 citation): the render must
    not depend on the link existing either. The link is named differently
    from its target so the beside-the-evidence basename candidate of issue
    #2340 cannot mask the case."""
    repo = _git_checkout(tmp_path)
    artifact = repo / "ota.gds"
    artifact.write_bytes(b"GDS-A")
    scratch = repo / "scratch"
    scratch.mkdir()
    (scratch / "link.gds").symlink_to("../ota.gds")
    envelope = {
        **DRC_CLEAN_ENVELOPE,
        "file": str(scratch / "link.gds"),
        "provenance": {
            **DRC_CLEAN_ENVELOPE["provenance"],
            "input": {"content_hash": _hash_of(artifact), "role": "layout"},
        },
    }
    drc_path = _write(repo, "drc.json", envelope)
    _git(repo, "add", "ota.gds", "drc.json")
    _git(repo, "commit", "-qm", "tracked target and drc evidence")
    manifest = _manifest(evidence={"3": drc_path})

    present = build_tier_report(manifest)
    assert _item(present, 3)["citation"]["input_verified"] is None

    (scratch / "link.gds").unlink()
    absent = build_tier_report(manifest)

    assert json.dumps(absent, sort_keys=True) == json.dumps(present, sort_keys=True)


def test_untracked_symlink_input_grades_null_until_the_link_is_tracked(tmp_path):
    """No gitignore involved: a never-committed link is untracked exactly
    like a never-committed file, and tracking the link itself (git indexes
    a symlink by its target, so a clone recreates it) turns the citation
    verifiable everywhere."""
    repo = _git_checkout(tmp_path)
    (repo / "untracked_link.gds").symlink_to("ota.gds")
    manifest, _ = _anchored_manifest(
        repo, 2, path="untracked_link.gds", content="GDS-BYTES"
    )
    _git(repo, "commit", "-qm", "tracked target, untracked link")

    present = build_tier_report(manifest)
    assert _item(present, 2)["reason"] == "unverifiable_provenance"

    (repo / "untracked_link.gds").unlink()
    assert json.dumps(build_tier_report(manifest), sort_keys=True) == json.dumps(
        present, sort_keys=True
    )

    (repo / "untracked_link.gds").symlink_to("ota.gds")
    _git(repo, "add", "untracked_link.gds")
    _git(repo, "commit", "-qm", "track the link")
    item = _item(build_tier_report(manifest), 2)

    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is True


def test_input_parent_step_needs_an_indexed_directory(tmp_path):
    """`scratch/../ota.gds`: a clone has no `scratch/` at all, so the
    producing host must grade the citation exactly as that clone does --
    until a child of the directory is indexed, which is the moment a clone
    recreates the directory."""
    repo = _git_checkout(tmp_path)
    (repo / "scratch").mkdir()
    manifest, _ = _anchored_manifest(
        repo, 2, path="scratch/../ota.gds", content="GDS-BYTES"
    )
    _git(repo, "commit", "-qm", "tracked target, empty ignored directory")

    present = build_tier_report(manifest)
    assert _item(present, 2)["reason"] == "unverifiable_provenance"

    (repo / "scratch").rmdir()
    assert json.dumps(build_tier_report(manifest), sort_keys=True) == json.dumps(
        present, sort_keys=True
    )

    (repo / "scratch").mkdir()
    (repo / "scratch" / "tracked").touch()
    _git(repo, "add", "-f", "scratch/tracked")
    item = _item(build_tier_report(manifest), 2)

    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is True


def test_input_symlink_chain_requires_each_link_tracked(tmp_path):
    """`first.gds -> second.gds -> ota.gds`: the cited link and the target
    being tracked is not enough -- every link a lookup traverses must be
    indexed, or the bytes are reachable only through local state."""
    repo = _git_checkout(tmp_path)
    (repo / "first.gds").symlink_to("second.gds")
    (repo / "second.gds").symlink_to("ota.gds")
    manifest, _ = _anchored_manifest(repo, 2, path="first.gds", content="GDS-BYTES")
    _git(repo, "add", "ota.gds", "first.gds", "item2.json")
    _git(repo, "commit", "-qm", "target and cited link only")

    assert _item(build_tier_report(manifest), 2)["reason"] == "unverifiable_provenance"

    _git(repo, "add", "second.gds")
    item = _item(build_tier_report(manifest), 2)

    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is True


def test_unrepresentable_input_name_grades_unverifiable_not_crash(tmp_path):
    """A name no filesystem call can represent (an embedded NUL byte) is
    not a render-aborting `ValueError` from the trackedness probe: the
    candidate stays attempted, nothing hashes, the citation grades
    `unverifiable_provenance`."""
    repo = _git_checkout(tmp_path)
    manifest, _ = _anchored_manifest(repo, 2, path="bad\0name.gds", content="GDS-BYTES")

    item = _item(build_tier_report(manifest), 2)

    assert item["status"] == "unmet"
    assert item["reason"] == "unverifiable_provenance"
    assert item["citation"] is None


def test_unrepresentable_drc_file_name_grades_null_not_crash(tmp_path):
    """The same through a `drc` citation's native `file` string: the
    cwd-relative and beside-evidence candidates both carry the NUL byte,
    and the render stays a render."""
    repo = _git_checkout(tmp_path)
    envelope = {
        **DRC_CLEAN_ENVELOPE,
        "file": "bad\0name.gds",
        "provenance": {
            **DRC_CLEAN_ENVELOPE["provenance"],
            "input": {"content_hash": "sha256:" + "0" * 64, "role": "layout"},
        },
    }
    drc_path = _write(repo, "drc.json", envelope)

    result = build_tier_report(_manifest(evidence={"3": drc_path}))

    assert _item(result, 3)["citation"]["input_verified"] is None


def test_unrepresentable_yaml_attest_path_is_unverifiable_not_crash(tmp_path):
    """The same name through the `attest: "yaml"` re-hash: `open()` raises
    `ValueError` where the raw-byte path's `os.path.isfile` guard returns
    `False`, so the re-hash must grade it `unverifiable`, not abort."""
    repo = _git_checkout(tmp_path)
    recorded = "sha256:" + "0" * 64
    resolution = {
        "envelope": {
            "attestation": {
                "checks": [
                    {
                        "check": "present",
                        "path": "bad\0ci.yml",
                        "attest": "yaml",
                        "content_hash": recorded,
                    }
                ]
            }
        },
        "file": str(repo / "item10.json"),
        "spec": {"cwd": str(repo)},
    }

    assert rehash_checks(resolution, None, _resolve_input_artifact_value) == [
        {
            "path": {"path": "bad\0ci.yml", "scope": "repo"},
            "content_hash": recorded,
            "status": "unverifiable",
        }
    ]


def test_gitless_copy_keeps_presence_for_links_traversal_and_invalid_names(
    tmp_path,
):
    """The #2878/#5857 carve-out after the fold: a `.git`-less copy graded
    with `--repo-root` has no index to ask, so a symlink, a `dir/..` path
    and even an unrepresentable name keep the filesystem-presence
    behaviour -- the first two verify, the last grades unresolvable
    without raising."""
    repo = _git_checkout(tmp_path)
    scratch = repo / "scratch"
    scratch.mkdir()
    (scratch / "link.gds").symlink_to("../ota.gds")
    artifact = repo / "ota.gds"
    artifact.write_text("GDS-BYTES")
    linked, linked_digest = _repo_scoped_generic(
        repo, path="scratch/link.gds", content_hash=_hash_of(artifact), item_id=1
    )
    traversed, traversed_digest = _repo_scoped_generic(
        repo, path="scratch/../ota.gds", content_hash=_hash_of(artifact), item_id=2
    )
    invalid, invalid_digest = _repo_scoped_generic(
        repo, path="bad\0name.gds", content_hash=_hash_of(artifact), item_id=9
    )
    copy = tmp_path / "archive-copy"
    shutil.copytree(repo, copy)
    shutil.rmtree(copy / ".git")

    def graded(envelope, digest, item_id):
        return _item(
            build_tier_report(
                _manifest(
                    evidence={
                        str(item_id): {
                            "file": str(copy / f"item{item_id}.json"),
                            "content_hash": digest,
                        }
                    }
                ),
                repo_root=str(copy),
            ),
            item_id,
        )

    for envelope, digest, item_id in (
        (linked, linked_digest, 1),
        (traversed, traversed_digest, 2),
    ):
        item = graded(envelope, digest, item_id)
        assert item["status"] == "met"
        assert item["citation"]["input_verified"] is True

    item = graded(invalid, invalid_digest, 9)
    assert item["status"] == "unmet"
    assert item["reason"] == "unverifiable_provenance"


def test_shared_predicate_contract_in_a_real_checkout(tmp_path):
    """The folded predicate's own contract, as the issue measured it: the
    same answers the yield path already gave, now from the one shared
    basis -- and never an exception."""
    repo = _git_checkout(tmp_path)
    data = repo / "data.json"
    data.write_text("{}")
    _git(repo, "add", "data.json")
    _git(repo, "commit", "-qm", "tracked")
    scratch = repo / "scratch"
    scratch.mkdir()
    (scratch / "link.json").symlink_to("../data.json")
    (repo / "untracked_link.json").symlink_to("data.json")
    predicate = _git_tracked.is_untracked_working_tree_file

    assert predicate(str(data)) is False
    assert predicate(str(scratch / "link.json")) is True
    assert predicate(str(repo / "untracked_link.json")) is True
    assert predicate(str(scratch / ".." / "data.json")) is True
    assert predicate(str(repo / "bad\0name.json")) is False
