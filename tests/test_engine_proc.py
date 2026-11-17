"""run_in_group tears down the whole process tree (issue #5871)."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from klayout_tools._engine_proc import run_in_group

pytestmark = pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX only")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0); treat it as gone
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def _gone(pid: int, wait_s: float = 3.0) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _grandchild_script(tmp_path, tail: str):
    pidfile = tmp_path / "gc.pid"
    return ["sh", "-c", f"sleep 300 & echo $! > {pidfile}; {tail}"], pidfile


def test_timeout_kills_grandchild(tmp_path):
    argv, pidfile = _grandchild_script(tmp_path, "wait")
    with pytest.raises(subprocess.TimeoutExpired):
        run_in_group(argv, capture_output=True, text=True, timeout=1)
    assert _gone(int(pidfile.read_text()))


def test_normal_exit_leaves_no_group_member(tmp_path):
    argv, pidfile = _grandchild_script(tmp_path, "exit 3")
    # grandchild keeps stdout closed so run() returns promptly
    done = run_in_group(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert done.returncode == 3
    assert _gone(int(pidfile.read_text()))


def test_keyboard_interrupt_kills_group(tmp_path, monkeypatch):
    argv, pidfile = _grandchild_script(tmp_path, "wait")
    real_run = subprocess.run

    def interrupting_run(*a, **kw):
        try:
            return real_run(*a, **{**kw, "timeout": 1})
        except subprocess.TimeoutExpired:
            raise KeyboardInterrupt from None

    monkeypatch.setattr(subprocess, "run", interrupting_run)
    with pytest.raises(KeyboardInterrupt):
        run_in_group(argv, capture_output=True)
    assert _gone(int(pidfile.read_text()))


def test_missing_binary_still_raises_oserror():
    with pytest.raises(OSError):
        run_in_group(["definitely-not-a-binary-5871"], capture_output=True)


def test_mocked_run_is_untouched(monkeypatch):
    sentinel = subprocess.CompletedProcess(["x"], 0, "", "")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: sentinel)
    assert run_in_group(["x"]) is sentinel
