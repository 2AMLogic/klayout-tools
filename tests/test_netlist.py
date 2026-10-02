"""Tests for `klt netlist` (issue #55): the headless xschem schematic ->
SPICE netlist wrapper and its `--check` staleness gate.

**Everything here runs against a stub `xschem`**, never the real binary, for
the same reason `tests/test_drc_klayout_engine.py` stubs `klayout`: CI cannot
assume a schematic-capture tool is installed. Unlike that module, though, the
stub is a real executable shell script launched through a real
`subprocess.Popen` rather than a patched `subprocess.run` -- the three
behaviours this verb exists to absorb are all *process* behaviours, and a
mocked `subprocess` cannot exhibit any of them:

- exiting non-zero after a successful export (`_STUB_NONZERO_EXIT`),
- hanging past the timeout while **ignoring SIGTERM**, so only SIGKILL stops
  it (`_STUB_IGNORES_SIGTERM`) -- the failure mode issue #55 was filed over,
- leaving a grandchild process behind that an un-grouped kill would orphan
  (`_STUB_SPAWNS_GRANDCHILD`).

The stub writes the netlist its invocation asked for by parsing its own
`-o <dir>` argument and the trailing schematic path, i.e. exactly the two
things `klt netlist` tells it, so a change to the real invocation shape shows
up here as a failing test rather than as a silently-unexercised stub.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

from klayout_tools import netlist as netlist_module
from klayout_tools.cli import main
from klayout_tools.netlist import (
    FORCED_XSCHEM_FLAGS,
    NetlistError,
    run_netlist,
)

# --------------------------------------------------------------------------- #
# xschem stubs
# --------------------------------------------------------------------------- #

#: Shared prologue: record the full argv (one arg per line, so a test can
#: assert on exact flags) and work out where the netlist is meant to go from
#: the invocation itself.
_STUB_PREAMBLE = r"""#!/bin/sh
printf '%s\n' "$@" > "$ARGV_LOG"
out_dir=""
schematic=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out_dir="$arg"; fi
  case "$arg" in -*) ;; *) schematic="$arg" ;; esac
  prev="$arg"
done
stem=$(basename "$schematic" .sch)
"""

#: The normal case, and issue #55's first sharp edge: a *successful* batch
#: netlist that exits non-zero. If `klt netlist` ever regresses to trusting
#: `$?`, this stub makes it fail.
_STUB_NONZERO_EXIT = (
    _STUB_PREAMBLE
    + """
printf '%s\\n' "$NETLIST_BODY" > "$out_dir/$stem.spice"
exit 1
"""
)

#: Exits 0 and writes nothing: the inverse trap. A zero exit status is not
#: success either, so this must be an error rather than an empty "clean" run.
_STUB_SILENT_SUCCESS_NO_FILE = (
    _STUB_PREAMBLE
    + """
exit 0
"""
)

#: Writes the netlist under a name nothing expects -- the single-fresh-file
#: fallback in `_resolve_produced_netlist`.
_STUB_UNEXPECTED_FILENAME = (
    _STUB_PREAMBLE
    + """
printf '%s\\n' "$NETLIST_BODY" > "$out_dir/$stem.cir"
exit 1
"""
)

#: Writes two files under names nothing expects: ambiguous, so it must fail
#: rather than guess which one is the netlist.
_STUB_TWO_UNEXPECTED_FILES = (
    _STUB_PREAMBLE
    + """
printf 'a\\n' > "$out_dir/$stem.cir"
printf 'b\\n' > "$out_dir/other.cir"
exit 1
"""
)

#: Issue #55's fifth, silent sharp edge, reproduced: hangs forever and
#: **ignores SIGTERM** (`trap '' TERM` is the portable stand-in for xschem's
#: re-entered, non-async-signal-safe `sig_handler`). Only SIGKILL stops it.
_STUB_IGNORES_SIGTERM = (
    _STUB_PREAMBLE
    + """
trap '' TERM
echo "initialising GUI" >&2
while : ; do sleep 0.05; done
"""
)

#: Hangs, but dies on SIGTERM -- so the wrapper must report that it did
#: *not* need to escalate, rather than claiming a SIGKILL it never sent.
_STUB_HANGS_KILLABLE = (
    _STUB_PREAMBLE
    + """
