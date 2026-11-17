"""End-to-end committed-summary walk for ``klt artifact verify --local``
(issue #5591). A chip records a Monte Carlo run: the per-sample bytes go to
the artifact store, a small summary carrying exactly the published statistics
is stored into the same manifest **and committed to git**, and the report
check re-establishes the record's numbers in a clean checkout — no broker
configured, no credential, no network.

The record-time half writes real bytes once (so the manifest entries carry
real digests) and then removes the samples from the checkout, the way the
store holds them outside git. The check half runs with ``KLT_ARTIFACT_BROKER``
unset and :class:`BrokerClient` poisoned, so any broker contact fails loudly
instead of passing silently.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from klayout_tools import artifact_objects as store
from klayout_tools import artifact_verify as av
from klayout_tools.cli import main

REPO = "acme/widgets"
SUMMARY_NAME = "sim/psrr-mc/summary.json"
SAMPLES_NAME = "sim/psrr-mc/corners/r1/samples.csv"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def no_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clean-checkout condition: no broker URL, and a BrokerClient that
    fails the test if the command ever tries to build one."""

    class ForbiddenBroker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("the check constructed a BrokerClient")

    monkeypatch.delenv(store.ENV_BROKER, raising=False)
    monkeypatch.setattr(av, "BrokerClient", ForbiddenBroker)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tiny chip checkout: manifest and summary committed, samples
    store-only (deleted here), a record publishing one summary figure."""
    for var, value in (
        ("GIT_AUTHOR_NAME", "t"),
        ("GIT_AUTHOR_EMAIL", "t@example.com"),
        ("GIT_COMMITTER_NAME", "t"),
        ("GIT_COMMITTER_EMAIL", "t@example.com"),
    ):
        monkeypatch.setenv(var, value)
    root = tmp_path / "chip"
    (root / "manifests" / "artifacts").mkdir(parents=True)
    (root / "sim" / "psrr-mc" / "corners" / "r1").mkdir(parents=True)
    (root / "sim" / "psrr-mc" / "records").mkdir(parents=True)

    samples = "".join(f"{i},0.{i % 97:02d}\n" for i in range(4000)).encode()
    summary = (
        json.dumps(
            {"kind": "psrr-mc", "worst_psrr_db": -86.4, "grid_points": 10},
            indent=2,
        )
        + "\n"
    ).encode()
    record = (
        b"# PSRR Monte Carlo record 20261108-220319\n\n"
        b"Published: worst_psrr_db: -86.4 over 10 grid points.\n"
    )
    (root / SAMPLES_NAME).write_bytes(samples)
    (root / SUMMARY_NAME).write_bytes(summary)
    (root / "sim/psrr-mc/records/psrr-mc.md").write_bytes(record)
    manifest = root / "manifests" / "artifacts" / "psrr-mc.manifest.json"
    producer = {
        "tool": "chip-ci",
        "version": "1.0",
        "loom.repo": REPO,
        "commit": None,
    }

    def entry_for(path: Path, name: str) -> dict[str, Any]:
        sha, size = store.hash_file(str(path))
        return store.build_entry(
            sha256=sha,
            size=size,
            kind="evidence",
            producer=producer,
            uri=f"artifact:evidence/{REPO.replace('/', '__')}/{sha[:2]}/{sha}",
            name=name,
        )

    store.add_entries(str(manifest), [entry_for(root / SUMMARY_NAME, SUMMARY_NAME)])
    _git(root, "init", "-q", "-b", "main")
    _git(
        root,
        "add",
        "manifests/artifacts/psrr-mc.manifest.json",
        SUMMARY_NAME,
        "sim/psrr-mc/records/psrr-mc.md",
    )
    _git(root, "commit", "-q", "-m", "record psrr-mc with committed summary")

    # The samples move to the store: the bytes leave git, the entry stays.
    store.add_entries(str(manifest), [entry_for(root / SAMPLES_NAME, SAMPLES_NAME)])
    (root / SAMPLES_NAME).unlink()
    _git(root, "add", "manifests/artifacts/psrr-mc.manifest.json")
    _git(root, "commit", "-q", "-m", "samples move to the artifact store")
    monkeypatch.chdir(root)
    return root


def report_check(root: Path, capsys: pytest.CaptureFixture[str]) -> int:
    """What a chip's ``--check`` does: verify the committed summary is the
    one the manifest binds, then compare the record's published figure."""
    manifest = root / "manifests" / "artifacts" / "psrr-mc.manifest.json"
    code = main(
        [
            "artifact",
            "verify",
            "--local",
            f"{manifest}#{SUMMARY_NAME}",
            "--format",
            "json",
        ]
    )
    if code != 0:
        return code
    _stdout, _ = capsys.readouterr()
    summary = json.loads((root / SUMMARY_NAME).read_bytes())
    record = (root / "sim/psrr-mc/records/psrr-mc.md").read_text()
    published = f"worst_psrr_db: {summary['worst_psrr_db']}"
    return 0 if published in record else 1


def test_report_check_passes_in_a_clean_checkout(
    checkout: Path, capsys: pytest.CaptureFixture[str]
):
    assert report_check(checkout, capsys) == 0


def test_absent_samples_are_reported_but_not_failures(
    checkout: Path, capsys: pytest.CaptureFixture[str]
):
    manifest = checkout / "manifests" / "artifacts" / "psrr-mc.manifest.json"
    code = main(["artifact", "verify", "--local", str(manifest), "--format", "json"])
    out, _ = capsys.readouterr()
    payload = json.loads(out)
    assert code == 0
    assert payload["mode"] == "local"
    statuses = {e["name"]: e["status"] for e in payload["entries"]}
    assert statuses[SAMPLES_NAME] == "absent"
    assert statuses[SUMMARY_NAME] == "ok"


def test_tampered_summary_fails_the_check(
    checkout: Path, capsys: pytest.CaptureFixture[str]
):
    summary = checkout / SUMMARY_NAME
    summary.write_bytes(summary.read_bytes().replace(b"-86.4", b"-96.4"))
    assert report_check(checkout, capsys) == 3
    _stdout, _ = capsys.readouterr()


def test_summary_manifest_mismatch_fails_the_check(
    checkout: Path, capsys: pytest.CaptureFixture[str]
):
    manifest = checkout / "manifests" / "artifacts" / "psrr-mc.manifest.json"
    data = json.loads(manifest.read_text())
    for entry in data["entries"]:
        if entry.get("name") == SUMMARY_NAME:
            entry["size"] += 1
    manifest.write_text(json.dumps(data))
    assert report_check(checkout, capsys) == 3
    _stdout, _ = capsys.readouterr()


def test_nothing_present_is_never_a_pass(
    checkout: Path, capsys: pytest.CaptureFixture[str]
):
    (checkout / SUMMARY_NAME).unlink()
    manifest = checkout / "manifests" / "artifacts" / "psrr-mc.manifest.json"
    code = main(["artifact", "verify", "--local", str(manifest), "--format", "json"])
    _stdout, err = capsys.readouterr()
    assert code == 1
    assert json.loads(err)["error"]["code"] == "nothing_verified"
