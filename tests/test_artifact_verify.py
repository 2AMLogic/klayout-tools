"""Tests for the standalone artifact verifier (issue #5958).

``klayout_tools/artifact_verify.py`` is the stdlib-only, vendorable twin of
``klt artifact verify``: same code (``artifact_objects`` imports it), same
JSON report, same exit codes. These tests bite on that threefold claim:

- the module imports nothing but the stdlib (AST check) and a copy of it,
  run under ``python -I -S`` from an empty directory with no
  ``klayout_tools`` importable, reports byte-for-byte what ``klt artifact
  verify`` reports against the same fake broker;
- every verdict (``ok``, ``missing``, ``size_mismatch``, ``hash_mismatch``,
  ``key_mismatch``) is produced by that standalone entry point, and a broker
  refusal fails closed (exit 1, never ``ok``);
- a presigned URL never reaches stdout, stderr or logs;
- ``verify_manifest`` accepts an injected in-process fake client and opens
  no socket at all -- the offline CI seam chip repos need.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from klayout_tools import artifact_objects as store
from klayout_tools import artifact_verify
from klayout_tools.cli import main

REPO = "acme/widgets"
SECRET = "X-Amz-Signature=fake0secret0signature0do0not0print"
PAYLOAD = b'{"status": "clean"}\n'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(repo: str, sha256: str) -> str:
    return f"evidence/{repo.replace('/', '__')}/{sha256[:2]}/{sha256}"


def _entry(sha256: str, size: int) -> dict[str, Any]:
    return {
        "sha256": sha256,
        "size": size,
        "kind": "evidence",
        "producer": {"tool": "klt", "version": "0", "loom.repo": REPO, "commit": None},
        "uri": f"artifact:{_key(REPO, sha256)}",
        "name": "report.json",
    }


def _manifest(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    path = tmp_path / "artifacts.manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "entries": entries}))
    return path


class FakeBroker:
    """The verify-facing half of the fake in ``test_artifact_store.py``:
    ``stat``/``get`` presign answers plus the object store behind the
    presigned URL, with tamper and wrong-key knobs per test."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.refuse: dict[str, tuple[int, str, str]] = {}
        self.corrupt = False
        self.wrong_key = False
        self.base = ""

    def answer(self, op: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if op in self.refuse:
            status, error, message = self.refuse[op]
            return status, {"error": error, "message": message}
        sha256 = body["sha256"]
        stored = _key(body["repo"], sha256)
        present = stored in self.objects
        key = _key(body["repo"], "0" * 64) if self.wrong_key else stored
        if op == "stat":
            return 200, {
                "key": key,
                "exists": present,
                "size": len(self.objects.get(stored, b"")),
            }
        if not present:
            return 404, {"error": "not_found", "message": "no such object"}
        return 200, {
            "key": key,
            "url": f"{self.base}/s3/{stored}?{SECRET}",
            "method": "GET",
            "exists": True,
            "size": len(self.objects[stored]),
        }


class Handler(BaseHTTPRequestHandler):
    @property
    def fake(self) -> FakeBroker:
        return self.server.fake  # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:  # keep test output quiet
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        op = self.path.removeprefix("/v1/")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        status, payload = self.fake.answer(op, body)
        self._send(status, json.dumps(payload).encode())

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.query != SECRET or not parts.path.startswith("/s3/"):
            self._send(403, b"<Error><Code>AccessDenied</Code></Error>")
            return
        data = self.fake.objects.get(parts.path.removeprefix("/s3/"))
        if data is None:
            self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")
            return
        if self.fake.corrupt:
            data = data + b"tampered"
        self._send(200, data, "application/octet-stream")


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch):
    fake = FakeBroker()
    fake.objects[_key(REPO, _sha(PAYLOAD))] = PAYLOAD
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.fake = fake  # type: ignore[attr-defined]
    fake.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(artifact_verify.ENV_BROKER, fake.base)
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def no_url_leak(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    yield
    assert "fake0secret" not in caplog.text


def _klt_verify(manifest: Path, capsys) -> tuple[int, dict[str, Any]]:
    code = main(["artifact", "verify", str(manifest), "--format", "json"])
    out, err = capsys.readouterr()
    assert "fake0secret" not in out + err
    return code, json.loads(out if code in (0, 3) else err)


@pytest.fixture
def vendor(tmp_path: Path) -> Path:
    """A vendored copy of ``artifact_verify.py`` in an empty directory, run
    with ``-I -S`` so neither the script's directory nor any site-packages
    (the worktree venv's editable ``klayout_tools`` included) is importable:
    only the stdlib can satisfy its imports."""
    directory = tmp_path / "vendor"
    directory.mkdir()
    path = directory / "artifact_verify.py"
    path.write_text(Path(artifact_verify.__file__).read_text())
    return path


def _run_vendored(
    vendor: Path, manifest: Path, base: str, fmt: str = "json"
) -> tuple[int, str, str]:
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(vendor),
            str(manifest),
            "--broker",
            base,
            "--format",
            fmt,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=vendor.parent,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_module_imports_stdlib_only():
    """No relative imports; every imported module is stdlib (the AST half of
    the vendoring claim; the ``python -I -S`` runs below are the other)."""
    tree = ast.parse(Path(artifact_verify.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import defeats vendoring"
            assert node.module == "__future__" or (
                node.module.split(".")[0] in sys.stdlib_module_names
            ), f"non-stdlib import: {node.module}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in sys.stdlib_module_names


def test_klt_verify_and_the_module_are_one_implementation():
    assert store.verify_manifest is artifact_verify.verify_manifest
    assert store.BrokerClient is artifact_verify.BrokerClient


def test_vendored_copy_reports_what_klt_reports(broker, tmp_path, vendor, capsys):
    """A ``python -I -S`` run of the isolated copy, with no
    ``klayout_tools`` importable, emits the same report and exit code as
    ``klt artifact verify`` against the same broker."""
    manifest = _manifest(tmp_path, [_entry(_sha(PAYLOAD), len(PAYLOAD))])

    klt_code, klt_payload = _klt_verify(manifest, capsys)
    code, out, err = _run_vendored(vendor, manifest, broker.base)

    assert klt_code == 0
    assert code == 0
    assert "fake0secret" not in out + err
    assert json.loads(out) == klt_payload


@pytest.mark.parametrize(
    ("knob", "expected_status"),
    [
        (lambda fake: None, "ok"),
        (lambda fake: fake.objects.clear(), "missing"),
        ("size_mismatch", "size_mismatch"),
        (lambda fake: setattr(fake, "corrupt", True), "hash_mismatch"),
        (lambda fake: setattr(fake, "wrong_key", True), "key_mismatch"),
    ],
)
def test_vendored_copy_produces_every_verdict(
    broker, tmp_path, vendor, capsys, knob, expected_status
):
    manifest = _manifest(tmp_path, [_entry(_sha(PAYLOAD), len(PAYLOAD))])
    if knob == "size_mismatch":
        data = json.loads(manifest.read_text())
        data["entries"][0]["size"] += 1
        manifest.write_text(json.dumps(data))
    else:
        knob(broker)

    code, out, err = _run_vendored(vendor, manifest, broker.base)

    assert "fake0secret" not in out + err  # presigned URL never surfaces
    payload = json.loads(out if code in (0, 3) else err)
    if expected_status == "ok":
        assert code == 0 and payload["status"] == "ok"
    else:
        assert code == 3, "a missing/mismatched entry must fail closed"
        assert payload["entries"][0]["status"] == expected_status


def test_vendored_copy_fails_closed_on_broker_refusal(broker, tmp_path, vendor):
    broker.refuse["stat"] = (403, "repo_firewalled", "this repo is firewalled")
    manifest = _manifest(tmp_path, [_entry(_sha(PAYLOAD), len(PAYLOAD))])

    code, out, err = _run_vendored(vendor, manifest, broker.base)

    assert code == 1
    assert out == ""
    assert "fake0secret" not in err
    error = json.loads(err)["error"]
    assert error["code"] == "broker_refused"
    assert error["broker_status"] == 403


def test_standalone_reports_rate_limited_without_sleeping(
    broker, tmp_path, monkeypatch, capsys
):
    """The standalone ``main`` (the vendored file's entry point, run
    in-process) surfaces an exhausted 429 as ``broker_rate_limited`` with
    exit 1 -- never an ``ok`` verdict."""
    queue = [429] * 10
    real = FakeBroker.answer
    sleeps: list[float] = []

    def throttled(self, op, body):
        if queue:
            queue.pop()
            return 429, {"error": "rate_limited", "message": "slow down"}
        return real(self, op, body)

    monkeypatch.setattr(FakeBroker, "answer", throttled)
    monkeypatch.setattr(artifact_verify, "_sleep", sleeps.append)
    manifest = _manifest(tmp_path, [_entry(_sha(PAYLOAD), len(PAYLOAD))])

    code = artifact_verify.main([str(manifest), "--format", "json"])

    out, err = capsys.readouterr()
    assert code == 1
    assert "fake0secret" not in out + err
    assert json.loads(err)["error"]["code"] == "broker_rate_limited"
    assert len(sleeps) == artifact_verify.RATE_LIMIT_ATTEMPTS - 1


class FakeClient:
    """The offline CI seam: an in-process broker stand-in with the
    ``BrokerClient`` surface verify uses (``presign``, ``fetch_into``); no
    URL is ever dereferenced, so no socket is ever opened."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def presign(self, op, kind, repo, sha256, size=None):
        data = self.objects.get(sha256)
        if data is None:
            return artifact_verify.Presigned(
                key=_key(repo, sha256), exists=False, size=0
            )
        return artifact_verify.Presigned(
            key=_key(repo, sha256), exists=True, size=len(data)
        )

    def fetch_into(self, target, sink):
        data = self.objects[target.sha256]
        hasher = hashlib.sha256()
        if sink is not None:
            sink(data)
        hasher.update(data)
        return {
            "key": target.key or _key(target.repo, target.sha256),
            "sha256": hasher.hexdigest(),
            "size": len(data),
        }


def test_verify_manifest_through_injected_client_opens_no_socket(tmp_path, monkeypatch):
    """The chip-repo CI seam: inject a fake client, refuse every socket, and
    the exact ``verify_manifest`` code still verdicts every entry."""
    manifest = _manifest(tmp_path, [_entry(_sha(PAYLOAD), len(PAYLOAD))])

    def no_sockets(*args, **kwargs):
        raise AssertionError("a socket was opened by the offline seam")

    monkeypatch.setattr(socket, "socket", no_sockets)
    monkeypatch.setattr(socket, "create_connection", no_sockets)

    report = artifact_verify.verify_manifest(
        FakeClient({_sha(PAYLOAD): PAYLOAD}), str(manifest)
    )

    assert report["status"] == "ok"
    assert report["entries"][0]["status"] == "ok"

    assert (
        artifact_verify.verify_manifest(FakeClient({}), str(manifest))["entries"][0][
            "status"
        ]
        == "missing"
    )


def test_standalone_zero_entries_is_ok(broker, tmp_path, capsys):
    manifest = _manifest(tmp_path, [])

    code = artifact_verify.main([str(manifest), "--format", "json"])

    out, _ = capsys.readouterr()
    assert code == 0
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["entry_count"] == 0
    assert payload["failed_count"] == 0


def test_standalone_rejects_non_artifact_uri_before_the_broker(
    broker, tmp_path, monkeypatch
):
    entry = _entry(_sha(PAYLOAD), len(PAYLOAD))
    entry["uri"] = f"s3://some-bucket/{_key(REPO, entry['sha256'])}"
    manifest = _manifest(tmp_path, [entry])
    calls = 0
    real = FakeBroker.answer

    def counting(self, op, body):
        nonlocal calls
        calls += 1
        return real(self, op, body)

    monkeypatch.setattr(FakeBroker, "answer", counting)

    code = artifact_verify.main([str(manifest), "--format", "json"])

    assert code == 1
    assert calls == 0  # refused before asking the broker
