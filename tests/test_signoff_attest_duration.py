"""The live-only ``duration_s`` side-channel on ``klt signoff --attest``
command checks (issue #5817; split from test_signoff_attest_item8.py for the
file-size ratchet, like test_signoff_attest_live.py was for ``hint``).

Every command a run actually launches -- passing, failing and timed-out
alike -- records its observed wall time out of band (never in a check dict
an attestation builder builds: a wall-clock value never repeats, so a
duration inside the envelope would rewrite the committed bytes on every run
and read as permanent ``--check`` drift). The CLI attaches it to the *live*
``--format json`` result only, after any ``--output`` write and ``--check``
diff, exactly like ``output_tail`` (issue #4698).
"""

from __future__ import annotations

import json
import re
import sys

import pytest

from klayout_tools.signoff_attest import (
    check_attestation,
    render_envelope,
    write_attestation,
)
from klayout_tools.signoff_attest_command import (
    attach_durations,
    attach_output_tails,
    failed_output_tail,
)
from klayout_tools.signoff_attest_stream import (
    build_stream_attestation,
    check_stream_attestation,
)
from test_signoff_attest_item2 import _inv, _stream
from test_signoff_attest_item8 import _analog_inventory, _attest, _repo

#: The text report's one timed audit line, ``[PASS] audit ci: <argv> (exit
#: N) in M.Ns``: the seconds are the run's own wall clock, so no two runs
#: ever print the same value there -- a stdout comparison that keeps them
#: compares clocks, not reports (issue #6868's legacy-stdout CI flake).
#: Shared with test_signoff_attest_description.py, whose 500-line ratchet
#: has no room for the pair.
_AUDIT_SECONDS_RE = re.compile(
    r"(?m)^(?P<line>\[PASS\] audit ci: .*\(exit \d+\) in )[0-9]+\.[0-9]s$"
)


def _audit_seconds_masked(report: str) -> str:
    """The report with exactly its one timed audit line's seconds masked.

    The command, exit status, and the line's newline stay in the comparison;
    a report whose timed audit line is missing (or duplicated) fails here
    rather than silently passing (issue #6868)."""
    masked, found = _AUDIT_SECONDS_RE.subn(r"\g<line><d>s", report)
    assert found == 1, report
    return masked


def test_command_duration_is_live_only_and_never_in_built_envelope(tmp_path):
    repo = _repo(tmp_path)
    envelope = _attest(repo, _analog_inventory())
    checks = envelope["attestation"]["checks"]
    assert all("duration_s" not in c for c in checks)

    attach_durations(envelope)

    ran = [c for c in checks if c["check"] == "command"]
    assert len(ran) == 2  # both envelope_checks commands launched and passed
    assert all(isinstance(c["duration_s"], float) and c["duration_s"] >= 0 for c in ran)
    assert all("duration_s" not in c for c in checks if c["check"] != "command")


