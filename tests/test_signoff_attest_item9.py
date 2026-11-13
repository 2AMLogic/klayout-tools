"""``klt signoff --attest 9``: per-bench cold start, unbenched rows, stderr
progress (issue #4848)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
    check_attestation,
    render_envelope,
)

_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]
_BAD = [sys.executable, "-c", "import sys; sys.exit(1)"]
_PINS = {
    "pdk": {"name": "sky130A", "revision": "0fe599b2afb6708d281543108caf8310912f54af"},
    "tools": {"ngspice": "42"},
}


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tb").mkdir()
    (repo / "attest").mkdir()
    for name in ("a", "b"):
        (repo / "tb" / f"{name}.spice").write_text(f"* {name}\n")
    return repo


def _inv(repo: Path, **fields) -> str:
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 9,
        "pins": _PINS,
        **fields,
    }
    path = repo / "attest" / "item9.json"
    path.write_text(json.dumps(inventory))
    return str(path)


def _commands(envelope: dict) -> list[dict]:
    return [c for c in envelope["attestation"]["checks"] if c["check"] == "command"]


def test_per_bench_cold_start_names_the_failing_bench(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=[
            {"path": "tb/a.spice", "cold_start": {"argv": _OK}},
            {"path": "tb/b.spice", "cold_start": {"argv": _BAD}},
        ],
    )

    envelope = build_attestation(9, inventory)

    assert envelope["status"] == "fail"
    by_bench = {c["testbench"]: c["passed"] for c in _commands(envelope)}
    assert by_bench == {"tb/a.spice": True, "tb/b.spice": False}


def test_mixed_entries_with_top_level_cold_start(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=["tb/a.spice", {"path": "tb/b.spice", "cold_start": {"argv": _OK}}],
        cold_start={"argv": _OK},
    )

    envelope = build_attestation(9, inventory)

    assert envelope["status"] == "pass"
    commands = _commands(envelope)
    assert [c.get("testbench") for c in commands] == [None, "tb/b.spice"]


def test_old_form_envelope_has_no_new_fields(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(repo, testbenches=["tb/b.spice", "tb/a.spice"], cold_start={"argv": _OK}),
    )

    roles = [c["role"] for c in envelope["attestation"]["checks"]]
    assert roles == ["testbench", "testbench", "cold_start", "pdk", "tools"]
    assert all("testbench" not in c for c in _commands(envelope))


@pytest.mark.parametrize(
    "testbenches",
    [
        ["tb/a.spice", "tb/b.spice"],
        [{"path": "tb/a.spice", "cold_start": {"argv": _OK}}, "tb/b.spice"],
    ],
)
def test_missing_cold_start_is_refused(tmp_path, testbenches):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match="cold_start"):
        build_attestation(9, _inv(repo, testbenches=testbenches))


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"cold_start": {"argv": _OK}}, "path"),
        ({"path": "tb/a.spice", "cold_start": {"argv": []}}, "argv"),
        ({"path": "tb/a.spice", "cold_start": {"argv": _OK}, "x": 1}, "unknown"),
    ],
)
def test_malformed_bench_entry_is_refused(tmp_path, entry, message):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match=message):
        build_attestation(9, _inv(repo, testbenches=[entry]))


def test_duplicate_bench_path_is_refused(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match="more than once"):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=[
                    "tb/a.spice",
                    {"path": "tb/a.spice", "cold_start": {"argv": _OK}},
                ],
                cold_start={"argv": _OK},
            ),
        )


def test_unbenched_rows_surface_as_passing_checks(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=["tb/a.spice"],
            cold_start={"argv": _OK},
            unbenched_rows=[{"row": "R9", "issue": 12}, {"row": "R2", "issue": 7}],
        ),
    )

    assert envelope["status"] == "pass"
    rows = [
        c for c in envelope["attestation"]["checks"] if c["check"] == "unbenched_row"
    ]
    assert rows == [
        {
            "check": "unbenched_row",
            "role": "row",
            "row": "R2",
            "issue": 7,
            "passed": True,
        },
        {
            "check": "unbenched_row",
            "role": "row",
            "row": "R9",
            "issue": 12,
            "passed": True,
        },
    ]


@pytest.mark.parametrize(
    "rows",
    [
        "R1",
        [{"row": "R1"}],
        [{"row": "", "issue": 1}],
        [{"row": "R1", "issue": 0}],
        [{"row": "R1", "issue": True}],
        [{"row": "R1", "issue": 1, "extra": 1}],
        [{"row": "R1", "issue": 1}, {"row": "R1", "issue": 2}],
    ],
)
def test_malformed_unbenched_rows_are_refused(tmp_path, rows):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo, testbenches=["tb/a.spice"], cold_start={"argv": _OK}, unbenched_rows=rows
    )
    with pytest.raises(AttestationError, match="unbenched"):
        build_attestation(9, inventory)


def test_progress_goes_to_stderr_and_stdout_stays_one_envelope(tmp_path, capsys):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=[{"path": "tb/a.spice", "cold_start": {"argv": _OK}}],
        cold_start={"argv": _OK},
    )

    assert (
        main(["signoff", "--attest", "9", "--inventory", inventory, "--format", "json"])
        == 0
    )

    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "running cold_start ..." in captured.err
    assert "running cold_start tb/a.spice ..." in captured.err
    assert "cold_start tb/a.spice ok in " in captured.err


def test_local_only_bench_is_skipped_under_check(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        testbenches=[
            {"path": "tb/a.spice", "cold_start": {"argv": _BAD, "local_only": True}}
        ],
    )
    committed = repo / "evidence" / "item9.json"
    committed.parent.mkdir()
    # Attest-time: the local-only bench passes when the command passes.
    ok_inventory = _inv(
        repo,
        testbenches=[
            {"path": "tb/a.spice", "cold_start": {"argv": _OK, "local_only": True}}
        ],
    )
    committed.write_text(render_envelope(build_attestation(9, ok_inventory)))
    # --check must not run the (now failing) command; it carries the outcome.
    code = main(
        [
            "signoff",
            "--attest",
            "9",
            "--inventory",
            inventory,
            "--check",
            str(committed),
            "--format",
            "json",
        ]
    )
    assert code == 0


def _pdk_pin_check(envelope: dict) -> dict:
    return next(
        c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "pin" and c["role"] == "pdk"
    )


def _with_pdk(tmp_path, pdk) -> dict:
    repo = _repo(tmp_path)
    pins = {"tools": {"iverilog": "12.0"}}
    if pdk is not None:
        pins["pdk"] = pdk
    inventory = _inv(
        repo,
        pins=pins,
        testbenches=["tb/a.spice"],
        cold_start={"argv": _OK},
    )
    return build_attestation(9, inventory)


def test_no_pdk_form_passes(tmp_path):
    envelope = _with_pdk(tmp_path, {"none": True, "reason": "behavioral RTL only"})

    check = _pdk_pin_check(envelope)
    assert check["passed"] is True
    assert check["value"] == {"none": True, "reason": "behavioral RTL only"}
    assert envelope["status"] == "pass"


def test_absent_pdk_still_fails(tmp_path):
    envelope = _with_pdk(tmp_path, None)

    assert _pdk_pin_check(envelope)["passed"] is False
    assert envelope["status"] == "fail"


@pytest.mark.parametrize(
    "pdk",
    [
        {"none": True},
        {"none": True, "reason": ""},
        {"none": True, "reason": "  "},
        {"none": True, "reason": 5},
        {"none": True, "reason": "x", "name": "sky130A", "revision": "abc"},
        {"none": "yes", "reason": "x"},
    ],
)
def test_malformed_no_pdk_form_fails_without_crashing(tmp_path, pdk):
    envelope = _with_pdk(tmp_path, pdk)

    assert _pdk_pin_check(envelope)["passed"] is False
    assert envelope["status"] == "fail"


# --------------------------------------------------------------------------- #
# `--attest --check` drift names the check, not only its list index
# (issue #5460)
# --------------------------------------------------------------------------- #


def _attested(repo: Path, inventory: str) -> str:
    committed = repo / "evidence" / "item9.json"
    committed.parent.mkdir(exist_ok=True)
    committed.write_text(render_envelope(build_attestation(9, inventory)))
    return str(committed)


def test_check_drift_names_the_drifted_testbench_path(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo, testbenches=["tb/a.spice", "tb/b.spice"], cold_start={"argv": _OK}
    )
    committed = _attested(repo, inventory)
    (repo / "tb" / "a.spice").write_text("* a, edited\n")

    result = check_attestation(9, inventory, committed)

    (hashed,) = [
        e
        for e in result["drift"]
        if e["field"].startswith("attestation.checks.")
        and e["field"].endswith(".content_hash")
    ]
    assert hashed["field"] == "attestation.checks.0.content_hash"
    assert hashed["check"] == {"role": "testbench", "path": "tb/a.spice"}


def test_check_drift_names_a_drifted_command_by_its_testbench(tmp_path):
    """A command check whose *outcome* moved while its argv did not: the
    identity has to come from the check, there is no path in the pointer."""
    repo = _repo(tmp_path)
    flag = tmp_path / "flag"
    flag.write_text("present\n")
    probe = [
        sys.executable,
        "-c",
        f"import os, sys; sys.exit(0 if os.path.exists({str(flag)!r}) else 1)",
    ]
    inventory = _inv(
        repo, testbenches=[{"path": "tb/a.spice", "cold_start": {"argv": probe}}]
    )
    committed = _attested(repo, inventory)
    flag.unlink()

    result = check_attestation(9, inventory, committed)

    moved = [
        e
        for e in result["drift"]
        if e["field"].endswith((".exit_status", ".passed"))
        and e["field"].startswith("attestation.checks.")
    ]
    assert moved, result["drift"]
    for entry in moved:
        assert entry["check"] == {"role": "cold_start", "testbench": "tb/a.spice"}


def test_pins_are_identified_by_role_alone(tmp_path):
    """The `pdk`/`tools` pins carry no identity key beyond `role`, so their
    `check` object is `{"role": ...}` -- not an absent key."""
    repo = _repo(tmp_path)
    good = _inv(repo, testbenches=["tb/a.spice"], cold_start={"argv": _OK})
    committed = _attested(repo, good)
    moved = _inv(
        repo,
        pins={**_PINS, "tools": {"ngspice": "43"}},
        testbenches=["tb/a.spice"],
        cold_start={"argv": _OK},
    )

    result = check_attestation(9, moved, committed)

    pin_drift = [e for e in result["drift"] if e.get("check") == {"role": "tools"}]
    assert pin_drift, result["drift"]
    assert all(e["field"].startswith("attestation.checks.") for e in pin_drift)


def test_an_added_testbench_still_reports_one_unannotated_whole_list_entry(tmp_path):
    """The length-mismatch branch of the diff names no single check, so it
    gets no `check` key (identity-based matching is out of scope, #5460)."""
    repo = _repo(tmp_path)
    committed = _attested(
        repo, _inv(repo, testbenches=["tb/a.spice"], cold_start={"argv": _OK})
    )
    grown = _inv(
        repo, testbenches=["tb/a.spice", "tb/b.spice"], cold_start={"argv": _OK}
    )

    result = check_attestation(9, grown, committed)

    whole_list = [e for e in result["drift"] if e["field"] == "attestation.checks"]
    assert len(whole_list) == 1
    assert "check" not in whole_list[0]
    assert not any(
        e["field"].startswith("attestation.checks.") for e in result["drift"]
    )


def test_a_top_level_cold_start_is_identified_by_role_alone(tmp_path):
    """A top-level `cold_start` is one check covering every bench, so it has
    no `testbench` to name: `{"role": "cold_start"}` is its whole identity."""
    repo = _repo(tmp_path)
    flag = tmp_path / "flag"
    flag.write_text("present\n")
    probe = [
        sys.executable,
        "-c",
        f"import os, sys; sys.exit(0 if os.path.exists({str(flag)!r}) else 1)",
    ]
    inventory = _inv(repo, testbenches=["tb/a.spice"], cold_start={"argv": probe})
    committed = _attested(repo, inventory)
    flag.unlink()

    result = check_attestation(9, inventory, committed)

    moved = [e for e in result["drift"] if e["field"].endswith(".exit_status")]
    assert [e["check"] for e in moved] == [{"role": "cold_start"}]
