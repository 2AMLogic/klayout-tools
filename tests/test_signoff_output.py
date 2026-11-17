"""``klt signoff --manifest/--fleet -o/--output``: committing the render's
verdict of record (issue #5867).

Split from ``test_signoff``, which sits at its file-size-ratchet ceiling;
the shared fixtures and manifest builders live there."""

from __future__ import annotations

import json
import os

import pytest

import test_signoff as base
from klayout_tools.cli import main

_write = base._write
_manifest = base._manifest
_full_t1_evidence = base._full_t1_evidence
_fleet_write = base._fleet_write
_fleet_block_manifest = base._fleet_block_manifest
DRC_CLEAN_ENVELOPE = base.DRC_CLEAN_ENVELOPE

# --------------------------------------------------------------------------- #
# CLI (`--manifest`/`--fleet` with `-o/--output`, issue #5867)
# --------------------------------------------------------------------------- #


def test_cli_manifest_output_writes_the_json_envelope_on_pass(tmp_path, capsys):
    """AC: `--manifest M -o FILE` writes FILE with bytes identical to what
    stdout carries, and the exit code stays the verdict's own (0 here)."""
    manifest_path = _write(
        tmp_path, "manifest.json", _manifest(evidence=_full_t1_evidence(tmp_path))
    )
    output = tmp_path / "tier-report.json"

    exit_code = main(
        ["signoff", "--manifest", manifest_path, "-o", str(output), "--format", "json"]
    )

    assert exit_code == 0
    stdout = capsys.readouterr().out
    assert output.read_bytes() == stdout.encode("utf-8")
    assert json.loads(stdout)["tier"] == "T1"


def test_cli_manifest_output_writes_on_exit_3_below_t1(tmp_path, capsys):
    """AC: a below-T1 render is still a verdict of record -- exit 3 writes
    FILE (exactly what the `>` redirect this replaces committed), and
    stdout still carries the envelope unchanged."""
    manifest_path = _write(tmp_path, "manifest.json", _manifest())
    output = tmp_path / "tier-report.json"

    exit_code = main(
        ["signoff", "--manifest", manifest_path, "-o", str(output), "--format", "json"]
    )

    assert exit_code == 3
    assert json.loads(output.read_text())["tier"] is None
    assert json.loads(capsys.readouterr().out)["tier"] is None


def test_cli_manifest_output_writes_json_even_under_text_format(tmp_path, capsys):
    """AC: the file is a machine record -- it always receives the JSON
    envelope, whatever `--format` says."""
    manifest_path = _write(tmp_path, "manifest.json", _manifest())
    output = tmp_path / "tier-report.json"

    exit_code = main(["signoff", "--manifest", manifest_path, "-o", str(output)])

    assert exit_code == 3
    assert "UNMET" in capsys.readouterr().out  # text rendering on stdout
    assert json.loads(output.read_text())["schema_version"] == 1


def test_cli_manifest_output_error_leaves_the_committed_file_untouched(
    tmp_path, capsys
):
    """AC: a failed render (error envelope, exit 1) never replaces a
    pre-existing good record -- the guarantee the `>` redirect broke."""
    manifest_path = _write(tmp_path, "manifest.json", _manifest())
    output = tmp_path / "tier-report.json"
    assert main(["signoff", "--manifest", manifest_path, "-o", str(output)]) == 3
    good = output.read_bytes()
    os.utime(output, (1_000_000_000, 1_000_000_000))

    broken = _write(tmp_path, "broken.json", ["not", "a", "manifest"])
    exit_code = main(
        ["signoff", "--manifest", broken, "-o", str(output), "--format", "json"]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().err)["error"]["command"] == "signoff"
    assert output.read_bytes() == good
    assert output.stat().st_mtime == 1_000_000_000


def test_cli_manifest_output_is_not_rewritten_when_unchanged(tmp_path, capsys):
    """AC: an unchanged render leaves the committed file untouched (mtime
    included), mirroring `write_attestation`; a not-yet-existing output
    directory is created on the first write."""
    manifest_path = _write(tmp_path, "manifest.json", _manifest())
    output = tmp_path / "signoff" / "records" / "tier-report.json"

    assert main(["signoff", "--manifest", manifest_path, "-o", str(output)]) == 3
    assert output.parent.is_dir()
    os.utime(output, (1_000_000_000, 1_000_000_000))

    assert main(["signoff", "--manifest", manifest_path, "-o", str(output)]) == 3
    assert output.stat().st_mtime == 1_000_000_000

    # A changed render moves the committed file again.
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    changed = _write(tmp_path, "manifest2.json", _manifest(evidence={"3": drc_path}))
    assert main(["signoff", "--manifest", changed, "-o", str(output)]) == 3
    assert output.stat().st_mtime != 1_000_000_000
    capsys.readouterr()


@pytest.mark.parametrize(
    "argv",
    [
        # `-o` with `--check`: --check verifies a committed report, never writes.
        [
            "signoff",
            "--manifest",
            "m.json",
            "--check",
            "committed.json",
            "-o",
            "f.json",
        ],
        # `-o` outside --attest/--manifest/--fleet (bare / envelope aggregation /
        # --describe-grader).
        ["signoff", "-o", "f.json"],
        ["signoff", "envelope.json", "-o", "f.json"],
        ["signoff", "--describe-grader", "-o", "f.json"],
    ],
)
def test_cli_output_refused_outside_the_writing_modes(tmp_path, capsys, argv):
    """AC: `-o` stays refused with `--check` and with no
    `--attest`/`--manifest`/`--fleet` render behind it."""
    existing = tmp_path / "f.json"
    existing.write_text("keep me\n")

    exit_code = main([*argv, "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "signoff"
    assert existing.read_text() == "keep me\n"


def test_cli_fleet_output_writes_the_json_envelope(tmp_path, capsys):
    """AC: `--fleet` renders accept `-o` the same way -- one mechanism, not
    a new one."""
    fleet_path = _fleet_write(
        tmp_path,
        [
            _fleet_block_manifest("canary-a", evidence=_full_t1_evidence(tmp_path)),
            _fleet_block_manifest("canary-b", evidence={}),
        ],
    )
    output = tmp_path / "fleet-report.json"

    exit_code = main(
        ["signoff", "--fleet", fleet_path, "-o", str(output), "--format", "json"]
    )

    assert exit_code == 3  # canary-b is not T1 yet -- still a verdict of record
    stdout = capsys.readouterr().out
    assert output.read_bytes() == stdout.encode("utf-8")
    assert json.loads(stdout)["t1_count"] == 1
