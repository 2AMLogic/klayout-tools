"""Unit tests for the eleven ``environment``-block contributors extracted out of
``sim._run_sim_corners``'s report epilogue (issue #5600).

Every key here is part of ``klt sim``'s JSON contract (``docs/cli/sim.md``),
and all but ``backend`` are additive/optional -- present only when the request
declared the corresponding input. So the failure mode these tests exist to
catch is a key that silently *stops* appearing (or starts appearing when it
should not): each helper is asserted in both its present and its absent state,
including the exact fragment it returns.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from klayout_tools import sim_environment_report as ser


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- netlist_closure / netlist_closure_error (issue #2799) -------------------


def test_netlist_closure_environment_pins_the_resolved_include_closure(tmp_path):
    (tmp_path / "shared.spice").write_text("R1 a b 1k\n")
    top = tmp_path / "top.spice"
    top.write_text(".include shared.spice\n")
    env = ser.netlist_closure_environment({}, str(top), repo_root=str(tmp_path))
    assert list(env) == ["netlist_closure"]
    assert [e["path"] for e in env["netlist_closure"]] == [
        "top.spice",
        "shared.spice",
    ]
    assert env["netlist_closure"][0]["sha256"] == _file_sha(top)


def test_netlist_closure_environment_discloses_an_unresolvable_closure(tmp_path):
    top = tmp_path / "top.spice"
    top.write_text(".include nope.spice\n")
    env = ser.netlist_closure_environment({}, str(top), repo_root=str(tmp_path))
    # Disclosed, never raised: exactly the other one of the two keys.
    assert list(env) == ["netlist_closure_error"]
    assert "nope.spice" in env["netlist_closure_error"]


def test_netlist_closure_environment_always_reports_exactly_one_key(tmp_path):
    (tmp_path / "ok.spice").write_text("R1 a b 1k\n")
    (tmp_path / "bad.spice").write_text(".include nope.spice\n")
    for name in ("ok.spice", "bad.spice"):
        env = ser.netlist_closure_environment(
            {}, str(tmp_path / name), repo_root=str(tmp_path)
        )
        assert len(env) == 1
        assert set(env) <= {"netlist_closure", "netlist_closure_error"}


# --- netlist_source ----------------------------------------------------------


def test_netlist_source_environment_present_only_when_declared():
    source = {"kind": "extract", "record": "run.json"}
    assert ser.netlist_source_environment(source) == {"netlist_source": source}
    assert ser.netlist_source_environment(None) == {}


# --- remote ------------------------------------------------------------------


def test_remote_entry_environment_absent_without_a_remote_backend():
    assert (
        ser.remote_entry_environment(
            None,
            corners_lost=0,
            artifacts_dir="/tmp/x",
            checkpoint_path=None,
        )
        == {}
    )


def test_remote_entry_environment_passes_the_block_through_untouched(tmp_path):
    remote = {"transport": "ssh", "host": "runner"}
    env = ser.remote_entry_environment(
        remote,
        corners_lost=0,
        artifacts_dir=str(tmp_path),
        checkpoint_path=None,
    )
    assert list(env) == ["remote"]
    # With nothing unrun, apply_unrun_signal returns the caller's own object.
    assert env["remote"] is remote


def test_remote_entry_environment_adds_the_unrun_signal(tmp_path, capsys):
    """Issue #5364's additive fields reach `environment.remote` through here."""
    remote = {"transport": "batch"}
    ckpt = tmp_path / "checkpoint.json"
    ckpt.write_text("{}")
    env = ser.remote_entry_environment(
        remote,
        corners_lost=2,
        artifacts_dir=str(tmp_path),
        checkpoint_path=str(ckpt),
    )
    assert env["remote"]["unrun_units"] == 2
    assert env["remote"]["resumable"] is True
    assert env["remote"]["shard_map"] == str(ckpt)
    assert "--resume" in capsys.readouterr().err


def test_remote_entry_environment_omits_the_signal_without_retained_state(tmp_path):
    """No retained checkpoint and no shard manifest -> nothing to resume from,
    so the block is passed through rather than marked resumable."""
    remote = {"transport": "batch"}
    env = ser.remote_entry_environment(
        remote,
        corners_lost=2,
        artifacts_dir=str(tmp_path),
        checkpoint_path=None,
    )
    assert env["remote"] == {"transport": "batch"}


