"""The record/envelope cycle `klt signoff --attest` cannot break out of, and
the documented way out (issue #5538).

An item-10 inventory may declare, as one of its `checks[]`, a `klt signoff
--manifest M --check R` of the tier-verdict record `R` that cites the very
envelope the attestation mints. That citation is self-referential by
construction:

    record  --cites-->  envelope  --audit check-->  record

so when both sides go stale together, the single documented command cannot
regenerate the envelope: `--attest` writes nothing while a check fails, and
the record cannot be regraded until the envelope has been rewritten.

These tests build that cycle for real -- a repository, a manifest pinning the
envelope, a committed record, and an inventory whose audit check is a genuine
`klt signoff --manifest ... --check ...` subprocess -- then confirm the
deadlock, the hint that names it, and that the three-step sequence the hint
and `docs/cli/signoff.md` prescribe actually converges.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from klayout_tools.cli import main

_RECORD = "record.json"
_ENVELOPE = "attest10.json"
_MANIFEST = "manifest.json"
_INVENTORY = "inventory10.json"

#: The audit check the inventory declares: a real record re-check, run as a
#: subprocess exactly as a repo's own hygiene lint would be.
_RECORD_CHECK_ARGV = [
    sys.executable,
    "-m",
    "klayout_tools.cli",
    "signoff",
    "--manifest",
    _MANIFEST,
    "--check",
    _RECORD,
]


def _write_inventory(repo: Path, hygiene: list[str]) -> None:
    (repo / _INVENTORY).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "t1-attestation-inventory",
                "t1_item": 10,
                "hygiene": hygiene,
                "checks": [{"name": "record-fresh", "argv": _RECORD_CHECK_ARGV}],
            }
        )
    )


def _pin_manifest(repo: Path) -> None:
    """Pin the manifest's item-10 evidence to the envelope, by the inventory's
    content hash -- the `content_hash` `--attest` itself prints as the manifest
    entry to cite. This is the repo-side step (a `make` target, by hand) that
    refreshes the pin after the inventory changes."""
    digest = hashlib.sha256((repo / _INVENTORY).read_bytes()).hexdigest()
    (repo / _MANIFEST).write_text(
        json.dumps(
            {
                "block": "demo-block",
                "kind": "analog",
                "evidence": {
                    "10": {"file": _ENVELOPE, "content_hash": f"sha256:{digest}"}
                },
            }
        )
    )


def _mint_record(repo: Path, capsys) -> int:
    """Regenerate the tier-verdict record from the manifest. Exit `3` here is
    the ordinary "this block is not at T1 yet" verdict, not a failure."""
    capsys.readouterr()  # the record is this command's stdout, nothing earlier
    exit_code = main(["signoff", "--manifest", _MANIFEST, "--format", "json"])
    (repo / _RECORD).write_text(capsys.readouterr().out)
    return exit_code


def _attest(*extra: str) -> int:
    return main(["signoff", "--attest", "10", "--inventory", _INVENTORY, *extra])


def _mint_envelope() -> int:
    return _attest("-o", _ENVELOPE)


def _item10(repo: Path) -> dict:
    record = json.loads((repo / _RECORD).read_text())
    return next(item for item in record["items"] if item["id"] == 10)


@pytest.fixture
def converged(tmp_path, monkeypatch, capsys):
    """A repository whose record and envelope agree: both `--check` runs pass."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "hygiene.md").write_text("# hygiene\n")
    _write_inventory(repo, ["hygiene.md"])
    _pin_manifest(repo)
    monkeypatch.chdir(repo)

    _mint_record(repo, capsys)
    assert _mint_envelope() == 0
    _mint_record(repo, capsys)
    capsys.readouterr()

    assert _item10(repo)["status"] == "met"
    assert main(["signoff", "--manifest", _MANIFEST, "--check", _RECORD]) == 0
    assert _attest("--check", _ENVELOPE) == 0
    capsys.readouterr()
    return repo


def _go_stale(repo: Path) -> None:
    """Edit an inventoried file. Adding it to the inventory changes the
    inventory's hash, so the manifest pin is refreshed to match -- and the
    committed record, graded against the envelope that has not been minted
    yet, goes stale at the same moment the envelope does."""
    (repo / "NOTES.md").write_text("# notes\n")
    _write_inventory(repo, ["hygiene.md", "NOTES.md"])
    _pin_manifest(repo)


def test_the_single_documented_command_deadlocks_and_says_why(converged, capsys):
    repo = converged
    before = (repo / _ENVELOPE).read_bytes()
    _go_stale(repo)

    exit_code = _mint_envelope()

    out = capsys.readouterr().out
    assert exit_code == 3
    assert (repo / _ENVELOPE).read_bytes() == before  # nothing was written
    assert "[FAIL] audit record-fresh:" in out
    # Without the hint this reads like any other failing check.
    assert "hint: this check re-verifies a tier-verdict record" in out
    assert "(1) regenerate the record, (2) re-run this --attest, (3)" in out


def test_the_three_step_sequence_converges(converged, capsys):
    repo = converged
    _go_stale(repo)
    assert _mint_envelope() == 3
    capsys.readouterr()

    # (1) regenerate the record -- it grades the item unmet/stale_evidence,
    #     because the envelope it cites has not been rewritten yet.
    _mint_record(repo, capsys)
    half_converged = (repo / _RECORD).read_text()
    assert _item10(repo)["status"] == "unmet"
    assert _item10(repo)["reason"] == "stale_evidence"

    # (2) re-run --attest: the record check passes now, so the envelope is
    #     written.
    assert _mint_envelope() == 0
    capsys.readouterr()

    # (3) regenerate the record again: it now grades the item met.
    _mint_record(repo, capsys)
    assert _item10(repo)["status"] == "met"

    assert main(["signoff", "--manifest", _MANIFEST, "--check", _RECORD]) == 0
    assert _attest("--check", _ENVELOPE) == 0
    capsys.readouterr()

    # The step-1 record is NOT the converged one: committing the pair after
    # step 1 or 2 commits a half-converged pair, which both `--check` runs
    # (CI's existing gates) reject.
    assert half_converged != (repo / _RECORD).read_text()
    (repo / _RECORD).write_text(half_converged)
    assert main(["signoff", "--manifest", _MANIFEST, "--check", _RECORD]) == 3
    assert _attest("--check", _ENVELOPE) == 3
    capsys.readouterr()


def test_the_check_mode_of_the_deadlocked_tree_carries_no_hint(converged, capsys):
    """`--attest --check` mints nothing, so the advice does not apply there."""
    repo = converged
    _go_stale(repo)

    assert _attest("--check", _ENVELOPE, "--format", "json") == 3

    payload = json.loads(capsys.readouterr().out)
    assert "hint" not in json.dumps(payload)


def test_the_written_envelope_never_carries_the_hint(converged, capsys):
    repo = converged
    _go_stale(repo)
    assert _mint_envelope() == 3
    capsys.readouterr()
    _mint_record(repo, capsys)

    assert _mint_envelope() == 0

    payload = json.loads((repo / _ENVELOPE).read_text())
    assert "hint" not in json.dumps(payload)
    capsys.readouterr()
