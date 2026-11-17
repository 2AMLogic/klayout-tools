"""`klt signoff` grades input artifacts by the git index, not the working
tree (issue #5857): a candidate inside a functioning checkout whose index
does not track it is dropped by `_resolve_input_artifact_value` before
anything reads it, so an evidence render is a function of the commit, not
of the producing host's working tree. The predicate lives in
`klayout_tools._git_tracked`; its module-contract tests (git unavailable,
the `ls-files` budget) live in `tests/test_git_tracked.py`. Lives beside
`tests/test_signoff.py` rather than in it (that file is at its
size-ratchet ceiling), reusing its envelope fixtures.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from klayout_tools.signoff import build_tier_report, input_set_digest
from test_signoff import (
    _ANCHORED_ARTIFACTS,
    DRC_CLEAN_ENVELOPE,
    GENERIC_PASS_ENVELOPE,
    GENERIC_PASS_ENVELOPE_WITH_PROVENANCE,
    PEX_PASS_ENVELOPE,
    _hash_of,
    _item,
    _item7_citation,
    _manifest,
    _sha_of,
    _write,
)

# --------------------------------------------------------------------------- #
# Issue #5857: the git index, not the working tree, is the grading basis
# --------------------------------------------------------------------------- #


def _git(root, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=True, timeout=60
    )


def _git_checkout(tmp_path, name: str = "repo", *, gitignore: str = "scratch/\n"):
    """A real git checkout -- unlike the bare `.git`-marked directories the
    suites above use (which only anchor `find_repo_root`, exercise no
    index, and keep the filesystem-presence fallback). Returns a repository
    with one commit, so its index actually answers trackedness questions
    and `git ls-files`/`rev-parse` behave as they do over a clone."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(gitignore)
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "init")
    return repo


def _repo_scoped_generic(repo, *, path: str, content_hash: str, item_id=None):
    """An opt-in `generic` envelope written into `repo`, citing `path` as a
    `{path, scope: "repo"}` input -- the artifact-anchored shape when
    `item_id` is given (T1 1/2/9/10), the item-8 disclosure shape when not."""
    envelope = {
        **GENERIC_PASS_ENVELOPE,
        "provenance": {
            "klt_version": "0.5.0",
            "klayout_version": "0.30.10",
            "pdk": None,
            "deck": None,
            "input": {
                "content_hash": content_hash,
                "path": {"path": path, "scope": "repo"},
            },
        },
    }
    if item_id is not None:
        envelope["t1_item"] = item_id
        envelope["summary"] = f"audited artifact for T1 item {item_id}"
        envelope["source"] = path
    return _write(repo, f"item{item_id or 8}.json", envelope), content_hash


def test_gitignored_artifact_renders_identically_present_and_absent(tmp_path):
    """The issue's own reproduction, as the parity contract: the same
    manifest, same commit, same `--repo-root` -- graded once with the
    gitignored scratch artifact present on the producing host and once with
    it deleted -- renders a byte-identical report. Before #5857 the two
    renders differed in `input_verified` (`true` vs `null`)."""
    repo = _git_checkout(tmp_path)
    scratch = repo / "scratch" / "drc" / "cellA"
    scratch.mkdir(parents=True)
    artifact = scratch / "cellA.gds"
    artifact.write_bytes(b"GDS-A")
    envelope_path, recorded = _repo_scoped_generic(
        repo, path="scratch/drc/cellA/cellA.gds", content_hash=_hash_of(artifact)
    )
    manifest = _manifest(
        evidence={"8": {"file": envelope_path, "content_hash": recorded}}
    )

    present = build_tier_report(manifest)
    # Not `true` on the producing host: an untracked input is never read.
    assert _item(present, 8)["citation"]["input_verified"] is None

    artifact.unlink()
    absent = build_tier_report(manifest)

    assert json.dumps(absent, sort_keys=True) == json.dumps(present, sort_keys=True)


