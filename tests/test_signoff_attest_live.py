"""Live-only `hint` on a failing record-check (issue #5538; split from
test_signoff_attest_item8.py for the file-size ratchet).

A failing `command` check that re-verifies a tier-verdict record gets a
live-only `hint` naming the record/envelope cycle and the way out. Same
lifecycle as `output_tail`: never in the written envelope, never in a
`--check` diff, never built by an attestation builder.
"""

from __future__ import annotations

import sys

import pytest

from test_signoff_attest_item8 import _PASS, _analog_inventory, _attest, _repo

# A failing command carrying the record-check argv shape. The trailing
# arguments land in the child's `sys.argv` and are never acted on -- the
# detection is on argv alone, so the cycle does not have to be built here
# (`tests/test_signoff_attest_record_cycle.py` builds the real one).
_RECORD_CHECK = [
    sys.executable,
    "-c",
    "raise SystemExit(3)",
    "signoff",
    "--manifest",
    "manifest.json",
    "--check",
    "record.json",
]


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(
            ["klt", "signoff", "--manifest", "m.json", "--check", "r.json"], id="bare"
        ),
        pytest.param(
            ["klt", "signoff", "--manifest=m.json", "--check=r.json"],
            id="equals-spelling",
        ),
        pytest.param(
            ["klt", "signoff", "--manifest", "m.json", "--check=r.json"],
            id="mixed-spelling",
        ),
        pytest.param(
            [
                "uvx",
                "--from",
                "klayout-tools==0.9.0",
                "klt",
                "signoff",
                "--tiers-doc",
                "t.md",
                "--manifest",
                "m.json",
                "--check",
                "r.json",
            ],
            id="uvx-wrapper",
        ),
        pytest.param(
            [
                sys.executable,
                "-m",
                "klayout_tools.cli",
                "signoff",
                "--manifest",
                "m.json",
                "--check",
                "r.json",
            ],
            id="python-m-wrapper",
        ),
    ],
)
def test_record_check_argv_shapes_are_recognised(argv):
    from klayout_tools.signoff_attest_live import is_record_check_argv

    assert is_record_check_argv(argv)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["klt", "signoff", "--manifest", "m.json"], id="no-check"),
        pytest.param(
            [
                "klt",
                "signoff",
                "--attest",
                "8",
                "--inventory",
                "i.json",
                "--check",
                "e.json",
            ],
            id="attest-check-not-a-record",
        ),
        pytest.param(
            ["make", "--manifest", "m.json", "--check", "r.json"], id="not-signoff"
        ),
        pytest.param(
            ["klt", "signoffs", "--manifest", "m.json", "--check", "r.json"],
            id="substring-is-not-the-token",
        ),
        pytest.param(
            ["klt", "signoff", "--manifest-of", "m", "--checker", "r"],
            id="flag-prefixes-are-not-the-flags",
        ),
        pytest.param([], id="empty"),
    ],
)
def test_other_argv_shapes_are_not_record_checks(argv):
    from klayout_tools.signoff_attest_live import is_record_check_argv

    assert not is_record_check_argv(argv)


def test_record_check_hint_is_live_only_and_only_on_the_failing_record_check(tmp_path):
    from klayout_tools.signoff_attest_live import (
        RECORD_CHECK_HINT,
        attach_record_check_hints,
    )

    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    # One failing record check, one failing command that is not one, one pass.
    inventory["envelope_checks"]["evidence/sim_ss.json"] = {"argv": _RECORD_CHECK}
    inventory["envelope_checks"]["evidence/sim_tt.json"] = _PASS
    inventory["report"] = "report.md"
    envelope = _attest(repo, inventory)
    checks = envelope["attestation"]["checks"]
    assert all("hint" not in c for c in checks)  # never built into the envelope

    attach_record_check_hints(envelope)

    hinted = [c for c in checks if "hint" in c]
    assert [c["target"] for c in hinted] == ["evidence/sim_ss.json"]
    assert hinted[0]["passed"] is False
    assert hinted[0]["hint"] == RECORD_CHECK_HINT
    assert "regenerate the record" in RECORD_CHECK_HINT
    assert "docs/cli/signoff.md" in RECORD_CHECK_HINT


def test_no_hint_on_a_passing_record_check_or_an_unrelated_failure(tmp_path):
    from klayout_tools.signoff_attest_live import attach_record_check_hints

    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    # A *passing* command with the record-check shape, and a failing command
    # without it: neither is advised.
    inventory["envelope_checks"]["evidence/sim_tt.json"] = {
        "argv": [sys.executable, "-c", "pass", "signoff", "--manifest", "--check"]
    }
    inventory["envelope_checks"]["evidence/sim_ss.json"] = {
        "argv": [sys.executable, "-c", "raise SystemExit(2)"]
    }
    envelope = _attest(repo, inventory)

    attach_record_check_hints(envelope)

    assert all("hint" not in c for c in envelope["attestation"]["checks"])


def test_carried_local_only_record_check_gets_no_hint(tmp_path):
    from klayout_tools.signoff_attest_live import attach_record_check_hints

    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["envelope_checks"]["evidence/sim_ss.json"] = {"argv": _RECORD_CHECK}
    envelope = _attest(repo, inventory)
    carried = next(
        c for c in envelope["attestation"]["checks"] if c.get("argv") == _RECORD_CHECK
    )
    # The `--skip-local-only` shape: the check was not run this time, its
    # failing outcome was carried from the committed envelope.
    result = {
        **envelope,
        "skipped_checks": [
            {k: carried[k] for k in ("role", "target", "argv", "cwd", "passed")}
        ],
    }

    attach_record_check_hints(result)

    assert all("hint" not in c for c in result["attestation"]["checks"])


def test_text_line_prints_the_hint_under_the_fail_line():
    from klayout_tools.signoff_attest_live import RECORD_CHECK_HINT
    from klayout_tools.signoff_attest_text import attestation_check_line

    check = {
        "check": "command",
        "role": "audit",
        "name": "record-fresh",
        "argv": ["klt", "signoff", "--manifest", "m.json", "--check", "r.json"],
        "cwd": ".",
        "exit_status": 3,
        "passed": False,
        "hint": RECORD_CHECK_HINT,
    }

    lines = attestation_check_line(check).splitlines()

    assert lines[0].startswith("[FAIL] audit record-fresh: ")
    assert lines[-1] == f"    hint: {RECORD_CHECK_HINT}"
    passing = {**check, "passed": True, "exit_status": 0}
    assert "hint" not in attestation_check_line(passing)
