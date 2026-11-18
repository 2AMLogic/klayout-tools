"""Store-pinned `klt yield` samples companions (issue #5963): a file-backed
yield evidence entry whose large samples document lives in the artifact
store, not in git, pins it offline through a **committed** store manifest
(``artifact_manifest`` + optional ``artifact_member``) -- the grader reads
the manifest's recorded ``sha256`` as the live hash the ``content_hash``
staleness gate compares, never contacting a broker and never re-hashing
bytes it did not read. Per the operator ruling of 2026-11-17 (option ``a``):
the envelope itself stays committed and file-backed, the store-resolved
hash never supplies a verdict, and the citation discloses its basis."""

import hashlib
import json

from klayout_tools.signoff import build_tier_report
from test_signoff import YIELD_PASS_ENVELOPE, _manifest

REPO = "acme/widgets"

#: The store manifest's member name for the samples document: the store's
#: full member path, which shares only a basename with the ``samples``
#: reference the `klt yield` run recorded -- the usual shape, and the one
#: the default (no ``artifact_member``) member resolution must survive.
MEMBER_NAME = "bench/20261115-172039-abc/mc-samples.json"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _key(sha256: str) -> str:
    return f"evidence/{REPO.replace('/', '__')}/{sha256[:2]}/{sha256}"


def _entry(sha256: str, name: str, size: int = 4096) -> dict:
    return {
        "sha256": sha256,
        "size": size,
        "kind": "evidence",
        "producer": {
            "tool": "klt",
            "version": "0",
            "loom.repo": REPO,
            "commit": None,
        },
        "uri": f"artifact:{_key(sha256)}",
        "name": name,
    }


def _store_manifest(root, entries) -> str:
    """A committed ``*.manifest.json`` at its conventional repo-relative
    path -- a different directory than the evidence file, resolved like
    ``file`` itself (from the grading cwd, not the report's directory)."""
    path = root / "manifests" / "artifacts" / "run-1.manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "entries": entries}))
    return str(path.relative_to(root))


def _yield_report(root, *, samples: str = "mc-samples.json", samples_bytes=None):
    """A committed file-backed yield envelope under ``block/yield/``; the
    samples document it names is written beside it only when the test asks
    for one on disk."""
    evidence_dir = root / "block" / "yield"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if samples_bytes is not None:
        (evidence_dir / samples).write_bytes(samples_bytes)
    envelope = {**YIELD_PASS_ENVELOPE, "samples": samples}
    report = evidence_dir / "yield.json"
    report.write_text(json.dumps(envelope))
    return str(report.relative_to(root))


def _grade(root, entry: dict):
    return build_tier_report(_manifest(evidence={"6": entry}))


def _item_6(result: dict) -> dict:
    return next(item for item in result["items"] if item["id"] == 6)


def test_store_pin_resolves_absent_samples_hash_and_discloses_basis(
    tmp_path, monkeypatch
):
    """AC 1+2: the samples file is absent, the committed manifest records
    its sha256, and the entry pins that hash -- the pin grades ``verified``
    (``met``) with the manifest's recorded hash as the live hash, disclosed
    as ``content_hash_basis: "artifact_manifest"`` and ``input_verified:
    null`` (the bytes were not read, so no re-hash is claimed)."""
    monkeypatch.chdir(tmp_path)
    samples_sha = _sha("monte carlo samples bytes")
    manifest = _store_manifest(tmp_path, [_entry(samples_sha, MEMBER_NAME)])
    report = _yield_report(tmp_path)

    result = _grade(
        tmp_path,
        {
            "file": report,
            "content_hash": f"sha256:{samples_sha}",
            "artifact_manifest": manifest,
        },
    )

    item_6 = _item_6(result)
    assert item_6["status"] == "met"
    citation = item_6["citation"]
    assert citation["content_hash"] == f"sha256:{samples_sha}"
    assert citation["content_hash_basis"] == "artifact_manifest"
    assert citation["input_verified"] is None
    assert "content_hash_unresolved" not in citation


def test_store_pin_stale_pin_renders_stale_evidence(tmp_path, monkeypatch):
    """AC 1: same resolution, a pin naming a different revision -- the same
    ``stale_evidence`` refusal a stale disk-hash pin always rendered."""
    monkeypatch.chdir(tmp_path)
    manifest = _store_manifest(
        tmp_path, [_entry(_sha("the new samples bytes"), MEMBER_NAME)]
    )
    report = _yield_report(tmp_path)

    result = _grade(
        tmp_path,
        {
            "file": report,
            "content_hash": "sha256:" + _sha("the old samples bytes"),
            "artifact_manifest": manifest,
        },
    )

    item_6 = _item_6(result)
    assert item_6["status"] == "unmet"
    assert item_6["reason"] == "stale_evidence"
    assert item_6["citation"] is None


