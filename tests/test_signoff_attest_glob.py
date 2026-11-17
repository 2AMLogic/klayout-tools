"""``klt signoff --attest``: ``{"glob": ...}`` inventory members (issue
#5614).

A ``sources`` (item 1) or ``hygiene`` (item 10) entry may be a
``{"glob": "<pattern>"}`` object, expanded at attest time to the git-tracked
files matching it. The core property under test is the one that was silent
before: a file **added** to a covered directory after the envelope was
committed is drift, not a clean pass. In its own file so
``tests/test_signoff.py`` stays under its line ratchet (it follows
``test_signoff_attest_item9_sources``'s precedent).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools import signoff_attest_glob as glob_module
from klayout_tools.cli import main
from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
    check_attestation,
    render_envelope,
)

_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]


# --------------------------------------------------------------------------- #
# Fixtures: a real git repository (expansion reads the index, so a faked
# `.git` entry is not enough) with a directory of markdown records
# --------------------------------------------------------------------------- #


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _tracked_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    for subdir in ("records", "attest", "evidence"):
        (repo / subdir).mkdir(parents=True)
    for name in ("a", "b"):
        (repo / "records" / f"{name}.md").write_text(f"# {name}\n")
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "seed", cwd=repo)
    return repo


def _inv(repo: Path, item: int = 10, schema_version: int = 2, **fields) -> str:
    inventory: dict = {
        "schema_version": schema_version,
        "kind": "t1-attestation-inventory",
        "t1_item": item,
        **fields,
    }
    if item == 10:
        inventory.setdefault("checks", [{"name": "ok", "argv": _OK}])
    path = repo / "attest" / f"item{item}.json"
    path.write_text(json.dumps(inventory))
    return str(path)


def _hash_of(path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _present(envelope: dict) -> list[dict]:
    return [c for c in envelope["attestation"]["checks"] if c["check"] == "present"]


# --------------------------------------------------------------------------- #
# Expansion: the tracked matches become the members
# --------------------------------------------------------------------------- #


def test_a_hygiene_glob_expands_to_the_tracked_matches(tmp_path):
    repo = _tracked_repo(tmp_path)
    inventory = _inv(repo, hygiene=[{"glob": "records/*.md"}])

    envelope = build_attestation(10, inventory)

    assert envelope["status"] == "pass"
    assert envelope["attestation"]["inventory_schema_version"] == 2
    present = [(c["role"], c["path"], c["content_hash"]) for c in _present(envelope)]
    assert present == [
        ("hygiene", "records/a.md", _hash_of(repo / "records/a.md")),
        ("hygiene", "records/b.md", _hash_of(repo / "records/b.md")),
    ]


def test_a_literal_pattern_behaves_as_a_one_file_glob(tmp_path):
    """A pattern with no metacharacters is one exact match -- still a glob,
    so still refused when that one file is untracked (zero matches)."""
    repo = _tracked_repo(tmp_path)
    inventory = _inv(repo, hygiene=[{"glob": "records/a.md"}])

    envelope = build_attestation(10, inventory)

    assert [c["path"] for c in _present(envelope)] == ["records/a.md"]


def test_an_untracked_matching_file_is_never_a_member(tmp_path):
    """Determinism: expansion reads the git index, not the filesystem, so a
    developer's untracked local file changes nothing and the envelope stays
    byte-identical to the run before it existed."""
    repo = _tracked_repo(tmp_path)
    inventory = _inv(repo, hygiene=[{"glob": "records/*.md"}])
    before = render_envelope(build_attestation(10, inventory))

    (repo / "records" / "local-only.md").write_text("# untracked\n")
    after = render_envelope(build_attestation(10, inventory))

    assert before == after


def test_a_directory_is_never_a_member(tmp_path):
    """``git ls-files`` lists files, not directories: a pattern naming a
    directory matches nothing and gets the zero-match refusal, instead of
    binding a path that is not a file."""
    repo = _tracked_repo(tmp_path)
    (repo / "records" / "nested").mkdir()
    (repo / "records" / "nested" / "d.md").write_text("# d\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "nested", cwd=repo)
    inventory = _inv(repo, hygiene=[{"glob": "records/nested"}])

    with pytest.raises(AttestationError, match="matched no git-tracked file"):
        build_attestation(10, inventory)


def test_a_symlink_under_a_globbed_directory_is_refused(tmp_path):
    """An expanded member walks the same validation a literal entry does:
    a symlink is refused, naming the path."""
    repo = _tracked_repo(tmp_path)
    (repo / "records" / "link.md").symlink_to("a.md")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "link", cwd=repo)
    inventory = _inv(repo, hygiene=[{"glob": "records/*.md"}])

    with pytest.raises(AttestationError, match="records/link.md.*symlink"):
        build_attestation(10, inventory)


# --------------------------------------------------------------------------- #
# The regression this issue is about: an added file is drift, not a clean pass
# --------------------------------------------------------------------------- #


def test_an_added_matching_file_is_drift(tmp_path):
    repo = _tracked_repo(tmp_path)
    inventory = _inv(repo, hygiene=[{"glob": "records/*.md"}])
    committed = repo / "evidence" / "item10.json"
    committed.write_text(render_envelope(build_attestation(10, inventory)))

    (repo / "records" / "late.md").write_text("# added after the attest\n")
    _git("add", "records/late.md", cwd=repo)

    result = check_attestation(10, inventory, str(committed))
    assert result["status"] == "drifted"
    assert "records/late.md" in json.dumps(result["drift"])
    exit_code = main(
        [
            "signoff",
            "--attest",
            "10",
            "--inventory",
            inventory,
            "--check",
            str(committed),
            "--format",
            "json",
        ]
    )
    assert exit_code != 0


# --------------------------------------------------------------------------- #
# Refusals: the inventory stays unambiguous
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        (42, "must be non-empty strings"),
        ("", "must be non-empty strings"),
        ("/records/*.md", "is absolute"),
        ("\\records\\*.md", "is absolute"),
        ("records/../x/*.md", "outside the repository"),
    ],
)
def test_a_bad_pattern_is_refused(tmp_path, pattern, message):
    repo = _tracked_repo(tmp_path)
    inventory = _inv(repo, hygiene=[{"glob": pattern}])

    with pytest.raises(AttestationError, match=message):
        build_attestation(10, inventory)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"path": "records/a.md"}, "exactly a 'glob' key"),
        ({}, "exactly a 'glob' key"),
        ({"glob": "records/*.md", "exclude": "x"}, "exactly a 'glob' key"),
    ],
)
def test_a_malformed_glob_entry_is_refused(tmp_path, entry, message):
    repo = _tracked_repo(tmp_path)
    inventory = _inv(repo, hygiene=[entry])

    with pytest.raises(AttestationError, match=message):
        build_attestation(10, inventory)


def test_a_glob_in_a_schema_1_inventory_is_refused(tmp_path):
    """A bare string must keep meaning "literal path" forever, so the glob
    form is versioned: schema 1 refuses it and points at the version."""
    repo = _tracked_repo(tmp_path)
    inventory = _inv(repo, schema_version=1, hygiene=[{"glob": "records/*.md"}])

    with pytest.raises(AttestationError, match="schema_version 2"):
        build_attestation(10, inventory)


def test_a_zero_match_glob_is_refused(tmp_path):
    """A glob that silently attests nothing reintroduces the coverage gap in
    a new shape -- same rule as an empty path list."""
    repo = _tracked_repo(tmp_path)
    inventory = _inv(repo, hygiene=[{"glob": "records/*.nonexistent"}])

    with pytest.raises(AttestationError, match="matched no git-tracked file"):
        build_attestation(10, inventory)


def test_a_glob_colliding_with_a_literal_entry_is_a_duplicate(tmp_path):
    repo = _tracked_repo(tmp_path)
    inventory = _inv(repo, hygiene=["records/a.md", {"glob": "records/*.md"}])

    with pytest.raises(AttestationError, match="records/a.md.*more than once"):
        build_attestation(10, inventory)


def test_git_being_unusable_is_an_error_not_a_filesystem_fallback(tmp_path):
    """A `.git` entry that is not a working repository: `git ls-files`
    fails and the glob is refused -- expansion never falls back to walking
    the filesystem, which would break byte-determinism."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "records").mkdir()
    (repo / "records" / "a.md").write_text("# a\n")
    (repo / "attest").mkdir()
    inventory = _inv(repo, hygiene=[{"glob": "records/*.md"}])

    with pytest.raises(AttestationError, match="git-tracked"):
        build_attestation(10, inventory)