@pytest.mark.parametrize("item_id", [1, 2, 9, 10])
def test_gitignored_artifact_anchored_item_is_unmet_on_the_producing_host(
    tmp_path, item_id
):
    """The blast radius the curation measured: for the artifact-anchored T1
    items an untracked input is not a disclosure diff but a verdict flip --
    `met` on the producing host, `unmet`/`unverifiable_provenance` in a
    fresh clone. After #5857 the producing host renders the fresh clone's
    verdict, deterministically."""
    repo = _git_checkout(tmp_path)
    scratch = repo / "scratch"
    scratch.mkdir()
    artifact_name, content = _ANCHORED_ARTIFACTS[item_id]
    artifact = scratch / artifact_name
    artifact.write_text(content)
    envelope_path, digest = _repo_scoped_generic(
        repo,
        path=f"scratch/{artifact_name}",
        content_hash=_hash_of(artifact),
        item_id=item_id,
    )
    # The envelope is committed evidence; the artifact it cites is ignored
    # scratch -- exactly the shape that made the render host-dependent.
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "evidence")

    producing_host = build_tier_report(
        _manifest(
            evidence={str(item_id): {"file": envelope_path, "content_hash": digest}}
        )
    )

    item = _item(producing_host, item_id)
    assert item["status"] == "unmet"
    assert item["reason"] == "unverifiable_provenance"

    # The fresh clone -- same commit, no scratch -- rendered identically.
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(repo), str(clone)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    clone_envelope = clone / f"item{item_id}.json"
    assert not (clone / "scratch").exists()  # untracked bytes never clone
    fresh_clone = build_tier_report(
        _manifest(
            evidence={
                str(item_id): {"file": str(clone_envelope), "content_hash": digest}
            }
        )
    )
    fresh_item = _item(fresh_clone, item_id)
    assert fresh_item["status"] == "unmet"
    assert fresh_item["reason"] == "unverifiable_provenance"
    assert fresh_item["citation"] is None


def test_tracked_artifact_committed_beside_its_evidence_still_verifies(tmp_path):
    """The portable case #2659 bought must not regress: an artifact that is
    committed beside its evidence reports `input_verified: true` -- the
    filter drops *untracked* candidates, not the repo itself."""
    repo = _git_checkout(tmp_path)
    artifact = repo / "ota.gds"
    artifact.write_text("GDS-BYTES")
    envelope_path, digest = _repo_scoped_generic(
        repo, path="ota.gds", content_hash=_hash_of(artifact), item_id=2
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "evidence")

    result = build_tier_report(
        _manifest(evidence={"2": {"file": envelope_path, "content_hash": digest}})
    )

    item = _item(result, 2)
    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is True


def test_tracked_artifact_with_drifted_bytes_is_false_not_null(tmp_path):
    """Tracked and re-hashed with different bytes: `false` (stale), not
    `null` -- the filter must not swallow a genuine drift report. Driven
    through the item-8 disclosure (a drifted *anchored* citation refuses
    with `stale_evidence` instead, per #2718)."""
    repo = _git_checkout(tmp_path)
    artifact = repo / "2026-q3-report.md"
    artifact.write_text("Q3 characterization sweep: all spec rows within limits")
    recorded = _hash_of(artifact)
    envelope = {
        **GENERIC_PASS_ENVELOPE_WITH_PROVENANCE,
        "provenance": {
            **GENERIC_PASS_ENVELOPE_WITH_PROVENANCE["provenance"],
            "input": {
                "content_hash": recorded,
                "path": {"path": "2026-q3-report.md", "scope": "repo"},
            },
        },
    }
    envelope_path = _write(repo, "characterization.json", envelope)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "evidence")
    artifact.write_text("regenerated -- every spec row re-derived from scratch")

    result = build_tier_report(
        _manifest(evidence={"8": {"file": envelope_path, "content_hash": recorded}})
    )

    item = _item(result, 8)
    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is False


def test_tracked_but_deleted_from_the_worktree_is_null(tmp_path):
    """In the index, absent on disk: the candidate survives the filter (it
    is tracked) but nothing is readable, so `null` -- not a crash, not a
    false pass."""
    repo = _git_checkout(tmp_path)
    artifact = repo / "ota.gds"
    artifact.write_text("GDS-BYTES")
    envelope_path, digest = _repo_scoped_generic(
        repo, path="ota.gds", content_hash=_hash_of(artifact), item_id=2
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "evidence")
    artifact.unlink()

    result = build_tier_report(
        _manifest(evidence={"2": {"file": envelope_path, "content_hash": digest}})
    )

    item = _item(result, 2)
    assert item["status"] == "unmet"
    assert item["reason"] == "unverifiable_provenance"
    assert item["citation"] is None


