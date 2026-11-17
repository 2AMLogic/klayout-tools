"""Tests for ``klt signoff --manifest/--fleet --require-grading-ruleset``
(issue #5847).

The flag pins the grading build: the command refuses (exit ``1``, error
envelope on stderr, nothing on stdout) unless the running build's
``grading_ruleset_id`` equals the given id, checked before the manifest is
read or any evidence command runs. These tests live here rather than in
``tests/test_signoff.py`` (ratchet-baselined), mirroring that file's
hand-built-fixture convention: a minimal ``klt drc`` envelope, no external
corpus.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.build_identity import grading_ruleset_id
from klayout_tools.cli import main

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_DRC_CLEAN_ENVELOPE = {
    "schema_version": 1,
    "file": "design.gds",
    "deck": "sky130",
    "dbu_um": 0.001,
    "status": "clean",
    "violation_count": 0,
    "rule_counts": {},
    "violations": [],
    "coverage": {
        "deck_layers": ["65/20"],
        "layers_checked": ["65/20"],
        "layers_in_stream_without_rules": [],
        "rules_skipped": [],
    },
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": None,
        "deck": {"name": "sky130", "content_hash": "sha256:drcdeck"},
        "input": {"content_hash": "sha256:layoutA"},
    },
}


def _write(tmp_path, name: str, payload) -> str:
    path = tmp_path / name
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload))
    else:
        path.write_text(payload)
    return str(path)


def _manifest(drc_path: str) -> dict:
    return {
        "block": "demo-block",
        "kind": "analog",
        "evidence": {"3": drc_path},
    }


def _fleet_manifest(drc_path: str) -> dict:
    return {"blocks": [_manifest(drc_path)]}


def _run(argv):
    """Run the CLI in-process; return ``(exit_code, stdout, stderr)``."""
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _error_message(stderr: str) -> dict:
    """The documented JSON error envelope (docs/json-contract.md)."""
    payload = json.loads(stderr)
    assert payload["schema_version"] == 1
    assert payload["error"]["command"] == "signoff"
    return payload["error"]["message"]


def _mismatched_id() -> str:
    """A well-shaped id that is not this build's (flip the last hex digit)."""
    gid = grading_ruleset_id()
    last = gid[-1]
    return gid[:-1] + ("0" if last != "0" else "1")


# --------------------------------------------------------------------------- #
# Render mode: match behaves exactly as without the flag
# --------------------------------------------------------------------------- #


def test_render_with_running_build_id_is_byte_identical(tmp_path):
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    manifest_path = _write(tmp_path, "manifest.json", _manifest(drc_path))

    rc_plain, out_plain, _ = _run(
        ["signoff", "--manifest", manifest_path, "--format", "json"]
    )
    rc_pinned, out_pinned, _ = _run(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--format",
            "json",
            "--require-grading-ruleset",
            grading_ruleset_id(),
        ]
    )

    assert rc_pinned == rc_plain == 3  # a T1 subset is met, verdict unchanged
    assert out_pinned == out_plain  # byte-identical report


def test_render_accepts_uppercase_hex_id(tmp_path):
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    manifest_path = _write(tmp_path, "manifest.json", _manifest(drc_path))

    rc_plain, out_plain, _ = _run(
        ["signoff", "--manifest", manifest_path, "--format", "json"]
    )
    rc_pinned, out_pinned, _ = _run(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--format",
            "json",
            "--require-grading-ruleset",
            grading_ruleset_id().upper(),
        ]
    )

    assert rc_pinned == rc_plain
    assert out_pinned == out_plain


# --------------------------------------------------------------------------- #
# Render mode: mismatch refuses before anything runs
# --------------------------------------------------------------------------- #


def test_mismatched_id_refuses_with_zero_stdout(tmp_path):
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    manifest_path = _write(tmp_path, "manifest.json", _manifest(drc_path))

    rc, out, err = _run(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--format",
            "json",
            "--require-grading-ruleset",
            _mismatched_id(),
        ]
    )

    assert rc == 1
    assert out == ""  # zero bytes on stdout: nothing to truncate a record with
    message = _error_message(err)
    assert grading_ruleset_id() in message
    assert _mismatched_id() in message


def test_text_format_mismatch_also_goes_to_stderr_only(tmp_path):
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    manifest_path = _write(tmp_path, "manifest.json", _manifest(drc_path))

    rc, out, err = _run(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--require-grading-ruleset",
            _mismatched_id(),
        ]
    )

    assert rc == 1
    assert out == ""
    assert err.startswith("klt signoff: ")
    assert "--require-grading-ruleset" in err


def test_mismatch_refuses_before_reading_the_manifest(tmp_path):
    # The evidence file the manifest cites does not exist: if the guard
    # ran after manifest/evidence processing, the failure would be about
    # the missing file, not the ruleset.
    manifest_path = _write(
        tmp_path, "manifest.json", _manifest(str(tmp_path / "absent.json"))
    )

    rc, out, err = _run(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--format",
            "json",
            "--require-grading-ruleset",
            _mismatched_id(),
        ]
    )

    assert rc == 1
    assert out == ""
    message = _error_message(err)
    assert "--require-grading-ruleset" in message
    assert "absent.json" not in message


def test_mismatch_never_touches_the_committed_output_file(tmp_path):
    # `-o FILE` writes only after a successful render; a refused render
    # must leave a previously committed verdict of record untouched.
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    manifest_path = _write(tmp_path, "manifest.json", _manifest(drc_path))
    committed = tmp_path / "tier-report.json"
    committed.write_text('{"previously": "committed"}')

    rc, out, _ = _run(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "-o",
            str(committed),
            "--format",
            "json",
            "--require-grading-ruleset",
            _mismatched_id(),
        ]
    )

    assert rc == 1
    assert out == ""
    assert committed.read_text() == '{"previously": "committed"}'