def test_git_disappearing_mid_run_is_an_error_not_a_fallback(tmp_path, monkeypatch):
    repo = _tracked_repo(tmp_path)
    inventory = _inv(repo, hygiene=[{"glob": "records/*.md"}])

    def boom(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(glob_module.subprocess, "run", boom)

    with pytest.raises(AttestationError, match="git-tracked"):
        build_attestation(10, inventory)


# --------------------------------------------------------------------------- #
# Item 1: expanded sources reach the input-set binding
# --------------------------------------------------------------------------- #


def test_an_item_1_sources_glob_reaches_the_input_set(tmp_path):
    """Expanded members participate in `provenance.input.paths` and the
    combined set digest, so adding a matching source changes the manifest
    pin -- the existing "Pin change" behaviour, extended to globs."""
    repo = _tracked_repo(tmp_path)
    (repo / "design").mkdir()
    (repo / "design" / "ota.sch").write_text("v {xschem}\n")
    (repo / "design" / "bias.sch").write_text("v {xschem bias}\n")
    (repo / "netlist").mkdir()
    (repo / "netlist" / "ota.spice").write_text("* ota\n.end\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "design", cwd=repo)
    inventory = _inv(
        repo,
        item=1,
        sources=[{"glob": "design/*.sch"}],
        derived=[
            {
                "path": "netlist/ota.spice",
                "generator": "klt netlist",
                "check": {"argv": _OK},
            }
        ],
    )

    envelope = build_attestation(1, inventory)

    assert envelope["status"] == "pass"
    members = envelope["provenance"]["input"]["paths"]
    assert {m["path"]["path"] for m in members} >= {
        "design/bias.sch",
        "design/ota.sch",
        "netlist/ota.spice",
    }
    by_path = {m["path"]["path"]: m["content_hash"] for m in members}
    assert by_path["design/ota.sch"] == _hash_of(repo / "design/ota.sch")