def test_disk_samples_hash_wins_over_the_manifest(tmp_path, monkeypatch):
    """AC 3: with the samples file present on disk, behavior and output are
    byte-identical to today -- the disk re-hash wins, the manifest is
    ignored (here it records a *wrong* sha), the basis key is absent and
    ``input_verified`` stays ``true`` by construction."""
    monkeypatch.chdir(tmp_path)
    samples_bytes = json.dumps({"measurements": []}).encode()
    disk_sha = hashlib.sha256(samples_bytes).hexdigest()
    manifest = _store_manifest(
        tmp_path, [_entry(_sha("not what is on disk"), MEMBER_NAME)]
    )
    report = _yield_report(tmp_path, samples_bytes=samples_bytes)

    result = _grade(
        tmp_path,
        {
            "file": report,
            "content_hash": f"sha256:{disk_sha}",
            "artifact_manifest": manifest,
        },
    )

    item_6 = _item_6(result)
    assert item_6["status"] == "met"
    citation = item_6["citation"]
    assert citation["content_hash"] == f"sha256:{disk_sha}"
    assert "content_hash_basis" not in citation
    assert citation["input_verified"] is True
    assert "content_hash_unresolved" not in citation


def test_unreadable_manifest_names_reason_never_raises(tmp_path, monkeypatch):
    """AC 4: a declared manifest that does not exist leaves the hash
    unresolved with a named reason -- on the citation of an unpinned entry,
    and in the ``detail`` of a pinned one (not ``met`` on freshness
    grounds), never an exception."""
    monkeypatch.chdir(tmp_path)
    report = _yield_report(tmp_path)

    unpinned = _grade(
        tmp_path, {"file": report, "artifact_manifest": "missing.manifest.json"}
    )
    citation = _item_6(unpinned)["citation"]
    assert citation["content_hash"] is None
    assert citation["content_hash_unresolved"] == {
        "samples": "mc-samples.json",
        "searched": ["block/yield/mc-samples.json", "mc-samples.json"],
        "artifact_manifest": "missing.manifest.json",
        "reason": "manifest_unreadable",
    }

    pinned = _grade(
        tmp_path,
        {
            "file": report,
            "content_hash": "sha256:" + _sha("anything"),
            "artifact_manifest": "missing.manifest.json",
        },
    )
    item_6 = _item_6(pinned)
    assert item_6["status"] == "unmet"
    assert item_6["reason"] == "unverifiable_provenance"
    assert item_6["citation"] is None
    assert item_6["detail"]["content_hash_unresolved"]["reason"] == (
        "manifest_unreadable"
    )


def test_invalid_manifest_names_reason(tmp_path, monkeypatch):
    """AC 4: a committed file that is not a valid manifest (wrong schema)
    is ``manifest_invalid``, same refusal shape as an unreadable one."""
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.manifest.json"
    bad.write_text(json.dumps({"schema_version": 1, "objects": []}))
    report = _yield_report(tmp_path)

    result = _grade(
        tmp_path,
        {
            "file": report,
            "content_hash": "sha256:" + _sha("anything"),
            "artifact_manifest": "bad.manifest.json",
        },
    )

    item_6 = _item_6(result)
    assert item_6["status"] == "unmet"
    assert item_6["reason"] == "unverifiable_provenance"
    assert item_6["detail"]["content_hash_unresolved"]["reason"] == "manifest_invalid"


def test_missing_member_names_reason(tmp_path, monkeypatch):
    """AC 4: a valid manifest naming no such member is ``member_missing``;
    the default member resolution (envelope ``samples`` basename) finding
    nothing is the same refusal."""
    monkeypatch.chdir(tmp_path)
    manifest = _store_manifest(
        tmp_path, [_entry(_sha("unrelated bytes"), "bench/other/design.gds")]
    )
    report = _yield_report(tmp_path)

    result = _grade(
        tmp_path,
        {
            "file": report,
            "content_hash": "sha256:" + _sha("anything"),
            "artifact_manifest": manifest,
        },
    )

    item_6 = _item_6(result)
    assert item_6["status"] == "unmet"
    assert item_6["reason"] == "unverifiable_provenance"
    assert item_6["detail"]["content_hash_unresolved"]["reason"] == "member_missing"