def test_differing_runtimes_do_not_change_the_envelope_bytes(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["envelope_checks"]["evidence/sim_ss.json"] = {
        "argv": [
            sys.executable,
            "-c",
            "import os, time; time.sleep(float(os.environ['DUR_X']))",
        ]
    }
    rendered = []
    durations = []
    for sleep_s in ("0", "0.3"):
        monkeypatch.setenv("DUR_X", sleep_s)
        envelope = _attest(repo, inventory)
        rendered.append(render_envelope(envelope))
        attach_durations(envelope)
        check = next(
            c
            for c in envelope["attestation"]["checks"]
            if c["check"] == "command" and c["target"] == "evidence/sim_ss.json"
        )
        durations.append(check["duration_s"])
        assert "duration_s" in render_envelope(envelope)  # live result only

    assert durations[1] > durations[0]
    assert rendered[0] == rendered[1]
    assert "duration_s" not in rendered[0]


def test_check_reports_match_across_differing_runtimes(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["envelope_checks"]["evidence/sim_ss.json"] = {
        "argv": [
            sys.executable,
            "-c",
            "import os, time; time.sleep(float(os.environ['DUR_X']))",
        ]
    }
    monkeypatch.setenv("DUR_X", "0")
    envelope = _attest(repo, inventory)
    committed = repo / "evidence" / "item8.json"
    committed.write_text(render_envelope(envelope))
    inv_path = str(repo / "inventory.json")

    assert check_attestation(8, inv_path, str(committed))["status"] == "match"

    monkeypatch.setenv("DUR_X", "0.3")
    rerun = check_attestation(8, inv_path, str(committed))
    assert rerun["status"] == "match"
    assert json.dumps(rerun).count("duration_s") == 0


def test_write_attestation_unchanged_second_run_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    envelope = _attest(repo, _analog_inventory())
    committed = repo / "evidence" / "item8.json"
    assert write_attestation(envelope, str(committed)) is True

    rerun = _attest(repo, _analog_inventory())
    assert render_envelope(rerun) == render_envelope(envelope)
    assert write_attestation(rerun, str(committed)) is False


def test_duration_covers_timeout_but_not_launch_failure(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["envelope_checks"]["evidence/sim_tt.json"] = {
        "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
        "timeout_s": 0.5,
    }
    inventory["envelope_checks"]["evidence/sim_ss.json"] = {
        "argv": ["no-such-binary-5817"]
    }
    envelope = _attest(repo, inventory)
    checks = envelope["attestation"]["checks"]
    assert all("duration_s" not in c for c in checks)  # built envelope never

    attach_durations(envelope)

    by_target = {c["target"]: c for c in checks if c["check"] == "command"}
    timed_out = by_target["evidence/sim_tt.json"]
    assert timed_out["error"] == "timeout" and timed_out["exit_status"] is None
    assert timed_out["duration_s"] >= 0.5
    unlaunched = by_target["evidence/sim_ss.json"]
    assert unlaunched["error"] == "launch_failed"
    assert "duration_s" not in unlaunched


def test_skipped_local_only_check_has_no_duration_or_stale_tail(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["envelope_checks"]["evidence/sim_ss.json"] = {
        "argv": [
            sys.executable,
            "-c",
            "import sys; print('BOOM-TAIL'); sys.exit(7)",
        ],
        "local_only": True,
    }
    envelope = _attest(repo, inventory)  # generate mode runs it; it fails
    committed = repo / "evidence" / "item8.json"
    committed.write_text(render_envelope(envelope))  # saved before decorating
    assert "output_tail" not in committed.read_text()
    attach_output_tails(envelope)
    ran_local = next(
        c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "command" and c.get("local_only")
    )
    assert ran_local["output_tail"] == "BOOM-TAIL"  # build 1 captured it
    inv_path = str(repo / "inventory.json")

    result = check_attestation(8, inv_path, str(committed))
    assert result["status"] == "match"
    skipped = result["skipped_checks"]
    assert len(skipped) == 1
    attach_durations(result["fresh"])
    attach_output_tails(result["fresh"])
    assert all("duration_s" not in c and "output_tail" not in c for c in skipped)
    # The carried check also stays in checks[] (its `skipped` marker is popped
    # into skipped_checks by the drift result), marked only by local_only: it
    # never ran, so it has no duration -- and no stale tail from build 1
    # (#6804), while its committed failure is still carried.
    carried = next(
        c
        for c in result["fresh"]["attestation"]["checks"]
        if c["check"] == "command" and c.get("local_only")
    )
    assert not carried["passed"] and carried["exit_status"] == 7
    assert "duration_s" not in carried
    assert "output_tail" not in carried
    assert failed_output_tail(carried) == ""
    ran = [
        c
        for c in result["fresh"]["attestation"]["checks"]
        if c["check"] == "command" and not c.get("local_only")
    ]
    assert all("duration_s" in c for c in ran)


def test_checks_sharing_argv_and_cwd_share_the_recorded_duration(tmp_path):
    # The side-channel key is (argv, cwd), so two checks with the same command
    # collide exactly as output_tail already does: the last run's value wins
    # and both checks read it. Existing behaviour, asserted not fixed here.
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    shared = [sys.executable, "-c", "pass"]
    inventory["envelope_checks"] = {
        "evidence/sim_tt.json": {"argv": list(shared)},
        "evidence/sim_ss.json": {"argv": list(shared)},
    }
    envelope = _attest(repo, inventory)
    attach_durations(envelope)
    ran = [c for c in envelope["attestation"]["checks"] if c["check"] == "command"]
    assert len(ran) == 2
    assert ran[0]["duration_s"] == ran[1]["duration_s"]


def test_text_line_names_the_observed_runtime():
    from klayout_tools.signoff_attest_text import attestation_check_line

    check = {
        "check": "command",
        "role": "freshness",
        "target": "evidence/sim_tt.json",
        "argv": [sys.executable, "-c", "pass"],
        "cwd": ".",
        "exit_status": 0,
        "passed": True,
    }
    assert " in " not in attestation_check_line(check)  # no runtime recorded
    assert attestation_check_line({**check, "duration_s": 1.2}).endswith(
        "(exit 0) in 1.2s"
    )
    failed = {**check, "passed": False, "exit_status": None, "error": "timeout"}
    assert attestation_check_line({**failed, "duration_s": 3.4}).endswith(
        "timeout) in 3.4s"
    )


def test_audit_seconds_mask_absorbs_only_the_timed_audit_line():
    """A pair of reports differing only in the audit line's own seconds --
    the 0.0s-vs-0.1s legacy-stdout CI flake (issue #6868) -- compares equal
    through the mask; a duration-like string in the disclosure, or a report
    that lost its timed audit line, still compares unequal."""

    def report(seconds: str, disclosure: str) -> str:
        return (
            "attest: T1 item 10  status: pass\n"
            f"description: {disclosure}\n\n"
            f"[PASS] audit ci: py -c pass (exit 0) in {seconds}\n"
        )

    assert _audit_seconds_masked(report("0.0s", "d")) == _audit_seconds_masked(
        report("0.1s", "d")
    )
    with pytest.raises(AssertionError):
        _audit_seconds_masked(report("0.00s", "d"))
    assert _audit_seconds_masked(report("0.1s", "x took 0.0s")) != (
        _audit_seconds_masked(report("0.1s", "x took 0.1s"))
    )
    untimed = report("0.0s", "d").replace(" in 0.0s", "")
    with pytest.raises(AssertionError):
        _audit_seconds_masked(untimed)


def test_stream_skipped_local_only_check_has_no_duration_or_stale_tail(tmp_path):
    # Stream sibling of test_skipped_local_only_check_has_no_duration: the
    # item-2 builder reaches _command_check via _inventory_checks, which never
    # entered _run_checks' reset, so an earlier build in the same process
    # could serve its wall time -- or, before #6804, its output tail -- to a
    # carried check (#6250).
    repo, stream = _stream(tmp_path)
    local = {
        "name": "lo",
        "argv": ["python3", "-c", "import sys; print('LO-TAIL'); sys.exit(7)"],
        "local_only": True,
    }
    inv = _inv(repo, [{"name": "ok", "argv": ["python3", "-c", "pass"]}, local])
    envelope = build_stream_attestation(stream, inventory_path=inv)  # runs both
    committed = repo / "evidence" / "item2.json"
    committed.write_text(render_envelope(envelope))  # saved before decorating
    assert "output_tail" not in committed.read_text()
    attach_output_tails(envelope)
    ran_local = next(
        c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "command" and c.get("local_only")
    )
    assert ran_local["output_tail"] == "LO-TAIL"  # build 1 captured it

    result = check_stream_attestation(stream, str(committed), inventory_path=inv)
    assert result["status"] == "match"
    assert [c["name"] for c in result["skipped_checks"]] == ["lo"]
    attach_durations(result["fresh"])
    attach_output_tails(result["fresh"])
    assert all(
        "duration_s" not in c and "output_tail" not in c
        for c in result["skipped_checks"]
    )
    # The carried local_only check also stays in checks[], marked only by
    # local_only: it never ran in this build, so it has no duration -- and,
    # since #6804, no stale tail from the generating build either.
    carried = next(
        c
        for c in result["fresh"]["attestation"]["checks"]
        if c["check"] == "command" and c.get("local_only")
    )
    assert not carried["passed"]
    assert "duration_s" not in carried
    assert "output_tail" not in carried
    assert failed_output_tail(carried) == ""
    ran = [
        c
        for c in result["fresh"]["attestation"]["checks"]
        if c["check"] == "command" and not c.get("local_only")
    ]
    assert all("duration_s" in c for c in ran)