while : ; do sleep 0.05; done
"""
)

#: Hangs after spawning a background grandchild, recording its pid. An
#: un-grouped kill reaps the stub and orphans the grandchild -- the "orphaned
#: to launchd" outcome issue #55 reported.
_STUB_SPAWNS_GRANDCHILD = (
    _STUB_PREAMBLE
    + """
sh -c 'while : ; do sleep 0.05; done' &
echo "$!" > "$GRANDCHILD_PID_FILE"
trap '' TERM
while : ; do sleep 0.05; done
"""
)

_NETLIST_BODY = "* stub netlist\n.subckt block a b\n.ends\n.end"


def _write_stub(tmp_path: Path, body: str, name: str = "xschem") -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _write_schematic(tmp_path: Path, name: str = "block.sch") -> str:
    path = tmp_path / name
    path.write_text("v {xschem version=3.4.7}\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def argv_log(tmp_path, monkeypatch) -> Path:
    """Where every stub records the argv it was invoked with."""
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    return log


def _recorded_argv(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines()


# --------------------------------------------------------------------------- #
# (1) The wrapper owns the invocation: -x can never be dropped
# --------------------------------------------------------------------------- #


def test_forced_flags_are_always_passed(tmp_path, argv_log):
    """The caller never spells the xschem command line, so a copy-paste that
    omits `-x` (issue #55's silent hang) is not expressible."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)

    report = run_netlist(
        schematic, str(tmp_path / "out" / "block.spice"), xschem_binary=binary
    )

    argv = _recorded_argv(argv_log)
    for flag in FORCED_XSCHEM_FLAGS:
        assert flag in argv, f"{flag} missing from {argv}"
    assert "-x" in argv  # named explicitly: this is the load-bearing one
    assert report["xschem"]["forced_flags"] == list(FORCED_XSCHEM_FLAGS)
    # And the payload records the exact command line that ran, so a consumer
    # can see for itself that -x was there.
    assert report["xschem"]["argv"][0] == binary
    assert "-x" in report["xschem"]["argv"]


def test_netlists_into_a_private_directory_not_the_output_path(tmp_path, argv_log):
    """`-o` always points at a temp dir, never at the caller's output path:
    a hung or failed run must not be able to leave a plausible-looking
    artifact where a consumer would read it."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "netlist" / "block.spice"

    run_netlist(schematic, str(output), xschem_binary=binary)

    argv = _recorded_argv(argv_log)
    out_dir = argv[argv.index("-o") + 1]
    assert out_dir != str(output.parent)
    assert "klt-netlist-" in out_dir
    # ...and the temp dir is cleaned up on the way out.
    assert not os.path.exists(out_dir)


def test_rcfile_is_passed_through(tmp_path, argv_log):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    rcfile = tmp_path / "xschemrc"
    rcfile.write_text("# project-local rc\n", encoding="utf-8")

    run_netlist(
        schematic,
        str(tmp_path / "block.spice"),
        xschem_binary=binary,
        rcfile=str(rcfile),
    )

    argv = _recorded_argv(argv_log)
    assert argv[argv.index("--rcfile") + 1] == str(rcfile)


def test_missing_rcfile_fails_before_launching(tmp_path, argv_log):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)

    with pytest.raises(NetlistError, match="xschemrc file not found"):
        run_netlist(
            schematic,
            str(tmp_path / "block.spice"),
            xschem_binary=binary,
            rcfile=str(tmp_path / "nope"),
        )
    assert not argv_log.exists()


# --------------------------------------------------------------------------- #
# (3) The exit status is a record, not the verdict
# --------------------------------------------------------------------------- #


def test_nonzero_exit_on_success_is_still_success(tmp_path, argv_log):
    """Issue #55 edge #1: `xschem -n -s -q -x` returns non-zero *after a
    successful export*. The verdict is the fresh output file."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "out" / "block.spice"

    report = run_netlist(schematic, str(output), xschem_binary=binary)

    assert report["status"] == "generated"
    assert report["xschem"]["exit_status"] == 1
    # Stated in the payload itself, so a consumer is not left to infer it.
    assert report["xschem"]["exit_status_trusted"] is False
    assert output.read_text(encoding="utf-8") == _NETLIST_BODY + "\n"
    assert report["netlist"]["path"] == str(output)
    assert report["netlist"]["content_hash"].startswith("sha256:")
    assert report["netlist"]["lines"] == 4


def test_zero_exit_with_no_output_file_is_a_failure(tmp_path, argv_log):
    """The inverse trap: a zero exit status is not success either."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_SILENT_SUCCESS_NO_FILE)

    with pytest.raises(NetlistError) as excinfo:
        run_netlist(schematic, str(tmp_path / "block.spice"), xschem_binary=binary)

    message = str(excinfo.value)
    assert "produced no fresh netlist file" in message
    assert "block.spice" in message  # names what it expected
    assert "exit status is deliberately not used" in message
    assert not (tmp_path / "block.spice").exists()


def test_single_unexpected_filename_is_accepted_and_named(tmp_path, argv_log):
    """A future xschem naming change should surface as a different reported
    filename, not as a spurious "produced nothing"."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_UNEXPECTED_FILENAME)
    output = tmp_path / "block.spice"

    report = run_netlist(schematic, str(output), xschem_binary=binary)

    assert report["status"] == "generated"
    assert output.read_text(encoding="utf-8") == _NETLIST_BODY + "\n"


def test_ambiguous_output_files_fail_rather_than_guess(tmp_path, argv_log):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_TWO_UNEXPECTED_FILES)

    with pytest.raises(NetlistError) as excinfo:
        run_netlist(schematic, str(tmp_path / "block.spice"), xschem_binary=binary)

    message = str(excinfo.value)
    assert "produced 2 files" in message
    assert "other.cir" in message


