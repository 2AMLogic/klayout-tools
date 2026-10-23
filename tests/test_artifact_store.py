"""Tests for ``klt artifact put|get|verify`` (issue #4063) against a fake
broker: a loopback ``http.server`` that speaks the broker's JSON contract and
also plays the object store behind its presigned URLs. No network, no cloud
SDK, no credential.

Every presigned URL the fake mints carries :data:`SECRET`; :func:`_run` fails
on it in stdout or stderr and the ``no_url_leak`` fixture on it in any log
record.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from klayout_tools import artifact_store as store
from klayout_tools import signoff
from klayout_tools.cli import main

REPO = "acme/widgets"
SECRET = "X-Amz-Signature=fake0secret0signature0do0not0print"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(repo: str, sha256: str) -> str:
    return f"evidence/{repo.replace('/', '__')}/{sha256[:2]}/{sha256}"


class FakeBroker:
    """State shared with the request handler."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.put_headers: list[dict[str, str]] = []
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
        if op == "put":
            if present:
                return 200, {"key": key, "exists": True}
            checksum = base64.b64encode(bytes.fromhex(sha256)).decode()
            return 200, {
                "key": key,
                "url": f"{self.base}/s3/{key}?{SECRET}",
                "method": "PUT",
                "headers": {"x-amz-checksum-sha256": checksum},
                "expires_at": "2026-10-22T00:05:00Z",
                "exists": False,
            }
        if not present:
            return 404, {"error": "not_found", "message": "no such object"}
        return 200, {
            "key": key,
            "url": f"{self.base}/s3/{key}?{SECRET}",
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

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def do_POST(self) -> None:
        op = self.path.removeprefix("/v1/")
        body = json.loads(self._body())
        self.fake.requests.append(("POST", op, body))
        status, payload = self.fake.answer(op, body)
        self._send(status, json.dumps(payload).encode())

    def _object_key(self) -> str | None:
        parts = urlsplit(self.path)
        if parts.query != SECRET or not parts.path.startswith("/s3/"):
            return None
        return parts.path.removeprefix("/s3/")

    def do_PUT(self) -> None:
        key = self._object_key()
        data = self._body()
        self.fake.requests.append(("PUT", key or "", {}))
        self.fake.put_headers.append(dict(self.headers.items()))
        want = base64.b64encode(hashlib.sha256(data).digest()).decode()
        if key is None or self.headers.get("x-amz-checksum-sha256") != want:
            err = b"<Error><Code>BadDigest</Code><Key>leaky</Key></Error>"
            self._send(400, err, "application/xml")
            return
        self.fake.objects[key] = data
        self._send(200, b"")

    def do_GET(self) -> None:
        key = self._object_key()
        self.fake.requests.append(("GET", key or "", {}))
        if key is None or key not in self.fake.objects:
            self._send(
                403, b"<Error><Code>AccessDenied</Code></Error>", "application/xml"
            )
            return
        data = self.fake.objects[key]
        if self.fake.corrupt:
            data = data + b"tampered"
        self._send(200, data, "application/octet-stream")


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch):
    fake = FakeBroker()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.fake = fake  # type: ignore[attr-defined]
    fake.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(store.ENV_BROKER, fake.base)
    monkeypatch.delenv(store.ENV_REPO, raising=False)
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


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    out, err = capsys.readouterr()
    assert "fake0secret" not in out + err
    return code, out, err


def _put(path: Path, manifest: Path, capsys, *extra: str) -> tuple[int, dict[str, Any]]:
    code, out, err = _run(
        [
            "artifact",
            "put",
            str(path),
            "--manifest",
            str(manifest),
            "--repo",
            REPO,
            "--commit",
            "c0ffee",
            "--format",
            "json",
            *extra,
        ],
        capsys,
    )
    return code, json.loads(out) if code == 0 else json.loads(err)


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "report.json"
    path.write_bytes(b'{"status": "clean"}\n')
    return path