def test_duplicate_member_name_names_reason(tmp_path, monkeypatch):
    """AC 4: two entries sharing the pinned member's name -- whether the
    default resolution or an explicit ``artifact_member`` found them -- is
    ``duplicate_member``, never a guess between them."""
    monkeypatch.chdir(tmp_path)
    manifest = _store_manifest(
        tmp_path,
        [
            _entry(_sha("first copy"), MEMBER_NAME),
            _entry(_sha("second copy"), MEMBER_NAME),
        ],
    )
    report = _yield_report(tmp_path)

    result = _grade(
        tmp_path,
        {
            "file": report,
            "content_hash": "sha256:" + _sha("anything"),
            "artifact_manifest": manifest,
            "artifact_member": MEMBER_NAME,
        },
    )

    item_6 = _item_6(result)
    assert item_6["status"] == "unmet"
    assert item_6["detail"]["content_hash_unresolved"]["reason"] == "duplicate_member"


def test_default_member_matches_exact_samples_path(tmp_path, monkeypatch):
    """The default member resolution's first leg: the entry name equal to
    the envelope's own ``samples`` reference matches exactly, no
    ``artifact_member`` needed."""
    monkeypatch.chdir(tmp_path)
    samples_sha = _sha("samples bytes")
    manifest = _store_manifest(
        tmp_path,
        [_entry(_sha("other"), "bench/other/drc.json")]
        + [_entry(samples_sha, "mc-samples.json")],
    )
    report = _yield_report(tmp_path)

    result = _grade(
        tmp_path,
        {
            "file": report,
            "content_hash": f"sha256:{samples_sha}",
            "artifact_manifest": manifest,
        },
    )

    assert _item_6(result)["status"] == "met"


def test_ambiguous_basename_names_reason(tmp_path, monkeypatch):
    """The default member resolution refusing to guess: two entries with
    distinct names sharing the samples basename are ``ambiguous_member``."""
    monkeypatch.chdir(tmp_path)
    manifest = _store_manifest(
        tmp_path,
        [
            _entry(_sha("run one"), "bench/run-1/mc-samples.json"),
            _entry(_sha("run two"), "bench/run-2/mc-samples.json"),
        ],
    )
    report = _yield_report(tmp_path)

    result = _grade(
        tmp_path,
        {
            "file": report,
            "content_hash": "sha256:" + _sha("anything"),
            "artifact_manifest": manifest,
        },
    )

    item_6 = _item_6(result)
    assert item_6["status"] == "unmet"
    assert item_6["detail"]["content_hash_unresolved"]["reason"] == "ambiguous_member"


def test_explicit_member_disambiguates_the_basename(tmp_path, monkeypatch):
    """``artifact_member`` selects one of several same-basename entries:
    the store's full member path is the unambiguous spelling."""
    monkeypatch.chdir(tmp_path)
    samples_sha = _sha("run two samples")
    manifest = _store_manifest(
        tmp_path,
        [
            _entry(_sha("run one"), "bench/run-1/mc-samples.json"),
            _entry(samples_sha, "bench/run-2/mc-samples.json"),
        ],
    )
    report = _yield_report(tmp_path)

    result = _grade(
        tmp_path,
        {
            "file": report,
            "content_hash": f"sha256:{samples_sha}",
            "artifact_manifest": manifest,
            "artifact_member": "bench/run-2/mc-samples.json",
        },
    )

    item_6 = _item_6(result)
    assert item_6["status"] == "met"
    assert item_6["citation"]["content_hash"] == f"sha256:{samples_sha}"


def test_non_string_store_pin_keys_fail_closed(tmp_path, monkeypatch):
    """AC 6: a non-string ``artifact_manifest``/``artifact_member`` renders
    that one item ``invalid_evidence`` -- an opt-in requirement never fails
    open -- and only that item."""
    monkeypatch.chdir(tmp_path)
    report = _yield_report(tmp_path)

    for overrides in (
        {"artifact_manifest": 5},
        {"artifact_member": ["x"]},
        {"artifact_member": 7},
    ):
        result = _grade(tmp_path, {"file": report, **overrides})
        item_6 = _item_6(result)
        assert item_6["status"] == "unmet"
        assert item_6["reason"] == "invalid_evidence"
        assert item_6["citation"] is None


def test_no_store_pin_declared_is_unchanged(tmp_path, monkeypatch):
    """No ``artifact_manifest`` on the entry: the fallback never runs, and
    an unresolvable samples document reports exactly the pre-#5963 shape
    (``samples`` + ``searched`` only, no reason key)."""
    monkeypatch.chdir(tmp_path)
    report = _yield_report(tmp_path)

    result = _grade(tmp_path, {"file": report})

    item_6 = _item_6(result)
    assert item_6["status"] == "met"
    assert item_6["citation"]["content_hash_unresolved"] == {
        "samples": "mc-samples.json",
        "searched": ["block/yield/mc-samples.json", "mc-samples.json"],
    }