def test_gitless_copy_with_repo_root_keeps_filesystem_presence(tmp_path):
    """The #2878 carve-out: over a `.git`-less copy graded with
    `--repo-root`, the tracked filter does not apply -- trackedness cannot
    be asked of a tree with no index -- so the same bytes that the checkout
    refuses (untracked there) still verify by presence over the copy."""
    repo = _git_checkout(tmp_path)
    scratch = repo / "scratch"
    scratch.mkdir()
    artifact = scratch / "cellA.gds"
    artifact.write_bytes(b"GDS-A")
    envelope_path, recorded = _repo_scoped_generic(
        repo, path="scratch/cellA.gds", content_hash=_hash_of(artifact), item_id=1
    )

    in_checkout = build_tier_report(
        _manifest(evidence={"1": {"file": envelope_path, "content_hash": recorded}})
    )
    assert _item(in_checkout, 1)["reason"] == "unverifiable_provenance"

    copy = tmp_path / "archive-copy"
    shutil.copytree(repo, copy)
    shutil.rmtree(copy / ".git")
    over_copy = build_tier_report(
        _manifest(
            evidence={"1": {"file": str(copy / "item1.json"), "content_hash": recorded}}
        ),
        repo_root=str(copy),
    )

    item = _item(over_copy, 1)
    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is True


def test_absolute_path_into_another_checkouts_scratch_is_null(tmp_path):
    """A pre-#2659 absolute echo pointing at gitignored scratch of a
    *different* real checkout: dropped by that checkout's own index, so
    `null` here -- not `true` by mere presence."""
    other = _git_checkout(tmp_path, name="other")
    scratch = other / "scratch"
    scratch.mkdir()
    artifact = scratch / "block.gds"
    artifact.write_bytes(b"GDS-OTHER")
    envelope = {
        **DRC_CLEAN_ENVELOPE,
        "file": str(artifact),
        "provenance": {
            **DRC_CLEAN_ENVELOPE["provenance"],
            "input": {"content_hash": _hash_of(artifact), "role": "layout"},
        },
    }
    drc_path = _write(tmp_path, "drc.json", envelope)

    result = build_tier_report(_manifest(evidence={"3": drc_path}))

    assert _item(result, 3)["citation"]["input_verified"] is None


def test_pex_schematic_inputs_gitignored_are_unverifiable_not_verified(tmp_path):
    """AC: `klt pex`'s schematic-side roles grade `unverifiable` -- not
    `verified` -- for an untracked path, through the same choke-point
    filter rather than a second code path."""
    repo = _git_checkout(tmp_path)
    scratch = repo / "scratch"
    scratch.mkdir()
    files = {}
    for name in ("req.json", "body.spice", "dut.spice"):
        files[name] = scratch / name
        files[name].write_text(f"original {name}\n")

    def ref(name):
        return {"path": f"scratch/{name}", "scope": "repo"}

    envelope = {
        **PEX_PASS_ENVELOPE,
        "reference_netlist": ref("dut.spice"),
        "reference_netlist_sha256": _sha_of(files["dut.spice"]),
        "testbenches": [
            {
                "request": ref("req.json"),
                "request_sha256": _sha_of(files["req.json"]),
                "netlist": ref("body.spice"),
                "netlist_sha256": _sha_of(files["body.spice"]),
                "schematic_netlist": ref("dut.spice"),
                "schematic_netlist_sha256": _sha_of(files["dut.spice"]),
            }
        ],
    }
    pex_path = _write(repo, "pex.json", envelope)

    citation = _item7_citation(pex_path)

    assert {entry["status"] for entry in citation["pex_inputs"]} == {"unverifiable"}
    assert citation["input_verified"] is None