def test_put_uploads_with_broker_headers_and_writes_manifest(
    broker, report, tmp_path, capsys
):
    manifest = tmp_path / "artifacts.manifest.json"
    code, payload = _put(report, manifest, capsys)

    assert code == 0
    sha256 = _sha(report.read_bytes())
    entry = payload["entries"][0]
    assert entry == {
        "sha256": sha256,
        "size": report.stat().st_size,
        "kind": "evidence",
        "producer": {
            "tool": "klt",
            "version": entry["producer"]["version"],
            "loom.repo": REPO,
            "commit": "c0ffee",
        },
        "uri": f"artifact:{_key(REPO, sha256)}",
        "name": "report.json",
        "media_type": "application/json",
    }
    assert payload["objects"] == [
        {"sha256": sha256, "key": _key(REPO, sha256), "uploaded": True}
    ]
    assert broker.objects[_key(REPO, sha256)] == report.read_bytes()
    assert json.loads(manifest.read_text()) == {"schema_version": 1, "entries": [entry]}
    assert store.entry_problems(entry) == []
    sent = {k.lower(): v for k, v in broker.put_headers[0].items()}
    assert (
        sent["x-amz-checksum-sha256"]
        == base64.b64encode(bytes.fromhex(sha256)).decode()
    )
    assert sent["content-length"] == str(report.stat().st_size)
    assert "content-type" not in sent  # exactly the broker's headers, nothing implicit


def test_reput_is_a_noop_success(broker, report, tmp_path, capsys):
    manifest = tmp_path / "m.json"
    assert _put(report, manifest, capsys)[0] == 0
    uploads = sum(1 for method, _, _ in broker.requests if method == "PUT")

    code, payload = _put(report, manifest, capsys)

    assert code == 0
    assert payload["objects"][0]["uploaded"] is False
    assert sum(1 for method, _, _ in broker.requests if method == "PUT") == uploads
    assert len(json.loads(manifest.read_text())["entries"]) == 1


