"""``klt signoff --artifact-manifest``: grading ``artifact:<key>`` citations
(issue #5882) against a fake artifact broker -- no network, no credential."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from klayout_tools import artifact_store as store
from klayout_tools.signoff import artifact_resolution, build_tier_report
from test_artifact_store import REPO, _key, _sha, broker  # noqa: F401

INPUT_BYTES = b"design source\n"
INPUT_HASH = "sha256:" + hashlib.sha256(INPUT_BYTES).hexdigest()
PRODUCER = {"tool": "klt", "version": "0", "loom.repo": REPO, "commit": "c" * 40}


def _envelope(input_path: object) -> dict:
    return {
        "schema_version": 1,
        "kind": "generic",
        "status": "pass",
        "summary": "audited artifact for T1 item 1",
        "source": "design",
        "t1_item": 1,
        "provenance": {
            "klt_version": "0.5.0",
            "klayout_version": "0.30.10",
            "pdk": None,
            "deck": None,
            "input": {"path": input_path, "content_hash": INPUT_HASH},
        },
    }


def _store(fake, data: bytes) -> tuple[str, dict]:
    sha = _sha(data)
    key = _key(REPO, sha)
    fake.objects[key] = data
    entry = store.build_entry(
        sha256=sha,
        size=len(data),
        kind="evidence",
        producer=PRODUCER,
        uri=f"artifact:{key}",
    )
    return f"artifact:{key}", entry


def _write_manifest(tmp_path: Path, *entries: dict, name="artifacts.json") -> str:
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {"schema_version": store.MANIFEST_SCHEMA_VERSION, "entries": list(entries)}
        )
    )
    return str(path)


def _grade(evidence, tmp_path, *manifests):
    manifest = {"block": "demo", "kind": "analog", "evidence": {"1": evidence}}
    with artifact_resolution(list(manifests)):
        report = build_tier_report(manifest, repo_root=str(tmp_path))
    return next(i for i in report["items"] if i["id"] == 1)


@pytest.fixture
def real_input(tmp_path):
    (tmp_path / "design.txt").write_bytes(INPUT_BYTES)
    return {"path": "design.txt", "scope": "repo"}


def test_stored_envelope_cited_by_uri_is_met(broker, tmp_path, real_input):  # noqa: F811
    uri, entry = _store(broker, json.dumps(_envelope(real_input)).encode())
    manifest = _write_manifest(tmp_path, entry)

    item = _grade({"file": uri, "content_hash": INPUT_HASH}, tmp_path, manifest)

    assert item["status"] == "met", item
    assert item["citation"]["file"] == uri
    assert item["citation"]["input_verified"] is True


def test_bare_string_uri_is_resolved_and_read(broker, tmp_path, real_input):  # noqa: F811
    """Item 1 requires a pinned `content_hash`, so the bare form cannot be
    `met` -- but it must be fetched and classified (`unanchored_evidence`),
    not refused as an unreadable literal path."""
    uri, entry = _store(broker, json.dumps(_envelope(real_input)).encode())

    item = _grade(uri, tmp_path, _write_manifest(tmp_path, entry))

    assert item["reason"] == "unanchored_evidence"


def test_gzip_stored_envelope_is_read(broker, tmp_path, real_input):  # noqa: F811
    uri, entry = _store(
        broker, gzip.compress(json.dumps(_envelope(real_input)).encode())
    )
    item = _grade(
        {"file": uri, "content_hash": INPUT_HASH},
        tmp_path,
        _write_manifest(tmp_path, entry),
    )
    assert item["status"] == "met", item


def test_artifact_input_path_is_rehashed_from_the_store(broker, tmp_path):  # noqa: F811
    in_uri, in_entry = _store(broker, INPUT_BYTES)
    env_path = tmp_path / "envelope.json"
    env_path.write_text(json.dumps(_envelope({"path": in_uri, "scope": "repo"})))

    item = _grade(
        {"file": str(env_path), "content_hash": INPUT_HASH},
        tmp_path,
        _write_manifest(tmp_path, in_entry),
    )

    assert item["status"] == "met", item
    assert item["citation"]["input_verified"] is True


def test_artifact_input_with_wrong_bytes_is_stale(broker, tmp_path):  # noqa: F811
    in_uri, in_entry = _store(broker, b"someone edited this\n")
    env_path = tmp_path / "envelope.json"
    env_path.write_text(json.dumps(_envelope({"path": in_uri, "scope": "repo"})))

    item = _grade(
        {"file": str(env_path), "content_hash": INPUT_HASH},
        tmp_path,
        _write_manifest(tmp_path, in_entry),
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "stale_evidence"


def test_real_path_control_is_unchanged(tmp_path, real_input):
    env_path = tmp_path / "envelope.json"
    env_path.write_text(json.dumps(_envelope(real_input)))
    item = _grade({"file": str(env_path), "content_hash": INPUT_HASH}, tmp_path)
    assert item["status"] == "met"
    assert item["citation"]["input_verified"] is True


def _assert_artifact_unmet(item, uri, cause):
    assert item["status"] == "unmet"
    assert item["reason"] == "artifact_unavailable"
    assert item["detail"]["artifact"] == uri
    assert cause in item["detail"]["problem"]
    assert "artifact:" not in item["detail"].get("path", "")


def test_no_manifest_flag_is_artifact_unavailable(tmp_path):
    uri = f"artifact:{_key(REPO, 'a' * 64)}"
    item = _grade({"file": uri, "content_hash": INPUT_HASH}, tmp_path)
    _assert_artifact_unmet(item, uri, "no_manifest")


def test_uri_missing_from_manifest(broker, tmp_path):  # noqa: F811
    _, other = _store(broker, b"other\n")
    uri = f"artifact:{_key(REPO, 'a' * 64)}"
    item = _grade(uri, tmp_path, _write_manifest(tmp_path, other))
    _assert_artifact_unmet(item, uri, "no_entry")


def test_broker_corruption_is_never_met(broker, tmp_path, real_input):  # noqa: F811
    uri, entry = _store(broker, json.dumps(_envelope(real_input)).encode())
    broker.corrupt = True
    item = _grade(uri, tmp_path, _write_manifest(tmp_path, entry))
    assert item["status"] == "unmet"
    assert item["reason"] == "artifact_unavailable"


def test_broker_unreachable_is_unmet(monkeypatch, tmp_path, real_input):
    monkeypatch.delenv(store.ENV_BROKER, raising=False)
    data = json.dumps(_envelope(real_input)).encode()
    sha = _sha(data)
    uri = f"artifact:{_key(REPO, sha)}"
    entry = store.build_entry(
        sha256=sha, size=len(data), kind="evidence", producer=PRODUCER, uri=uri
    )
    item = _grade(uri, tmp_path, _write_manifest(tmp_path, entry))
    _assert_artifact_unmet(item, uri, "broker_unconfigured")


def test_key_in_two_manifests_resolves(broker, tmp_path, real_input):  # noqa: F811
    uri, entry = _store(broker, json.dumps(_envelope(real_input)).encode())
    first = _write_manifest(tmp_path, entry, name="a.json")
    second = _write_manifest(tmp_path, entry, name="b.json")
    assert (
        _grade({"file": uri, "content_hash": INPUT_HASH}, tmp_path, first, second)[
            "status"
        ]
        == "met"
    )


def test_cli_flag_resolves(broker, tmp_path, real_input, capsys):  # noqa: F811
    from klayout_tools.cli import main

    uri, entry = _store(broker, json.dumps(_envelope(real_input)).encode())
    manifest = tmp_path / "block.json"
    manifest.write_text(
        json.dumps(
            {
                "block": "demo",
                "kind": "analog",
                "evidence": {"1": {"file": uri, "content_hash": INPUT_HASH}},
            }
        )
    )
    main(
        [
            "signoff",
            "--manifest",
            str(manifest),
            "--repo-root",
            str(tmp_path),
            "--artifact-manifest",
            _write_manifest(tmp_path, entry),
            "--format",
            "json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    item = next(i for i in report["items"] if i["id"] == 1)
    assert item["status"] == "met"
