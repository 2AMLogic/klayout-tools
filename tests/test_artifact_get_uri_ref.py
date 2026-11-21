"""``klt artifact get|delete`` accept the ``artifact:<key>`` manifest ``uri`` as a
REF (issue #6721).

A manifest entry publishes ``uri: artifact:<key>`` (ruling H1, 2am#5553). Before
this, handing that same string back to ``get`` fell through to the
``MANIFEST[#SELECTOR]`` branch and failed as ``cannot read manifest ...: No such
file or directory`` -- a misdirection toward a file that was never a path.

The broker fixtures and helpers come from ``test_artifact_store`` (a top-level
module: ``tests/`` has no ``__init__.py``), which sits at its file-size ceiling
and so cannot take these cases itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import artifact_objects as store
from test_artifact_store import REPO, _key, _put, _run, _sha

# Fixtures pytest must find in *this* module's namespace: `broker` and `report`
# are requested by name below, `no_url_leak` is autouse (the secret-leak guard
# every artifact test runs under).
from test_artifact_store import broker as broker
from test_artifact_store import no_url_leak as no_url_leak
from test_artifact_store import report as report


def _stored(report: Path, manifest: Path, capsys) -> tuple[str, str]:
    """Put ``report`` and return ``(sha256, artifact:<key>)`` as the manifest
    publishes them."""
    code, _ = _put(report, manifest, capsys)
    assert code == 0
    entry = json.loads(manifest.read_text())["entries"][0]
    return entry["sha256"], entry["uri"]


def test_get_artifact_uri_to_a_file_without_repo(broker, report, tmp_path, capsys):
    sha256, uri = _stored(report, tmp_path / "m.json", capsys)
    assert uri == f"artifact:{_key(REPO, sha256)}"
    out_path = tmp_path / "fetched"

    # No --repo: the repo comes from the key's evidence/<owner>__<name>/ segment.
    code, _, err = _run(
        ["artifact", "get", uri, "-o", str(out_path), "--format", "json"], capsys
    )

    assert code == 0, err
    assert out_path.read_bytes() == report.read_bytes()


def test_get_artifact_uri_to_stdout_emits_only_the_verified_bytes(
    broker, report, tmp_path, capsys
):
    _, uri = _stored(report, tmp_path / "m.json", capsys)

    code, out, err = _run(["artifact", "get", uri, "-o", "-"], capsys)

    assert code == 0, err
    assert out == report.read_text()  # no envelope
    assert err == ""


def test_get_artifact_uri_names_the_same_key_as_the_bare_form(
    broker, report, tmp_path, capsys
):
    sha256, uri = _stored(report, tmp_path / "m.json", capsys)

    payloads = []
    for ref, extra in ((uri, []), (sha256, ["--repo", REPO])):
        code, out, err = _run(
            ["artifact", "get", ref, "-o", str(tmp_path / "o"), "--force"]
            + ["--format", "json"]
            + extra,
            capsys,
        )
        assert code == 0, err
        payloads.append(json.loads(out))

    assert payloads[0] == payloads[1]


def test_get_artifact_uri_refuses_a_broker_key_that_differs(
    broker, report, tmp_path, capsys
):
    sha256, _ = _stored(report, tmp_path / "m.json", capsys)
    # Same digest and repo, but not the key the broker will answer with.
    uri = f"artifact:evidence/acme__widgets/zz/{sha256}"
    assert uri != f"artifact:{_key(REPO, sha256)}"

    code, _, err = _run(
        ["artifact", "get", uri, "-o", str(tmp_path / "x"), "--format", "json"], capsys
    )

    assert code == 1
    assert json.loads(err)["error"]["code"] == "key_mismatch"
    assert not (tmp_path / "x").exists()


def test_get_artifact_uri_refuses_a_broker_key_naming_another_digest(
    broker, report, tmp_path, capsys
):
    _, uri = _stored(report, tmp_path / "m.json", capsys)
    broker.wrong_key = True

    code, _, err = _run(
        ["artifact", "get", uri, "-o", str(tmp_path / "x"), "--format", "json"], capsys
    )

    assert code == 1
    assert json.loads(err)["error"]["code"] == "key_mismatch"
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize(
    "ref",
    [
        "artifact:",
        "artifact:evidence/x",
        "artifact:evidence/acme__widgets/ab/not-a-sha",
        # 64 chars, but uppercase hex: the digest check is lowercase-only.
        "artifact:evidence/acme__widgets/AB/" + "AB" * 32,
        # 63 hex digits.
        "artifact:evidence/acme__widgets/ab/" + "ab" * 31 + "a",
        "artifact://evidence/acme__widgets/ab/" + "ab" * 32,
    ],
)
def test_malformed_artifact_uri_is_refused_without_the_manifest_misdirection(
    broker, tmp_path, capsys, ref
):
    fetches = len(broker.requests)

    code, _, err = _run(
        ["artifact", "get", ref, "-o", str(tmp_path / "x"), "--format", "json"], capsys
    )

    assert code == 1
    error = json.loads(err)["error"]
    assert error["code"]  # a structured code, not a bare OSError string
    assert "cannot read manifest" not in error["message"]
    assert "No such file" not in error["message"]
    assert len(broker.requests) == fetches  # refused before asking the broker
    assert not (tmp_path / "x").exists()


def test_missing_manifest_path_still_says_cannot_read_manifest(
    broker, tmp_path, capsys
):
    code, _, err = _run(
        [
            "artifact",
            "get",
            str(tmp_path / "no-such-manifest.json"),
            "-o",
            str(tmp_path / "x"),
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 1
    assert "cannot read manifest" in json.loads(err)["error"]["message"]


def test_artifact_uri_without_an_evidence_key_falls_back_to_repo(
    broker, report, tmp_path, capsys
):
    sha256, _ = _stored(report, tmp_path / "m.json", capsys)
    # repo_from_key is None for a key that is not evidence/<owner>__<name>/...
    uri = f"artifact:blob/{sha256}"
    assert store.target_for_uri(uri, REPO).repo == REPO

    code, _, err = _run(
        [
            "artifact",
            "get",
            uri,
            "--repo",
            REPO,
            "-o",
            str(tmp_path / "x"),
            "--format",
            "json",
        ],
        capsys,
    )

    # The fallback repo is used; the broker's own key then differs from the
    # URI's, which is the integrity refusal rather than a repo failure.
    assert code == 1
    assert json.loads(err)["error"]["code"] == "key_mismatch"


def test_artifact_uri_without_an_evidence_key_and_no_repo_surfaces_resolve_repo(
    broker, report, tmp_path, capsys, monkeypatch
):
    sha256, _ = _stored(report, tmp_path / "m.json", capsys)
    monkeypatch.setattr(store, "_git", lambda *a, **k: None)  # no origin remote

    code, _, err = _run(
        [
            "artifact",
            "get",
            f"artifact:blob/{sha256}",
            "-o",
            str(tmp_path / "x"),
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 1
    assert json.loads(err)["error"]["code"] == "repo_unconfigured"


def test_delete_artifact_uri_resolves_to_the_same_target_as_get(
    broker, report, tmp_path, capsys
):
    from klayout_tools.cli import artifact_cmd

    sha256, uri = _stored(report, tmp_path / "m.json", capsys)
    stored_key = _key(REPO, sha256)

    target, entry = artifact_cmd._target_for_ref(uri, None)
    assert entry is None
    assert (target.kind, target.repo, target.sha256, target.key) == (
        "evidence",
        REPO,
        sha256,
        stored_key,
    )
    assert target.size is None  # a URI carries no size

    # Through the CLI: the dry run reports the object `get` resolved, and
    # deletes nothing.
    code, out, err = _run(["artifact", "delete", uri, "--format", "json"], capsys)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["status"] == "dry_run"
    assert [(o["sha256"], o["key"], o["status"]) for o in payload["objects"]] == [
        (sha256, stored_key, "would_delete")
    ]
    assert stored_key in broker.objects  # a dry run deletes nothing
    assert _sha(report.read_bytes()) == sha256
