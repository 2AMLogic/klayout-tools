"""Grade item 1 against a pinned upstream object database (#7919)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools._provenance import sha256_file
from klayout_tools.cli import main
from klayout_tools.signoff_attest import AttestationError, build_attestation
from test_signoff import _attest_cli, _attest_repo, _item, _manifest, _write_inventory


def _git(repo, *args):
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.org",
            *args,
        ],
        text=True,
    ).strip()


@pytest.fixture
def trees(tmp_path, request):
    repo = _attest_repo(tmp_path)
    _git(repo, "init", "-q")
    upstream = tmp_path / "vendor"
    upstream.mkdir()
    object_format = getattr(request, "param", "sha1")
    _git(upstream, "init", "-q", f"--object-format={object_format}")
    name = "design/ota.sch"
    (upstream / "design").mkdir()
    (upstream / name).write_bytes(b"a\x00\xff\r\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-qm", "upstream input")
    source = {
        "repo": "../vendor" if object_format == "sha1" else str(upstream),
        "commit": _git(upstream, "rev-parse", "HEAD"),
        "path": name,
        "sha256": sha256_file(str(upstream / name)),
    }
    derived = "netlist/ota.spice"
    inventory = Path(
        _write_inventory(
            repo,
            1,
            schema_version=2,
            sources=[source],
            derived=[
                {
                    "path": derived,
                    "generator": "test generator",
                    "recorded": {
                        "derived_sha256": sha256_file(str(repo / derived)),
                        "sources": [source],
                    },
                }
            ],
        )
    )
    _git(repo, "add", str(inventory), derived)
    _git(repo, "commit", "-qm", "consumer inventory and output")
    return repo, upstream, source, inventory


def _mint(repo, inventory, capsys):
    output = repo / "evidence" / "item1.json"
    assert _attest_cli(1, str(inventory), "-o", str(output), "--format", "json") == 0
    entry = json.loads(capsys.readouterr().out)["manifest_entry"]
    manifest = repo / "manifest.json"
    manifest.write_text(json.dumps(_manifest(evidence={"1": entry})))
    return output, manifest


def _grade(manifest, capsys):
    assert main(["signoff", "--manifest", str(manifest), "--format", "json"]) == 3
    return _item(json.loads(capsys.readouterr().out), 1)


@pytest.mark.parametrize("trees", ["sha1", "sha256"], indirect=True)
def test_checkout_then_bare_verifies_same_citation(trees, capsys):
    repo, upstream, source, inventory = trees
    _, manifest = _mint(repo, inventory, capsys)
    _git(upstream, "commit", "--allow-empty", "-qm", "new HEAD")
    (upstream / source["path"]).write_bytes(b"dirty")
    for bare in (False, True):
        if bare:
            upstream.rename(repo.parent / "original")
            _git(repo, "clone", "--bare", "-q", "../original", str(upstream))
            shutil.rmtree(repo.parent / "original")
        graded = _grade(manifest, capsys)
        assert graded["status"] == "met"
        members = graded["citation"]["artifact_binding"]["members"]
        assert len(members) == 3
        assert {m["status"] for m in members} == {"verified"}
        assert {
            "path": {k: v for k, v in source.items() if k != "sha256"}
            | {"scope": "git"},
            "content_hash": "sha256:" + source["sha256"],
            "status": "verified",
        } in members


@pytest.mark.parametrize("substitution", ["repo", "commit"])
def test_substituted_git_identity_invalidates_original_pin(trees, capsys, substitution):
    repo, upstream, source, inventory = trees
    output, manifest = _mint(repo, inventory, capsys)
    _git(upstream, "commit", "--allow-empty", "-qm", "same blob")
    _git(repo, "clone", "--bare", "-q", str(upstream), "../copy.git")
    alternative = {"repo": "../copy.git", "commit": _git(upstream, "rev-parse", "HEAD")}
    envelope = json.loads(output.read_text())
    members = envelope["provenance"]["input"]["paths"]
    location = next(m["path"] for m in members if m["path"]["scope"] == "git")
    location[substitution] = alternative[substitution]
    output.write_text(json.dumps(envelope))
    assert _grade(manifest, capsys)["reason"] == "stale_evidence"


@pytest.mark.parametrize("missing", ["repo", "commit", "blob"])
def test_missing_objects_never_fall_back_to_matching_file(trees, capsys, missing):
    repo, upstream, source, inventory = trees
    _, manifest = _mint(repo, inventory, capsys)
    (repo / source["path"]).write_bytes((upstream / source["path"]).read_bytes())
    _git(repo, "add", source["path"])
    if missing == "repo":
        upstream.rename(repo.parent / "unavailable")
    else:
        oid = (
            source["commit"]
            if missing == "commit"
            else _git(upstream, "rev-parse", "HEAD:" + source["path"])
        )
        (upstream / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    graded = _grade(manifest, capsys)
    assert graded["reason"] == "unverifiable_provenance"
    bad = [
        m
        for m in graded["detail"]["artifact_binding"]["members"]
        if m["status"] != "verified"
    ]
    assert len(bad) == 1
    assert bad[0]["path"]["repo"] == source["repo"]
    assert bad[0]["status"] == "unverifiable"


@pytest.mark.parametrize("revision", ["HEAD", "v1", "abc1234"])
def test_rejects_symbolic_and_abbreviated_revisions(trees, revision):
    _, _, _, inventory = trees
    data = json.loads(inventory.read_text())
    data["sources"][0]["commit"] = revision
    inventory.write_text(json.dumps(data))
    with pytest.raises(AttestationError, match="full.*commit"):
        build_attestation(1, str(inventory))


def test_wrong_expected_digest_at_mint_and_grade(trees, capsys):
    repo, _, source, inventory = trees
    data = json.loads(inventory.read_text())
    for declared in (data["sources"][0], data["derived"][0]["recorded"]["sources"][0]):
        declared["sha256"] = "0" * 64
    inventory.write_text(json.dumps(data))
    output = repo / "evidence" / "item1.json"
    assert _attest_cli(1, str(inventory), "-o", str(output), "--format", "text") == 3
    assert not output.exists()
    assert source["commit"] in capsys.readouterr().out
    envelope = build_attestation(1, str(inventory))
    assert envelope["status"] == "fail"
    present, _, freshness = envelope["attestation"]["checks"]
    assert present["actual_content_hash"] == "sha256:" + source["sha256"]
    assert freshness["mismatches"] == [
        {"path": present["path"], "expected": "0" * 64, "actual": source["sha256"]}
    ]
    # Even a passing author assertion with a consistent set pin must rehash the blob.
    envelope["status"] = "pass"
    output.write_text(json.dumps(envelope))
    entry = {
        "file": str(output),
        "content_hash": envelope["provenance"]["input"]["content_hash"],
    }
    manifest = repo / "manifest.json"
    manifest.write_text(json.dumps(_manifest(evidence={"1": entry})))
    graded = _grade(manifest, capsys)
    assert graded["reason"] == "stale_evidence"
    members = graded["detail"]["artifact_binding"]["members"]
    assert [m["status"] for m in members if m["path"] == present["path"]] == ["stale"]


def test_same_path_in_three_repositories(trees, capsys):
    repo, upstream, source, inventory = trees
    _git(repo, "clone", "--bare", "-q", str(upstream), "../second.git")
    _git(repo, "add", source["path"])
    second = {**source, "repo": "../second.git"}
    data = json.loads(inventory.read_text())
    data["sources"].extend([source["path"], second])
    local = {"path": source["path"], "sha256": sha256_file(str(repo / source["path"]))}
    data["derived"][0]["recorded"]["sources"].extend([second, local])
    inventory.write_text(json.dumps(data))
    _, manifest = _mint(repo, inventory, capsys)
    graded = _grade(manifest, capsys)
    assert graded["status"] == "met"
    members = graded["citation"]["artifact_binding"]["members"]
    assert len([m for m in members if m["path"]["path"] == source["path"]]) == 3
    data["derived"][0]["recorded"]["sources"][0]["repo"] = "../other.git"
    inventory.write_text(json.dumps(data))
    with pytest.raises(AttestationError, match="not in"):
        build_attestation(1, str(inventory))


def test_present_check_cannot_credit_an_unbound_revision(trees, capsys):
    repo, upstream, source, inventory = trees
    output, manifest = _mint(repo, inventory, capsys)
    _git(upstream, "commit", "--allow-empty", "-qm", "same tree")
    envelope = json.loads(output.read_text())
    source_check = next(
        c for c in envelope["attestation"]["checks"] if c["role"] == "source"
    )
    source_check["path"]["commit"] = _git(upstream, "rev-parse", "HEAD")
    output.write_text(json.dumps(envelope))
    graded = _grade(manifest, capsys)
    assert graded["reason"] == "unverifiable_provenance"
    assert graded["detail"]["artifact_binding"]["members"] == [
        {
            "path": source_check["path"],
            "content_hash": "sha256:" + source["sha256"],
            "status": "unverifiable",
        }
    ]


def test_non_commit_object_id_cannot_pin_a_revision(trees):
    _, upstream, _, inventory = trees
    data = json.loads(inventory.read_text())
    tree = _git(upstream, "rev-parse", "HEAD^{tree}")
    for declared in (data["sources"][0], data["derived"][0]["recorded"]["sources"][0]):
        declared["commit"] = tree
    inventory.write_text(json.dumps(data))
    envelope = build_attestation(1, str(inventory))
    assert envelope["status"] == "fail"
    assert envelope["attestation"]["checks"][0]["actual_content_hash"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "../member.txt"),
        ("path", "/member.txt"),
        ("repo", ""),
        ("sha256", "ab"),
        ("unknown", 1),
    ],
)
def test_rejects_malformed_git_source(trees, field, value):
    _, _, _, inventory = trees
    data = json.loads(inventory.read_text())
    data["sources"][0][field] = value
    inventory.write_text(json.dumps(data))
    with pytest.raises(AttestationError, match="sources"):
        build_attestation(1, str(inventory))