def test_gitignored_member_of_an_input_set_is_unverifiable_deterministically(
    tmp_path,
):
    """The multi-member path (#3044) resolves through the same choke point
    (the Champion's fourth surface): a set with one tracked and one
    gitignored member names the gitignored one `unverifiable` in
    `detail.artifact_binding.members` -- identically with the scratch file
    present and deleted."""
    repo = _git_checkout(tmp_path)
    tracked = repo / "testbenches.txt"
    tracked.write_text("tb/ota_ac.spice\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "evidence")
    scratch = repo / "scratch"
    scratch.mkdir()
    notes = scratch / "notes.txt"
    notes.write_text("audit notes\n")
    members = [
        {"path": "testbenches.txt", "content_hash": _hash_of(tracked)},
        {"path": "scratch/notes.txt", "content_hash": _hash_of(notes)},
    ]
    combined = input_set_digest(
        (member["path"], member["content_hash"]) for member in members
    )
    envelope = {
        **GENERIC_PASS_ENVELOPE,
        "t1_item": 9,
        "summary": "audited artifact for T1 item 9",
        "source": "testbenches.txt",
        "provenance": {
            "klt_version": "0.5.0",
            "klayout_version": "0.30.10",
            "pdk": None,
            "deck": None,
            "input": {"content_hash": combined, "paths": members},
        },
    }
    envelope_path = _write(repo, "item9.json", envelope)

    present = build_tier_report(
        _manifest(evidence={"9": {"file": envelope_path, "content_hash": combined}})
    )
    item = _item(present, 9)
    assert item["status"] == "unmet"
    assert item["reason"] == "unverifiable_provenance"
    statuses = {
        member["path"]: member["status"]
        for member in item["detail"]["artifact_binding"]["members"]
    }
    assert statuses == {
        "testbenches.txt": "verified",
        "scratch/notes.txt": "unverifiable",
    }

    notes.unlink()
    absent = build_tier_report(
        _manifest(evidence={"9": {"file": envelope_path, "content_hash": combined}})
    )
    assert json.dumps(_item(absent, 9), sort_keys=True) == json.dumps(
        item, sort_keys=True
    )


def test_all_tracked_input_set_still_grades_met(tmp_path):
    """The control for the set case above: every member tracked and
    committed, the combined digest consistent -- the item stays `met`,
    so the filter costs no true positives."""
    repo = _git_checkout(tmp_path)
    first = repo / "testbenches.txt"
    first.write_text("tb/ota_ac.spice\n")
    second = repo / "ci.yml"
    second.write_text("jobs: {validate: {}}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "evidence")
    members = [
        {"path": "testbenches.txt", "content_hash": _hash_of(first)},
        {"path": "ci.yml", "content_hash": _hash_of(second)},
    ]
    combined = input_set_digest(
        (member["path"], member["content_hash"]) for member in members
    )
    envelope = {
        **GENERIC_PASS_ENVELOPE,
        "t1_item": 9,
        "summary": "audited artifact for T1 item 9",
        "source": "testbenches.txt",
        "provenance": {
            "klt_version": "0.5.0",
            "klayout_version": "0.30.10",
            "pdk": None,
            "deck": None,
            "input": {"content_hash": combined, "paths": members},
        },
    }
    envelope_path = _write(repo, "item9.json", envelope)

    result = build_tier_report(
        _manifest(evidence={"9": {"file": envelope_path, "content_hash": combined}})
    )

    item = _item(result, 9)
    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is True


def test_artifact_inside_a_submodule_verifies_by_the_submodules_index(tmp_path):
    """A submodule's files are tracked by the submodule's own index, not
    the superproject's (which records only the gitlink) -- the filter asks
    the checkout the file actually lives in, so this stays `true`."""
    inner = _git_checkout(tmp_path, name="inner")
    dut = inner / "dut.spice"
    dut.write_text(".SUBCKT DUT\n.ENDS\n")
    _git(inner, "add", "-A")
    _git(inner, "commit", "-qm", "artifact")
    outer = _git_checkout(tmp_path, name="outer")
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "-C",
            str(outer),
            "submodule",
            "add",
            "-q",
            str(inner),
            "sub",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    _git(outer, "commit", "-qm", "submodule")

    envelope_path, digest = _repo_scoped_generic(
        outer, path="sub/dut.spice", content_hash=_hash_of(dut), item_id=1
    )

    result = build_tier_report(
        _manifest(evidence={"1": {"file": envelope_path, "content_hash": digest}})
    )

    item = _item(result, 1)
    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is True