def test_stale_output_file_is_not_mistaken_for_this_runs_output(tmp_path):
    """The freshness half of the success test, unit-tested directly: a file
    left behind by an *earlier* run must not be read as this run's output --
    that is the drift failure `--check` exists to catch, and accepting a
    stale file here would re-introduce it one layer down."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stale = work_dir / "block.spice"
    stale.write_text("* from last week\n", encoding="utf-8")
    long_ago = time.time() - 86_400
    os.utime(stale, (long_ago, long_ago))

    with pytest.raises(NetlistError, match="produced no fresh netlist file"):
        netlist_module._resolve_produced_netlist(
            str(work_dir),
            "block",
            time.time(),
            exit_status=1,
            stdout="",
            stderr="",
        )

    # ...and a file written now, in the same directory, is accepted.
    stale.write_text("* fresh\n", encoding="utf-8")
    assert netlist_module._resolve_produced_netlist(
        str(work_dir), "block", time.time(), exit_status=1, stdout="", stderr=""
    ) == str(stale)


# --------------------------------------------------------------------------- #
# (2) Timeout with SIGKILL escalation
# --------------------------------------------------------------------------- #


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not expected for our own child
        return True
    return True


def test_hang_ignoring_sigterm_is_escalated_to_sigkill(tmp_path, argv_log):
    """Issue #55's actual failure: xschem hangs and SIGTERM does nothing.
    `subprocess.run(timeout=...)` alone would have been enough only if
    SIGTERM worked, so the escalation is the test."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_IGNORES_SIGTERM)

    started = time.monotonic()
    with pytest.raises(NetlistError) as excinfo:
        run_netlist(
            schematic,
            str(tmp_path / "block.spice"),
            xschem_binary=binary,
            timeout_s=0.5,
        )
    elapsed = time.monotonic() - started

    message = str(excinfo.value)
    assert "did not complete within 0.5s" in message
    assert "SIGTERM was ignored, so it was SIGKILLed" in message
    assert "No netlist was written" in message
    # The actionable half: name the cause a caller cannot see for themselves.
    assert "initialises its GUI" in message
    assert " ".join(FORCED_XSCHEM_FLAGS) in message
    # It really did return promptly rather than waiting out the hang.
    assert elapsed < 20
    assert not (tmp_path / "block.spice").exists()