# --- backend -----------------------------------------------------------------


def test_backend_environment_is_unconditional():
    provenance = {"resolved": "local", "source": "default", "requested": None}
    assert ser.backend_environment(provenance) == {"backend": provenance}


# --- monte_carlo -------------------------------------------------------------


def test_monte_carlo_environment_present_only_when_declared():
    info = {"samples": 8, "seed": 1234}
    assert ser.monte_carlo_environment(info) == {"monte_carlo": info}
    assert ser.monte_carlo_environment(None) == {}


# --- budget ------------------------------------------------------------------


def test_budget_environment_reports_the_schedule_as_applied():
    env = ser.budget_environment(
        30.0, elapsed_s=12.5, exceeded=False, corners_skipped=0
    )
    assert env == {
        "budget": {
            "wall_clock_budget_s": 30.0,
            "elapsed_s": 12.5,
            "exceeded": False,
            "corners_skipped": 0,
        }
    }


def test_budget_environment_absent_without_a_declared_budget():
    assert (
        ser.budget_environment(None, elapsed_s=1.0, exceeded=False, corners_skipped=0)
        == {}
    )


def test_budget_environment_reports_exceeded_and_skipped_even_at_zero_skips():
    env = ser.budget_environment(5.0, elapsed_s=9.0, exceeded=True, corners_skipped=0)
    assert env["budget"]["exceeded"] is True
    assert env["budget"]["corners_skipped"] == 0


# --- orphaned ----------------------------------------------------------------


def test_orphaned_environment_present_only_when_the_check_fired():
    assert ser.orphaned_environment(True) == {"orphaned": True}
    assert ser.orphaned_environment(False) == {}


# --- timeout_preflight_warning (issue #1686) ---------------------------------


def test_timeout_preflight_environment_present_only_when_advisory_fired():
    warning = {"code": "timeout_too_small", "message": "…"}
    assert ser.timeout_preflight_environment(warning) == {
        "timeout_preflight_warning": warning
    }
    assert ser.timeout_preflight_environment(None) == {}


# --- fail_fast_probe (issue #1694) -------------------------------------------


def test_fail_fast_probe_environment_present_only_when_the_probe_concluded():
    probe = {"abort": False, "reached_s": 1.2e-9, "estimate_s": 4.0}
    assert ser.fail_fast_probe_environment(probe) == {"fail_fast_probe": probe}
    assert ser.fail_fast_probe_environment(None) == {}


# --- resume ------------------------------------------------------------------


def test_resume_environment_normalises_the_checkpoint_path(tmp_path):
    ckpt = tmp_path / ".klt" / "sim" / "checkpoint.json"
    ckpt.parent.mkdir(parents=True)
    ckpt.write_text("{}")
    env = ser.resume_environment(
        True,
        resumed_corners=3,
        checkpoint_path=str(ckpt),
        checkpoint_retained=True,
        repo_root=str(tmp_path),
    )
    assert env["resume"]["resumed_corners"] == 3
    assert env["resume"]["checkpoint_retained"] is True
    assert env["resume"]["checkpoint_path"] == {
        "path": ".klt/sim/checkpoint.json",
        "scope": "repo",
    }


def test_resume_environment_absent_unless_resume_was_requested(tmp_path):
    assert (
        ser.resume_environment(
            False,
            resumed_corners=0,
            checkpoint_path=str(tmp_path / "c.json"),
            checkpoint_retained=False,
            repo_root=str(tmp_path),
        )
        == {}
    )


def test_resume_environment_reports_a_dropped_checkpoint(tmp_path):
    env = ser.resume_environment(
        True,
        resumed_corners=0,
        checkpoint_path=str(tmp_path / "c.json"),
        checkpoint_retained=False,
        repo_root=str(tmp_path),
    )
    assert env["resume"]["checkpoint_retained"] is False
    assert env["resume"]["resumed_corners"] == 0


# --- the module's own contract ----------------------------------------------


@pytest.mark.parametrize("name", ser.__all__)
def test_every_exported_helper_is_callable_and_documented(name):
    fn = getattr(ser, name)
    assert callable(fn)
    assert fn.__doc__, f"{name} must document the environment key it owns"
