"""``klt signoff --attest 8 --inventory FILE`` (issue #4311)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_attest import (
    AttestationError,
    attach_output_tails,
    build_attestation,
    check_attestation,
    render_envelope,
)

_PASS = {"argv": [sys.executable, "-c", "pass"]}
_FAIL = {"argv": [sys.executable, "-c", "raise SystemExit(1)"]}


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "evidence").mkdir()
    (repo / "report.md").write_text("# characterization\n")
    for name in ("sim_tt", "sim_ss"):
        (repo / "evidence" / f"{name}.json").write_text(
            json.dumps(
                {"status": "pass", "result": {"fmax": 100, "area": 5, "power": 2}}
            )
        )
    return repo


def _analog_inventory(**overrides) -> dict:
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 8,
        "report": "report.md",
        "spec_rows": ["gain", "bandwidth"],
        "citations": {
            "gain": ["evidence/sim_tt.json"],
            "bandwidth": ["evidence/sim_tt.json", "evidence/sim_ss.json"],
        },
        "envelope_checks": {
            "evidence/sim_tt.json": _PASS,
            "evidence/sim_ss.json": _PASS,
        },
    }
    inventory.update(overrides)
    return inventory


def _digital_inventory(**overrides) -> dict:
    metrics = {
        corner: {
            name: {"envelope": f"evidence/sim_{corner}.json", "field": f"result.{name}"}
            for name in ("fmax", "area", "power")
        }
        for corner in ("tt", "ss")
    }
    return _analog_inventory(corners=["tt", "ss"], metrics=metrics, **overrides)


def _attest(repo: Path, inventory: dict) -> dict:
    path = repo / "inventory.json"
    path.write_text(json.dumps(inventory))
    return build_attestation(8, str(path))


def _failed(envelope: dict) -> list[tuple]:
    return [
        (c["check"], c.get("row") or c.get("target") or c.get("metric") or c["role"])
        for c in envelope["attestation"]["checks"]
        if not c["passed"]
    ]


def _grade(repo: Path, envelope: dict) -> dict:
    env_path = repo / "evidence" / "characterization.json"
    env_path.write_text(render_envelope(envelope))
    manifest = {
        "block": "demo",
        "kind": "analog",
        "evidence": {
            "8": {
                "file": str(env_path),
                "content_hash": envelope["provenance"]["input"]["content_hash"],
            }
        },
    }
    report = build_tier_report(manifest)
    return next(i for i in report["items"] if i["id"] == 8)


def test_analog_pass_shape_and_grades_met_with_disclosure(tmp_path):
    repo = _repo(tmp_path)

    envelope = _attest(repo, _analog_inventory())

    assert envelope["status"] == "pass"
    assert envelope["kind"] == "generic"
    assert envelope["t1_item"] == 8
    assert envelope["attestation"]["hash_algorithm"] == "sha256-raw-bytes"
    item = _grade(repo, envelope)
    assert item["status"] == "met"
    disclosure = item["citation"]["attestation"]
    assert disclosure["tool_produced"] is True
    assert disclosure["producer"] == "klt signoff --attest"
    assert disclosure["check_count"] == envelope["attestation"]["check_count"]


def test_bare_generic_still_grades_with_tool_produced_false(tmp_path):
    repo = _repo(tmp_path)
    bare = {
        "schema_version": 1,
        "kind": "generic",
        "status": "pass",
        "summary": "hand written",
    }
    env_path = repo / "evidence" / "bare.json"
    env_path.write_text(json.dumps(bare))
    report = build_tier_report(
        {"block": "demo", "kind": "analog", "evidence": {"8": {"file": str(env_path)}}}
    )

    item = next(i for i in report["items"] if i["id"] == 8)

    assert item["status"] == "met"
    assert item["citation"]["attestation"] == {"tool_produced": False}


def test_spec_row_without_citation_fails(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory(spec_rows=["gain", "bandwidth", "psrr"])

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "fail"
    assert _failed(envelope) == [("citation", "psrr")]


def test_missing_cited_envelope_fails(tmp_path):
    repo = _repo(tmp_path)
    (repo / "evidence" / "sim_ss.json").unlink()

    envelope = _attest(repo, _analog_inventory())

    assert envelope["status"] == "fail"
    assert ("citation", "bandwidth") in _failed(envelope)


def test_failing_envelope_check_fails(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["envelope_checks"]["evidence/sim_ss.json"] = _FAIL

    envelope = _attest(repo, inventory)

    assert _failed(envelope) == [("command", "evidence/sim_ss.json")]


def test_digital_metrics_pass_and_are_recorded(tmp_path):
    repo = _repo(tmp_path)

    envelope = _attest(repo, _digital_inventory())

    assert envelope["status"] == "pass"
    metrics = [c for c in envelope["attestation"]["checks"] if c["check"] == "metric"]
    assert len(metrics) == 6
    assert {c["value"] for c in metrics} == {100, 5, 2}


def test_digital_missing_metric_for_one_corner_fails(tmp_path):
    repo = _repo(tmp_path)
    inventory = _digital_inventory()
    del inventory["metrics"]["ss"]["fmax"]

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "fail"
    assert _failed(envelope) == [("metric", "fmax")]


def test_digital_non_numeric_metric_field_fails(tmp_path):
    repo = _repo(tmp_path)
    inventory = _digital_inventory()
    inventory["metrics"]["tt"]["power"]["field"] = "status"

    envelope = _attest(repo, inventory)

    assert _failed(envelope) == [("metric", "power")]


def test_mixed_signal_style_inventory_with_duplicate_citation(tmp_path):
    repo = _repo(tmp_path)
    inventory = _digital_inventory()
    inventory["citations"]["gain"] = ["evidence/sim_tt.json", "evidence/sim_tt.json"]

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "pass"
    cited_once = [
        c for c in envelope["attestation"]["checks"] if c["role"] == "evidence"
    ]
    assert len(cited_once) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda inv: inv.pop("report"),
        lambda inv: inv.update(spec_rows=[]),
        lambda inv: inv["citations"].update(unknown=["evidence/sim_tt.json"]),
        lambda inv: inv.pop("envelope_checks"),
        lambda inv: inv["envelope_checks"].pop("evidence/sim_ss.json"),
        lambda inv: inv.update(corners=["tt"]),
        lambda inv: inv.update(typo=1),
        lambda inv: inv["citations"].update(gain=["../outside.json"]),
    ],
)
def test_malformed_inventory_is_refused(tmp_path, mutate):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    mutate(inventory)

    with pytest.raises(AttestationError):
        _attest(repo, inventory)


def test_check_reports_drift_on_cited_envelope_and_row_map(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    envelope = _attest(repo, inventory)
    committed = repo / "evidence" / "item8.json"
    committed.write_text(render_envelope(envelope))
    inv_path = str(repo / "inventory.json")

    clean = check_attestation(8, inv_path, str(committed))
    assert clean["status"] == "match"
    assert clean["fresh_status"] == "pass"

    (repo / "evidence" / "sim_tt.json").write_text(json.dumps({"status": "pass"}))
    drifted = check_attestation(8, inv_path, str(committed))
    assert drifted["status"] != "match"

    (repo / "evidence" / "sim_tt.json").write_text(
        json.dumps({"status": "pass", "result": {"fmax": 100, "area": 5, "power": 2}})
    )
    inventory["citations"]["gain"] = ["evidence/sim_ss.json"]
    (repo / "inventory.json").write_text(json.dumps(inventory))
    remapped = check_attestation(8, inv_path, str(committed))
    assert remapped["status"] != "match"


def test_report_cited_as_evidence_is_refused(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["citations"]["gain"] = ["report.md"]
    inventory["envelope_checks"]["report.md"] = _PASS

    with pytest.raises(AttestationError):
        _attest(repo, inventory)


def _failing_inventory(text: str) -> dict:
    inventory = _analog_inventory()
    inventory["envelope_checks"]["evidence/sim_ss.json"] = {
        "argv": [sys.executable, "-c", f"import sys; print({text!r}); sys.exit(2)"]
    }
    return inventory


def test_failing_command_tail_is_live_only_and_never_in_built_envelope(tmp_path):
    repo = _repo(tmp_path)
    envelope = _attest(repo, _failing_inventory("boom"))
    checks = envelope["attestation"]["checks"]
    assert all("output_tail" not in c for c in checks)

    attach_output_tails(envelope)

    failed = [c for c in checks if c["check"] == "command" and not c["passed"]]
    assert [c["output_tail"] for c in failed] == ["boom"]
    assert all(
        "output_tail" not in c
        for c in checks
        if c["check"] == "command" and c["passed"]
    )


def test_host_specific_tail_text_does_not_change_the_envelope_bytes(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["envelope_checks"]["evidence/sim_ss.json"] = {
        "argv": [
            sys.executable,
            "-c",
            "import os, sys; print(os.environ['TAIL_X']); sys.exit(2)",
        ]
    }
    rendered = []
    for host_text in ("host-a-text", "host-b-text"):
        monkeypatch.setenv("TAIL_X", host_text)
        envelope = _attest(repo, inventory)
        rendered.append(render_envelope(envelope))
        attach_output_tails(envelope)
        assert host_text in render_envelope(envelope)  # live result only

    assert rendered[0] == rendered[1]
    assert "host-" not in rendered[0]


def test_text_line_names_failing_citation_row(tmp_path):
    from klayout_tools.signoff_attest_text import attestation_check_line

    repo = _repo(tmp_path)
    inventory = _analog_inventory(spec_rows=["gain", "bandwidth", "psrr"])

    envelope = _attest(repo, inventory)

    lines = [
        attestation_check_line(c)
        for c in envelope["attestation"]["checks"]
        if c["check"] == "citation"
    ]
    assert "[FAIL] spec row psrr: no envelope cited" in lines
    assert any(line.startswith("[PASS] spec row gain: ") for line in lines)


def test_text_line_per_check_kind():
    from klayout_tools.signoff_attest_text import attestation_check_line

    assert (
        attestation_check_line(
            {
                "check": "recorded_hash",
                "role": "freshness",
                "target": "a.gds",
                "passed": False,
                "mismatches": [{"path": "a.gds"}],
            }
        )
        == "[FAIL] freshness a.gds recorded hash mismatch: a.gds"
    )
    assert (
        attestation_check_line(
            {
                "check": "metric",
                "role": "digital",
                "corner": "tt",
                "metric": "fmax",
                "value": None,
                "passed": False,
            }
        )
        == "[FAIL] digital tt fmax: missing"
    )
    with pytest.raises(ValueError):
        attestation_check_line({"check": "bogus", "passed": True})


# Issue #5538: a failing `command` check that re-verifies a tier-verdict
# record gets a live-only `hint` naming the record/envelope cycle and the way
# out. Same lifecycle as `output_tail` above: never in the written envelope,
# never in a `--check` diff, never built by an attestation builder.
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