def test_a_build_that_cannot_compute_its_id_refuses(tmp_path, monkeypatch):
    from klayout_tools import signoff_require_build

    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    manifest_path = _write(tmp_path, "manifest.json", _manifest(drc_path))
    monkeypatch.setattr(signoff_require_build, "grading_ruleset_id", lambda: None)

    rc, out, err = _run(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--format",
            "json",
            "--require-grading-ruleset",
            "sha256:" + "a" * 64,
        ]
    )

    assert rc == 1
    assert out == ""
    assert "cannot compute its own grading_ruleset_id" in _error_message(err)


# --------------------------------------------------------------------------- #
# Malformed values name the expected shape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "deadbeef",  # no sha256: prefix
        "md5:" + "a" * 64,  # wrong hash name
        "sha256:" + "a" * 63,  # one hex digit short
        "sha256:" + "a" * 65,  # one hex digit long
        "sha256:" + "g" * 64,  # not hex
        "sha256:",  # empty digest
    ],
)
def test_malformed_value_refuses_naming_the_expected_shape(tmp_path, value):
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    manifest_path = _write(tmp_path, "manifest.json", _manifest(drc_path))

    rc, out, err = _run(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--format",
            "json",
            "--require-grading-ruleset",
            value,
        ]
    )

    assert rc == 1
    assert out == ""
    message = _error_message(err)
    assert "'sha256:'" in message
    assert "64 hex digits" in message


# --------------------------------------------------------------------------- #
# The same flag on --fleet and --check
# --------------------------------------------------------------------------- #


def test_fleet_render_with_running_build_id_is_byte_identical(tmp_path):
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    fleet_path = _write(tmp_path, "fleet.json", _fleet_manifest(drc_path))

    rc_plain, out_plain, _ = _run(
        ["signoff", "--fleet", fleet_path, "--format", "json"]
    )
    rc_pinned, out_pinned, _ = _run(
        [
            "signoff",
            "--fleet",
            fleet_path,
            "--format",
            "json",
            "--require-grading-ruleset",
            grading_ruleset_id(),
        ]
    )

    assert rc_pinned == rc_plain == 3
    assert out_pinned == out_plain


def test_fleet_render_with_mismatched_id_refuses(tmp_path):
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    fleet_path = _write(tmp_path, "fleet.json", _fleet_manifest(drc_path))

    rc, out, err = _run(
        [
            "signoff",
            "--fleet",
            fleet_path,
            "--format",
            "json",
            "--require-grading-ruleset",
            _mismatched_id(),
        ]
    )

    assert rc == 1
    assert out == ""
    assert "--require-grading-ruleset" in _error_message(err)


def test_check_with_running_build_id_still_reproduces(tmp_path):
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    manifest_path = _write(tmp_path, "manifest.json", _manifest(drc_path))
    _, report, _ = _run(["signoff", "--manifest", manifest_path, "--format", "json"])
    committed_path = _write(tmp_path, "committed.json", report)

    rc, out, err = _run(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--check",
            committed_path,
            "--format",
            "json",
            "--require-grading-ruleset",
            grading_ruleset_id(),
        ]
    )

    assert rc == 0  # --check's own contract: 0 = the committed report holds
    assert json.loads(out)["status"] == "match"


def test_check_with_mismatched_id_refuses_before_reading_it(tmp_path):
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)
    manifest_path = _write(tmp_path, "manifest.json", _manifest(drc_path))

    rc, out, err = _run(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--check",
            str(tmp_path / "absent.json"),
            "--format",
            "json",
            "--require-grading-ruleset",
            _mismatched_id(),
        ]
    )

    assert rc == 1
    assert out == ""
    message = _error_message(err)
    assert "--require-grading-ruleset" in message
    assert "absent.json" not in message


# --------------------------------------------------------------------------- #
# Refused in the modes that produce no grade to pin
# --------------------------------------------------------------------------- #


def test_refused_with_describe_grader():
    rc, out, err = _run(
        [
            "signoff",
            "--describe-grader",
            "--format",
            "json",
            "--require-grading-ruleset",
            grading_ruleset_id(),
        ]
    )

    assert rc == 1
    assert out == ""
    assert "only applies to --manifest/--fleet" in _error_message(err)


def test_refused_with_envelope_aggregation(tmp_path):
    drc_path = _write(tmp_path, "drc.json", _DRC_CLEAN_ENVELOPE)

    rc, out, err = _run(
        [
            "signoff",
            drc_path,
            "--format",
            "json",
            "--require-grading-ruleset",
            grading_ruleset_id(),
        ]
    )

    assert rc == 1
    assert out == ""
    assert "only applies to --manifest/--fleet" in _error_message(err)


def test_refused_with_attest_even_before_the_inventory_is_read(tmp_path):
    # The inventory does not exist: the mode refusal must win, proving the
    # guard fires before --attest dispatch reads anything.
    rc, out, err = _run(
        [
            "signoff",
            "--attest",
            "1",
            "--inventory",
            str(tmp_path / "absent.json"),
            "--format",
            "json",
            "--require-grading-ruleset",
            grading_ruleset_id(),
        ]
    )

    assert rc == 1
    assert out == ""
    assert "only applies to --manifest/--fleet" in _error_message(err)


def test_flag_is_registered_on_the_parser(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["signoff", "--help"])
    assert excinfo.value.code == 0
    assert "--require-grading-ruleset" in capsys.readouterr().out