def test_hang_that_dies_on_sigterm_is_reported_as_such(tmp_path, argv_log):
    """The escalation is conditional: do not claim a SIGKILL that was never
    sent."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_HANGS_KILLABLE)

    with pytest.raises(NetlistError) as excinfo:
        run_netlist(
            schematic,
            str(tmp_path / "block.spice"),
            xschem_binary=binary,
            timeout_s=0.5,
        )

    message = str(excinfo.value)
    assert "it exited on SIGTERM" in message
    assert "SIGKILLed" not in message


def test_timeout_kills_the_whole_process_group(tmp_path, argv_log, monkeypatch):
    """Signalling only the parent orphans whatever xschem spawned (issue #55
    saw one orphaned to launchd for over an hour), so the kill addresses the
    child's process group."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_SPAWNS_GRANDCHILD)
    pid_file = tmp_path / "grandchild.pid"
    monkeypatch.setenv("GRANDCHILD_PID_FILE", str(pid_file))

    with pytest.raises(NetlistError, match="whole process group was signalled"):
        run_netlist(
            schematic,
            str(tmp_path / "block.spice"),
            xschem_binary=binary,
            timeout_s=1.0,
        )

    grandchild = int(pid_file.read_text(encoding="utf-8").strip())
    # SIGKILL delivery is asynchronous; poll briefly rather than racing it.
    deadline = time.monotonic() + 5
    while _pid_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(grandchild), f"grandchild {grandchild} was orphaned"


def test_signal_group_never_signals_our_own_group(tmp_path, monkeypatch):
    """The guard that keeps a process-group kill from taking out the `klt`
    run itself if a child ever shared our group."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        netlist_module.os, "getpgid", lambda _pid: os.getpgrp(), raising=True
    )
    monkeypatch.setattr(
        netlist_module.os,
        "killpg",
        lambda pgid, sig: sent.append(("killpg", pgid, sig)),
        raising=True,
    )

    class _FakeProc:
        pid = os.getpid()

        def send_signal(self, sig):
            sent.append(("send_signal", sig))

    netlist_module._signal_group(_FakeProc(), signal.SIGTERM)

    assert sent == [("send_signal", signal.SIGTERM)]


def test_non_positive_timeout_is_rejected(tmp_path):
    schematic = _write_schematic(tmp_path)
    with pytest.raises(NetlistError, match="--timeout-s must be positive"):
        run_netlist(schematic, str(tmp_path / "block.spice"), timeout_s=0)


# --------------------------------------------------------------------------- #
# (4) --check: the staleness gate
# --------------------------------------------------------------------------- #


def test_check_reports_match_for_an_up_to_date_netlist(tmp_path, argv_log):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    output.write_text(_NETLIST_BODY + "\n", encoding="utf-8")

    report = run_netlist(schematic, str(output), check=True, xschem_binary=binary)

    assert report["status"] == "match"
    assert report["mode"] == "check"
    assert report["drift"]["committed_present"] is True
    assert report["drift"]["diff"] == []
    assert (
        report["drift"]["committed_content_hash"] == report["netlist"]["content_hash"]
    )
    # The regenerated copy is not retained, so it is not named.
    assert report["netlist"]["path"] is None


def test_check_reports_drift_and_never_writes(tmp_path, argv_log):
    """The highest-value half of issue #55: a committed netlist that no
    longer matches its schematics must be reported, not silently refreshed."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    stale = "* stub netlist\n.subckt block a b c\n.ends\n.end\n"
    output.write_text(stale, encoding="utf-8")

    report = run_netlist(schematic, str(output), check=True, xschem_binary=binary)

    assert report["status"] == "drifted"
    assert report["drift"]["committed_present"] is True
    assert any(
        line.startswith("-.subckt block a b c") for line in report["drift"]["diff"]
    )
    assert any(
        line.startswith("+.subckt block a b") for line in report["drift"]["diff"]
    )
    assert report["drift"]["diff_truncated"] is False
    # --check is read-only: the committed file is untouched.
    assert output.read_text(encoding="utf-8") == stale


def test_check_treats_an_absent_committed_netlist_as_drift(tmp_path, argv_log):
    """ "Never committed" and "committed but stale" get the same non-zero
    exit, so a CI gate does not have to distinguish them."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "nothing-here.spice"

    report = run_netlist(schematic, str(output), check=True, xschem_binary=binary)

    assert report["status"] == "drifted"
    assert report["drift"]["committed_present"] is False
    assert report["drift"]["committed_content_hash"] is None
    assert "no committed netlist" in report["drift"]["reason"]
    assert not output.exists()


def test_check_diff_is_capped(tmp_path, argv_log, monkeypatch):
    """A drifted netlist can differ by thousands of lines; the payload says
    *that* it drifted and shows the head, it is not a file transport."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    monkeypatch.setenv("NETLIST_BODY", "\n".join(f"* fresh {i}" for i in range(500)))
    output = tmp_path / "block.spice"
    output.write_text(
        "\n".join(f"* committed {i}" for i in range(500)) + "\n", encoding="utf-8"
    )

    report = run_netlist(schematic, str(output), check=True, xschem_binary=binary)

    assert report["status"] == "drifted"
    assert report["drift"]["diff_truncated"] is True
    assert len(report["drift"]["diff"]) == netlist_module.MAX_DIFF_LINES


