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
import sys

from klayout_tools.signoff_attest import (
    check_attestation,
    render_envelope,
    write_attestation,
)
from klayout_tools.signoff_attest_command import (
    attach_durations,
)
from test_signoff_attest_item8 import _analog_inventory, _attest, _repo


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


def test_skipped_local_only_check_has_no_duration(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["envelope_checks"]["evidence/sim_ss.json"] = {
        "argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
        "local_only": True,
    }
    envelope = _attest(repo, inventory)  # generate mode runs it
    committed = repo / "evidence" / "item8.json"
    committed.write_text(render_envelope(envelope))
    inv_path = str(repo / "inventory.json")

    result = check_attestation(8, inv_path, str(committed))
    skipped = result["skipped_checks"]
    assert len(skipped) == 1
    attach_durations(result["fresh"])
    assert all("duration_s" not in c for c in skipped)
    # The carried check also stays in checks[] (its `skipped` marker is popped
    # into skipped_checks by the drift result), marked only by local_only: it
    # never ran, so it has no duration either.
    carried = next(
        c
        for c in result["fresh"]["attestation"]["checks"]
        if c["check"] == "command" and c.get("local_only")
    )
    assert "duration_s" not in carried
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
