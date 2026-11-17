"""Unit-level ``klt artifact verify --local`` cases (issue #5591), moved out
of :mod:`test_artifact_store` to keep that file under its per-file size
ratchet ceiling (issue #5464). They reuse its fake-broker fixtures and
helpers; every case here runs with :class:`BrokerClient` poisoned --
``--local`` never contacts a broker.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from klayout_tools import artifact_objects as store
from klayout_tools import artifact_verify as av
from test_artifact_store import REPO, _put, _run, _stored
from test_artifact_store import broker as broker
from test_artifact_store import report as report

# --------------------------------------------------------------------------
# verify --local: broker-free re-hash of files under a root (issue #5591)
# --------------------------------------------------------------------------


class _ForbiddenBroker:
    """Any construction is a failure: --local never talks to a broker."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("verify --local constructed a BrokerClient")


def test_verify_local_ok_whole_and_selector(
    broker, report, tmp_path, capsys, monkeypatch
):
    manifest, sha = _stored(broker, report, tmp_path, capsys)
    monkeypatch.delenv(store.ENV_BROKER, raising=False)
    monkeypatch.setattr(av, "BrokerClient", _ForbiddenBroker)

    argv = [
        "artifact",
        "verify",
        "--local",
        str(manifest),
        "--root",
        str(tmp_path),
        "--format",
        "json",
    ]
    code, out, _ = _run(argv, capsys)
    payload = json.loads(out)
    assert code == 0
    assert payload["status"] == "ok"
    assert payload["mode"] == "local"
    assert payload["root"] == str(tmp_path)
    assert payload["entry_count"] == 1 and payload["failed_count"] == 0
    entry = payload["entries"][0]
    assert entry["status"] == "ok"
    assert entry["path"] == str(tmp_path / "report.json")

    for selector in (sha, "report.json"):
        code, out, _ = _run(
            [
                "artifact",
                "verify",
                "--local",
                f"{manifest}#{selector}",
                "--root",
                str(tmp_path),
                "--format",
                "json",
            ],
            capsys,
        )
        payload = json.loads(out)
        assert code == 0 and payload["entry_count"] == 1
        assert payload["entries"][0]["status"] == "ok"


def test_verify_local_text_output_carries_path(broker, report, tmp_path, capsys):
    manifest, _ = _stored(broker, report, tmp_path, capsys)
    code, out, _ = _run(
        ["artifact", "verify", "--local", str(manifest), "--root", str(tmp_path)],
        capsys,
    )
    assert code == 0
    assert "ok" in out and str(tmp_path / "report.json") in out


def test_verify_local_tamper_statuses(broker, report, tmp_path, capsys):
    manifest, _ = _stored(broker, report, tmp_path, capsys)
    original = report.read_bytes()
    argv = [
        "artifact",
        "verify",
        "--local",
        str(manifest),
        "--root",
        str(tmp_path),
        "--format",
        "json",
    ]

    report.write_bytes(original + b"appended")
    code, out, _ = _run(argv, capsys)
    assert code == 3
    assert json.loads(out)["entries"][0]["status"] == "hash_mismatch"

    report.write_bytes(original[:-1])
    code, out, _ = _run(argv, capsys)
    assert code == 3
    assert json.loads(out)["entries"][0]["status"] == "size_mismatch"

    report.write_bytes(b'{"status": "cleaz"}\n')  # same-length content edit
    code, out, _ = _run(argv, capsys)
    assert code == 3
    assert json.loads(out)["entries"][0]["status"] == "hash_mismatch"


def test_verify_local_absent_selector_fails_absent_without_passes(
    broker, report, tmp_path, capsys
):
    manifest, _ = _stored(broker, report, tmp_path, capsys)
    extra = tmp_path / "extra.json"
    extra.write_bytes(b'{"ok": true}\n')
    code, _ = _put(extra, manifest, capsys)
    assert code == 0
    report.unlink()  # the large object: store-only, absent locally

    code, out, _ = _run(
        [
            "artifact",
            "verify",
            "--local",
            f"{manifest}#report.json",
            "--root",
            str(tmp_path),
            "--format",
            "json",
        ],
        capsys,
    )
    assert code == 3
    payload = json.loads(out)
    assert payload["entries"][0]["status"] == "absent"

    code, out, _ = _run(
        [
            "artifact",
            "verify",
            "--local",
            str(manifest),
            "--root",
            str(tmp_path),
            "--format",
            "json",
        ],
        capsys,
    )
    payload = json.loads(out)
    assert code == 0  # absent entries are reported, not failures
    assert payload["entry_count"] == 2
    assert sorted(e["status"] for e in payload["entries"]) == ["absent", "ok"]