# --------------------------------------------------------------------------- #
# Input validation and binary resolution
# --------------------------------------------------------------------------- #


def test_missing_schematic_fails_before_launching(tmp_path, argv_log):
    with pytest.raises(NetlistError, match="schematic not found"):
        run_netlist(str(tmp_path / "nope.sch"), str(tmp_path / "block.spice"))
    assert not argv_log.exists()


def test_schematic_directory_is_rejected(tmp_path):
    with pytest.raises(NetlistError, match="is a directory"):
        run_netlist(str(tmp_path), str(tmp_path / "block.spice"))


def test_missing_binary_is_an_actionable_error(tmp_path):
    schematic = _write_schematic(tmp_path)
    with pytest.raises(NetlistError) as excinfo:
        run_netlist(
            schematic,
            str(tmp_path / "block.spice"),
            xschem_binary=str(tmp_path / "definitely-not-installed"),
        )
    message = str(excinfo.value)
    assert "could not launch xschem" in message
    assert "--xschem-binary" in message
    assert "xschem.sourceforge.io" in message


# --------------------------------------------------------------------------- #
# CLI surface: exit codes and the shared JSON envelope
# --------------------------------------------------------------------------- #


def test_cli_generate_json_envelope(tmp_path, argv_log, capsys):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    code = main(
        [
            "netlist",
            schematic,
            "-o",
            str(output),
            "--xschem-binary",
            binary,
            "--format",
            "json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == netlist_module.SCHEMA_VERSION
    assert payload["status"] == "generated"
    assert payload["mode"] == "generate"
    assert payload["xschem"]["exit_status_trusted"] is False
    assert output.exists()


def test_cli_check_exit_codes(tmp_path, argv_log, capsys):
    """0 for match, 3 for drifted -- the same split `klt drc --check` uses."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    argv = [
        "netlist",
        schematic,
        "-o",
        str(output),
        "--check",
        "--xschem-binary",
        binary,
        "--format",
        "json",
    ]

    output.write_text(_NETLIST_BODY + "\n", encoding="utf-8")
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"

    output.write_text("* drifted\n", encoding="utf-8")
    assert main(argv) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "drifted"


def test_cli_error_uses_the_shared_error_envelope(tmp_path, capsys):
    code = main(
        [
            "netlist",
            str(tmp_path / "nope.sch"),
            "-o",
            str(tmp_path / "block.spice"),
            "--format",
            "json",
        ]
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "netlist"
    assert "schematic not found" in error["error"]["message"]


def test_cli_text_output_qualifies_the_exit_status(tmp_path, argv_log, capsys):
    """A reader who sees `exit_status=1` next to `status: generated` with no
    explanation reasonably concludes something broke, so the courtesy
    rendering never prints the number bare."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)

    code = main(
        [
            "netlist",
            schematic,
            "-o",
            str(tmp_path / "block.spice"),
            "--xschem-binary",
            binary,
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "status: generated" in out
    assert "exit_status=1" in out
    assert "not the verdict" in out
    assert "-x" in out


def test_cli_output_is_required(tmp_path):
    """The output path is the committed artifact's identity in both modes --
    there is no default, so omitting it is a usage error, not a guess."""
    with pytest.raises(SystemExit) as excinfo:
        main(["netlist", str(tmp_path / "block.sch")])
    assert excinfo.value.code == 2


def test_timeout_message_names_the_bound_without_a_subprocess():
    """Unit-level guard on the error text itself, so the message contract is
    asserted independently of the (slower) real-hang tests above."""
    message = netlist_module._timeout_message(
        ["xschem", "-n", "-x", "block.sch"], 7.5, True, "some output", ""
    )
    assert "within 7.5s" in message
    assert "SIGKILLed" in message
    assert "some output" in message
    assert "--timeout-s" in message


def test_stub_really_exits_nonzero_on_success(tmp_path, argv_log):
    """Guard on the fixtures themselves: if the stub stopped exhibiting
    xschem's non-zero-on-success behaviour, the tests above would still pass
    while no longer testing anything."""
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    out_dir = tmp_path / "raw"
    out_dir.mkdir()

    completed = subprocess.run(
        [binary, *FORCED_XSCHEM_FLAGS, "-o", str(out_dir), schematic],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "ARGV_LOG": str(argv_log), "NETLIST_BODY": _NETLIST_BODY},
    )

    assert completed.returncode != 0
    assert (out_dir / "block.spice").is_file()


# ---------------------------------------------------------------------------
# PDK-root consistency with `klt pdk` (issue #2680, edge 2 option A)
# ---------------------------------------------------------------------------


def _fake_pdk_root(tmp_path: Path, name: str = "pdks") -> Path:
    root = tmp_path / name
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


def _rc(tmp_path: Path, body: str) -> str:
    rc = tmp_path / "xschemrc"
    rc.write_text(body, encoding="utf-8")
    return str(rc)


def test_no_pdk_means_no_pdk_block(tmp_path, argv_log, monkeypatch):
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.delenv("PDK_ROOT", raising=False)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    report = run_netlist(
        schematic,
        str(tmp_path / "b.spice"),
        xschem_binary=binary,
        rcfile=_rc(tmp_path, "set PDK_ROOT /somewhere\n"),
    )
    assert "pdk" not in report


def test_pdk_root_mismatch_is_surfaced(tmp_path, argv_log):
    root = _fake_pdk_root(tmp_path)
    other = _fake_pdk_root(tmp_path, "other")
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    report = run_netlist(
        schematic,
        str(tmp_path / "b.spice"),
        xschem_binary=binary,
        rcfile=_rc(tmp_path, f"set PDK_ROOT {other}\n"),
        pdk="sky130A",
        pdk_root=str(root),
    )
    block = report["pdk"]
    assert block["consistent"] is False
    assert block["rcfile_pdk_root"] == os.path.realpath(other)
    assert "different PDKs" in block["warning"]
    assert report["status"] == "generated"


def test_pdk_root_match_and_parent_forms(tmp_path, argv_log):
    root = _fake_pdk_root(tmp_path)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    for decl in (
        f"set PDK_ROOT {root}",
        f'set ::env(PDK_ROOT) "{root}"',
        f"set env(PDK_ROOT) {root / 'sky130A'}",
    ):
        report = run_netlist(
            schematic,
            str(tmp_path / "b.spice"),
            xschem_binary=binary,
            rcfile=_rc(tmp_path, decl + "\n"),
            pdk_root=str(root),
        )
        assert report["pdk"]["consistent"] is True, decl
        assert report["pdk"]["warning"] is None


def test_pdk_root_wrong_variant_same_install_root_is_inconsistent(tmp_path, argv_log):
    root = tmp_path / "pdks"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    (root / "sky130B" / "libs.tech").mkdir(parents=True)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    report = run_netlist(
        schematic,
        str(tmp_path / "b.spice"),
        xschem_binary=binary,
        rcfile=_rc(tmp_path, f"set PDK_ROOT {root / 'sky130A'}\n"),
        pdk="sky130B",
        pdk_root=str(root),
    )
    block = report["pdk"]
    assert block["consistent"] is False
    assert "different PDKs" in block["warning"]


def test_pdk_root_declared_ancestor_of_install_root_is_inconsistent(tmp_path, argv_log):
    root = _fake_pdk_root(tmp_path)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    report = run_netlist(
        schematic,
        str(tmp_path / "b.spice"),
        xschem_binary=binary,
        rcfile=_rc(tmp_path, f"set PDK_ROOT {root.parent}\n"),
        pdk_root=str(root),
    )
    block = report["pdk"]
    assert block["consistent"] is False
    assert "different PDKs" in block["warning"]


def test_undeclared_or_computed_rcfile_root_is_not_compared(tmp_path, argv_log):
    root = _fake_pdk_root(tmp_path)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    for body in ("# nothing\n", "set PDK_ROOT $env(HOME)/pdks\n"):
        report = run_netlist(
            schematic,
            str(tmp_path / "b.spice"),
            xschem_binary=binary,
            rcfile=_rc(tmp_path, body),
            pdk_root=str(root),
        )
        assert report["pdk"]["consistent"] is None
        assert report["pdk"]["rcfile_pdk_root"] is None


def test_explicit_unresolvable_pdk_is_an_error(tmp_path, argv_log):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    with pytest.raises(NetlistError):
        run_netlist(
            schematic,
            str(tmp_path / "b.spice"),
            xschem_binary=binary,
            pdk_root=str(tmp_path / "nope"),
        )


def test_cli_text_prints_pdk_mismatch_warning(tmp_path, argv_log, capsys):
    root = _fake_pdk_root(tmp_path)
    other = _fake_pdk_root(tmp_path, "other")
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    code = main(
        [
            "netlist", schematic, "-o", str(tmp_path / "b.spice"),
            "--xschem-binary", binary,
            "--rcfile", _rc(tmp_path, f"set PDK_ROOT {other}\n"),
            "--pdk-root", str(root),
        ]
    )  # fmt: skip
    assert code == 0
    assert "pdk: WARNING:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Block mode: an includable .subckt (issue #2680, edge 1)
# ---------------------------------------------------------------------------

#: What a real `xschem -n -s -q -x -r` run against a trivial block emits when
#: netlisted as a testbench (issue #2680's own framing): the top-level
#: `.subckt`/`.ends` pair commented out, plus a trailing `.end`.
_TESTBENCH_SHAPED_BODY = (
    "* stub netlist\n**.subckt block a b\n.something x\n**.ends\n.end"
)


def test_block_mode_uncomments_subckt_and_drops_trailing_end(
    tmp_path, argv_log, monkeypatch
):
    monkeypatch.setenv("NETLIST_BODY", _TESTBENCH_SHAPED_BODY)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    report = run_netlist(schematic, str(output), xschem_binary=binary, block=True)

    written = output.read_text(encoding="utf-8")
    assert written == "* stub netlist\n.subckt block a b\n.something x\n.ends\n"
    # includable: no trailing .end to terminate an including testbench deck.
    assert ".end" not in written.splitlines()

    block = report["block"]
    assert block == {
        "requested": True,
        "subckt_uncommented": True,
        "ends_uncommented": True,
        "trailing_end_removed": True,
        "warning": None,
    }


def test_without_block_flag_testbench_shape_is_unchanged(
    tmp_path, argv_log, monkeypatch
):
    """Regression: `--block` is off by default, so the raw (commented
    subckt/ends, trailing .end) testbench export is unaffected."""
    monkeypatch.setenv("NETLIST_BODY", _TESTBENCH_SHAPED_BODY)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    report = run_netlist(schematic, str(output), xschem_binary=binary)

    assert output.read_text(encoding="utf-8") == _TESTBENCH_SHAPED_BODY + "\n"
    assert "block" not in report


def test_block_mode_warns_when_not_testbench_shaped(tmp_path, argv_log, monkeypatch):
    """A schematic that was not exported in the commented-header/trailing-.end
    shape is passed through unchanged, with the gap reported as a warning
    rather than failing the run."""
    # _NETLIST_BODY is already bare .subckt/.ends -- no commented header.
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    report = run_netlist(schematic, str(output), xschem_binary=binary, block=True)

    block = report["block"]
    assert block["subckt_uncommented"] is False
    assert block["ends_uncommented"] is False
    # _NETLIST_BODY does end with ".end", so that part is still removed.
    assert block["trailing_end_removed"] is True
    assert "no commented '**.subckt'" in block["warning"]
    assert "no commented '**.ends'" in block["warning"]
    assert report["status"] == "generated"


def test_block_mode_applies_before_check_diff(tmp_path, argv_log, monkeypatch):
    """Under `--check`, the committed artifact is the block-shaped netlist,
    so the comparison must run on the transformed text, not xschem's raw
    testbench-shaped export."""
    monkeypatch.setenv("NETLIST_BODY", _TESTBENCH_SHAPED_BODY)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "* stub netlist\n.subckt block a b\n.something x\n.ends\n", encoding="utf-8"
    )

    report = run_netlist(
        schematic, str(output), xschem_binary=binary, check=True, block=True
    )

    assert report["status"] == "match"
    assert report["drift"]["diff"] == []


def test_cli_text_prints_block_summary_and_warning(
    tmp_path, argv_log, capsys, monkeypatch
):
    # _NETLIST_BODY (the argv_log fixture's default) is bare .subckt/.ends
    # with no commented header, so block mode warns rather than failing.
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)

    code = main(
        [
            "netlist", schematic, "-o", str(tmp_path / "b.spice"),
            "--xschem-binary", binary,
            "--block",
        ]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert "block: subckt_uncommented=False" in out
    assert "block: WARNING:" in out