def test_get_by_manifest_entry_writes_verified_bytes(broker, report, tmp_path, capsys):
    manifest = tmp_path / "m.json"
    _put(report, manifest, capsys)
    out_path = tmp_path / "fetched.json"

    code, out, _ = _run(
        [
            "artifact",
            "get",
            f"{manifest}#report.json",
            "-o",
            str(out_path),
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 0
    assert out_path.read_bytes() == report.read_bytes()
    payload = json.loads(out)
    assert payload["status"] == "verified"
    assert payload["sha256"] == _sha(report.read_bytes())
    assert "url" not in payload


def test_get_by_bare_sha256(broker, report, tmp_path, capsys):
    _put(report, tmp_path / "m.json", capsys)
    out_path = tmp_path / "fetched"

    code, _, _ = _run(
        [
            "artifact",
            "get",
            _sha(report.read_bytes()),
            "--repo",
            REPO,
            "-o",
            str(out_path),
        ],
        capsys,
    )

    assert code == 0
    assert out_path.read_bytes() == report.read_bytes()


def test_get_refuses_to_write_on_hash_mismatch(broker, report, tmp_path, capsys):
    manifest = tmp_path / "m.json"
    _put(report, manifest, capsys)
    broker.corrupt = True
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out_path = out_dir / "fetched.json"

    code, _, err = _run(
        ["artifact", "get", str(manifest), "-o", str(out_path), "--format", "json"],
        capsys,
    )

    assert code == 1
    assert json.loads(err)["error"]["code"] == "hash_mismatch"
    assert list(out_dir.iterdir()) == []  # nothing written, no temp file left


def test_get_refuses_a_key_naming_another_digest(broker, report, tmp_path, capsys):
    manifest = tmp_path / "m.json"
    _put(report, manifest, capsys)
    broker.wrong_key = True

    code, _, err = _run(
        [
            "artifact",
            "get",
            str(manifest),
            "-o",
            str(tmp_path / "x"),
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 1
    assert json.loads(err)["error"]["code"] == "key_mismatch"
    assert not (tmp_path / "x").exists()


def test_get_will_not_replace_existing_output_without_force(
    broker, report, tmp_path, capsys
):
    manifest = tmp_path / "m.json"
    _put(report, manifest, capsys)
    existing = tmp_path / "keep.json"
    existing.write_text("mine")

    code, _, _ = _run(["artifact", "get", str(manifest), "-o", str(existing)], capsys)
    assert code == 1
    assert existing.read_text() == "mine"

    code, _, _ = _run(
        ["artifact", "get", str(manifest), "-o", str(existing), "--force"], capsys
    )
    assert code == 0
    assert existing.read_bytes() == report.read_bytes()


def test_verify_ok_missing_and_mismatch(broker, report, tmp_path, capsys):
    manifest = tmp_path / "m.json"
    _put(report, manifest, capsys)

    code, out, _ = _run(
        ["artifact", "verify", str(manifest), "--format", "json"], capsys
    )
    assert code == 0
    assert json.loads(out)["status"] == "ok"

    broker.corrupt = True
    code, out, _ = _run(
        ["artifact", "verify", str(manifest), "--format", "json"], capsys
    )
    assert code == 3
    assert json.loads(out)["entries"][0]["status"] == "hash_mismatch"

    broker.corrupt = False
    broker.objects.clear()
    code, out, _ = _run(["artifact", "verify", str(manifest)], capsys)
    assert code == 3
    assert "missing" in out


def test_verify_detects_size_mismatch(broker, report, tmp_path, capsys):
    manifest = tmp_path / "m.json"
    _put(report, manifest, capsys)
    data = json.loads(manifest.read_text())
    data["entries"][0]["size"] += 1
    manifest.write_text(json.dumps(data))

    code, out, _ = _run(
        ["artifact", "verify", str(manifest), "--format", "json"], capsys
    )

    assert code == 3
    assert json.loads(out)["entries"][0]["status"] == "size_mismatch"


@pytest.mark.parametrize("op", ["put", "get", "stat"])
def test_broker_refusal_is_surfaced_verbatim(broker, report, tmp_path, capsys, op):
    manifest = tmp_path / "m.json"
    if op != "put":
        _put(report, manifest, capsys)
    broker.refuse[op] = (403, "repo_firewalled", "this repo is firewalled")

    if op == "put":
        argv = ["artifact", "put", str(report), "--repo", REPO, "--format", "json"]
    elif op == "get":
        argv = [
            "artifact",
            "get",
            str(manifest),
            "-o",
            str(tmp_path / "o"),
            "--format",
            "json",
        ]
    else:
        argv = ["artifact", "verify", str(manifest), "--format", "json"]
    code, out, err = _run(argv, capsys)

    assert code == 1
    assert out == ""
    error = json.loads(err)["error"]
    assert error["code"] == "broker_refused"
    assert error["broker_status"] == 403
    assert error["broker_error"] == "repo_firewalled"
    assert error["broker_message"] == "this repo is firewalled"
    assert "repo_firewalled" in error["message"]


def test_refusal_text_format(broker, report, capsys):
    broker.refuse["put"] = (
        503,
        "fleet_unavailable",
        "the fleet manifest cannot be read",
    )

    code, _, err = _run(["artifact", "put", str(report), "--repo", REPO], capsys)

    assert code == 1
    assert "fleet_unavailable" in err and "the fleet manifest cannot be read" in err


def test_put_refuses_a_checksum_binding_for_other_bytes(
    broker, report, monkeypatch, capsys
):
    original = broker.answer

    def bad_checksum(op, body):
        status, payload = original(op, body)
        if op == "put" and "headers" in payload:
            payload["headers"] = {
                "x-amz-checksum-sha256": base64.b64encode(b"\0" * 32).decode()
            }
        return status, payload

    monkeypatch.setattr(broker, "answer", bad_checksum)
    code, _, err = _run(
        ["artifact", "put", str(report), "--repo", REPO, "--format", "json"], capsys
    )

    assert code == 1
    assert json.loads(err)["error"]["code"] == "key_mismatch"
    assert broker.objects == {}  # refused before sending a byte


def test_transfer_failure_names_status_and_code_only(broker, report):
    sha256, size = store.hash_file(str(report))
    answer = store.Presigned(
        key=_key(REPO, sha256),
        exists=False,
        method="PUT",
        url=f"{broker.base}/s3/{_key(REPO, sha256)}?{SECRET}",
        headers={"x-amz-checksum-sha256": "wrong"},
    )
    with open(report, "rb") as handle, pytest.raises(store.ArtifactError) as excinfo:
        store._transfer(answer, "PUT", handle, size, None)
    assert excinfo.value.code == "transfer_failed"
    assert str(excinfo.value) == "presigned PUT was rejected: HTTP 400 (BadDigest)"
    assert "fake0secret" not in repr(answer)


def test_no_broker_configured(monkeypatch, report, capsys):
    monkeypatch.delenv(store.ENV_BROKER, raising=False)

    code, _, err = _run(
        ["artifact", "put", str(report), "--repo", REPO, "--format", "json"], capsys
    )

    assert code == 1
    assert json.loads(err)["error"]["code"] == "broker_unconfigured"


def test_put_writes_the_broker_key_verbatim_as_an_artifact_uri(
    broker, report, monkeypatch, capsys
):
    """The ``uri`` is ``artifact:<key>`` with whatever key the broker answers,
    never one ``klt`` assembles, and names no storage location."""
    original = broker.answer

    def odd_layout(op, body):
        status, payload = original(op, body)
        if op == "put":
            payload["key"] = f"v2/Evidence/{body['sha256']}"
            payload["uri"] = f"s3://somewhere/{payload['key']}"  # ignored
            payload["bucket"] = "somewhere"  # ignored
        return status, payload

    monkeypatch.setattr(broker, "answer", odd_layout)
    code, out, _ = _run(
        ["artifact", "put", str(report), "--repo", REPO, "--format", "json"], capsys
    )

    assert code == 0
    sha256 = _sha(report.read_bytes())
    entry = json.loads(out)["entries"][0]
    assert entry["uri"] == f"artifact:v2/Evidence/{sha256}"
    assert "somewhere" not in out


def test_put_refuses_a_key_that_cannot_round_trip(broker, report, monkeypatch, capsys):
    original = broker.answer

    def spaced(op, body):
        status, payload = original(op, body)
        payload["key"] = f"evidence/a b/{body['sha256']}"
        return status, payload

    monkeypatch.setattr(broker, "answer", spaced)
    code, _, err = _run(
        ["artifact", "put", str(report), "--repo", REPO, "--format", "json"], capsys
    )

    assert code == 1
    assert json.loads(err)["error"]["code"] == "broker_bad_response"
    assert broker.objects == {}  # refused before any upload


def test_bucket_option_is_gone(broker, report, capsys):
    assert not hasattr(store, "ENV_BUCKET")
    with pytest.raises(SystemExit) as excinfo:
        main(["artifact", "put", str(report), "--repo", REPO, "--bucket", "b"])
    assert excinfo.value.code == 2
    capsys.readouterr()


@pytest.mark.parametrize(
    "uri",
    [
        "s3://some-bucket/{key}",
        "https://example.invalid/{key}",
        "artifact://{key}",
        "Artifact:{key}",
        "{key}",
        "artifact:",
    ],
)
@pytest.mark.parametrize("command", ["get", "verify"])
def test_get_and_verify_refuse_other_uri_schemes(
    broker, report, tmp_path, capsys, uri, command
):
    manifest = tmp_path / "m.json"
    _put(report, manifest, capsys)
    data = json.loads(manifest.read_text())
    key = _key(REPO, _sha(report.read_bytes()))
    data["entries"][0]["uri"] = uri.format(key=key)
    manifest.write_text(json.dumps(data))
    fetches = len(broker.requests)

    argv = ["artifact", command, str(manifest), "--format", "json"]
    if command == "get":
        argv[3:3] = ["-o", str(tmp_path / "x")]
    code, _, err = _run(argv, capsys)

    assert code == 1
    assert json.loads(err)["error"]["code"] == "invalid_manifest"
    assert len(broker.requests) == fetches  # refused before asking the broker
    assert not (tmp_path / "x").exists()


def _move_manifest_key(manifest: Path, sha256: str) -> None:
    """Point the entry's uri at another key that still names the same digest
    and repo, so only the uri-key == broker-key check can catch it."""
    data = json.loads(manifest.read_text())
    data["entries"][0]["uri"] = f"artifact:evidence/acme__widgets/zz/{sha256}"
    manifest.write_text(json.dumps(data))


def test_get_refuses_a_broker_key_other_than_the_uri_key(
    broker, report, tmp_path, capsys
):
    manifest = tmp_path / "m.json"
    _put(report, manifest, capsys)
    _move_manifest_key(manifest, _sha(report.read_bytes()))

    code, _, err = _run(
        [
            "artifact",
            "get",
            str(manifest),
            "-o",
            str(tmp_path / "x"),
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == 1
    assert json.loads(err)["error"]["code"] == "key_mismatch"
    assert not (tmp_path / "x").exists()


def test_verify_reports_a_broker_key_other_than_the_uri_key(
    broker, report, tmp_path, capsys
):
    manifest = tmp_path / "m.json"
    _put(report, manifest, capsys)
    _move_manifest_key(manifest, _sha(report.read_bytes()))

    code, out, _ = _run(
        ["artifact", "verify", str(manifest), "--format", "json"], capsys
    )

    assert code == 3
    assert json.loads(out)["entries"][0]["status"] == "key_mismatch"


def test_entry_problems_rejects_extra_and_malformed_fields():
    sha256 = "a" * 64
    good = store.build_entry(
        sha256=sha256,
        size=3,
        kind="evidence",
        producer={"tool": "klt", "version": "0", "loom.repo": REPO, "commit": None},
        uri=f"artifact:{_key(REPO, sha256)}",
    )
    assert store.entry_problems(good) == []
    assert store.entry_problems({**good, "url": "x"}) == ["unknown field(s) url"]
    assert store.entry_problems({**good, "uri": f"artifact:evidence/x/{'b' * 64}"}) == [
        "uri key does not end in the entry's sha256"
    ]
    assert store.entry_problems(
        {**good, "uri": f"s3://some-bucket/{_key(REPO, sha256)}"}
    ) == [f"uri 's3://some-bucket/{_key(REPO, sha256)}' is not artifact:<key>"]
    assert "kind must be one of" in store.entry_problems({**good, "kind": "blob"})[0]


def test_repo_with_double_underscore_is_refused():
    with pytest.raises(store.ArtifactError):
        store.check_repo("acme/wid__gets")


def test_signoff_cites_an_artifact_through_a_command_backed_entry(
    broker, tmp_path, capsys
):
    """The existing ``klt signoff --manifest`` citation path (#2342's
    ``pointer`` included) grades bytes fetched by ``klt artifact get -o -``:
    no signoff change, and the bytes are hash-verified before signoff sees
    them."""
    composite = tmp_path / "composed.json"
    drc = json.loads((REPO_ROOT / "examples" / "signoff" / "drc.json").read_text())
    composite.write_text(
        json.dumps({"drc": drc, "new_violations_from_composition": []})
    )
    manifest = tmp_path / "artifacts.manifest.json"
    assert _put(composite, manifest, capsys)[0] == 0

    spec = signoff._normalize_evidence_entry(
        {
            "command": [
                sys.executable,
                "-m",
                "klayout_tools.cli",
                "artifact",
                "get",
                f"{manifest}#composed.json",
                "-o",
                "-",
            ],
            "pointer": "/drc",
        }
    )
    resolution, reason = signoff._resolve_evidence(spec)

    assert reason is None
    assert resolution["kind"] == "drc"
    assert resolution["envelope"] == drc
    assert resolution["exit_status"] == 0

    broker.corrupt = True
    resolution, reason = signoff._resolve_evidence(spec)
    assert resolution is None
    assert reason == "command_failed"  # unverified bytes never reach the grader


def test_no_presigned_url_in_argv_or_environment_of_children(
    broker, report, tmp_path, capsys, monkeypatch
):
    """The client spawns no process for a transfer (only `git` for producer
    metadata, which is given no URL)."""
    spawned: list[list[str]] = []
    real_run = store.subprocess.run

    def spy(argv, *args, **kwargs):
        spawned.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(store.subprocess, "run", spy)
    _put(report, tmp_path / "m.json", capsys)
    _run(
        ["artifact", "get", str(tmp_path / "m.json"), "-o", str(tmp_path / "o")], capsys
    )

    assert all(argv[0] == "git" for argv in spawned)
    assert not any("fake0secret" in part for argv in spawned for part in argv)
    assert "fake0secret" not in json.dumps(dict(os.environ))