def test_verify_local_nothing_verified(broker, report, tmp_path, capsys):
    manifest, _ = _stored(broker, report, tmp_path, capsys)
    report.unlink()
    code, _, err = _run(
        [
            "artifact",
            "verify",
            "--local",
            str(manifest),
            "--root",
            str(tmp_path),
            "--format",
            "json",
        ],
        capsys,
    )
    assert code == 1
    assert json.loads(err)["error"]["code"] == "nothing_verified"


def test_verify_local_malformed_manifest(tmp_path, capsys):
    manifest = tmp_path / "m.json"
    manifest.write_text('{"schema_version": 1}')
    code, _, err = _run(
        [
            "artifact",
            "verify",
            "--local",
            str(manifest),
            "--root",
            str(tmp_path),
            "--format",
            "json",
        ],
        capsys,
    )
    assert code == 1
    assert json.loads(err)["error"]["code"] == "invalid_manifest"


def test_verify_local_entry_without_name_is_reported(broker, tmp_path, capsys):
    named = tmp_path / "named.json"
    named.write_bytes(b'{"a": 1}\n')
    sha, size = store.hash_file(str(named))
    producer = {"tool": "klt", "version": "0", "loom.repo": REPO, "commit": None}

    def entry(name: str | None) -> dict[str, Any]:
        return store.build_entry(
            sha256=sha,
            size=size,
            kind="evidence",
            producer=producer,
            uri=f"artifact:evidence/{REPO.replace('/', '__')}/{sha[:2]}/{sha}",
            name=name,
        )

    manifest = tmp_path / "m.json"
    store.add_entries(str(manifest), [entry(None)])
    argv = [
        "artifact",
        "verify",
        "--local",
        str(manifest),
        "--root",
        str(tmp_path),
        "--format",
        "json",
    ]
    code, _, err = _run(argv, capsys)
    assert code == 1  # nothing locally checkable: no entry resolved
    assert json.loads(err)["error"]["code"] == "nothing_verified"

    store.add_entries(str(manifest), [entry("named.json")])
    code, out, _ = _run(argv, capsys)
    payload = json.loads(out)
    assert code == 0  # the unnamed entry is reported, not a failure
    statuses = sorted(e["status"] for e in payload["entries"])
    assert statuses == ["no_name", "ok"]


def test_verify_local_refuses_names_that_escape_the_root(broker, tmp_path, capsys):
    root = tmp_path / "checkout"
    (root / "sim").mkdir(parents=True)
    evidence = root / "sim" / "evidence.json"
    evidence.write_bytes(b'{"ok": true}\n')
    manifest = root / "m.json"
    code, _ = _put(evidence, manifest, capsys)
    assert code == 0
    secret = tmp_path / "secret.txt"  # outside the verify root
    secret.write_bytes(b"secret bytes")
    secret_sha, secret_size = store.hash_file(str(secret))
    producer = {"tool": "klt", "version": "0", "loom.repo": REPO, "commit": None}

    def escape_entry(name: str) -> dict[str, Any]:
        sha_uri = f"artifact:evidence/{REPO.replace('/', '__')}/{secret_sha[:2]}"
        return store.build_entry(
            sha256=secret_sha,
            size=secret_size,
            kind="evidence",
            producer=producer,
            uri=f"{sha_uri}/{secret_sha}",
            name=name,
        )

    argv = [
        "artifact",
        "verify",
        "--local",
        str(manifest),
        "--root",
        str(root),
        "--format",
        "json",
    ]
    for name in ("../secret.txt", str(secret)):
        escape = root / "escape.json"
        store.add_entries(str(escape), [escape_entry(name)])
        code, _, err = _run([*argv[:3], str(escape), *argv[4:]], capsys)
        assert code == 1, name
        error = json.loads(err)["error"]
        assert error["code"] == "name_escapes_root", name

    # A symlink whose target escapes the root is refused before it is read.
    linked = root / "link.json"
    linked.symlink_to(secret)
    store.add_entries(str(manifest), [escape_entry("link.json")])
    code, _, err = _run(argv, capsys)
    assert code == 1
    assert json.loads(err)["error"]["code"] == "name_escapes_root"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads chmod-000 files")
def test_verify_local_unreadable_file_is_not_absent(broker, report, tmp_path, capsys):
    manifest, _ = _stored(broker, report, tmp_path, capsys)
    report.chmod(0o000)
    code, _, err = _run(
        [
            "artifact",
            "verify",
            "--local",
            str(manifest),
            "--root",
            str(tmp_path),
            "--format",
            "json",
        ],
        capsys,
    )
    assert code == 1
    assert json.loads(err)["error"]["code"] == "unreadable_input"


def test_verify_local_root_requires_local(broker, report, tmp_path, capsys):
    manifest, _ = _stored(broker, report, tmp_path, capsys)
    code, _, err = _run(
        [
            "artifact",
            "verify",
            str(manifest),
            "--root",
            str(tmp_path),
            "--format",
            "json",
        ],
        capsys,
    )
    assert code == 2
    assert "--local" in err
