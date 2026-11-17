"""`_run_yosys` pins ``COLUMNS`` in its Yosys child's environment (#5780).

Yosys 0.67 drives ``yosys-abc`` as a persistent interactive REPL over
pipes, one conversation per module: it writes ``empty\\nsource
<tmpdir>/yosys-abc-XXXXXX/abc.script\\n`` to abc's stdin, then reads abc's
stdout until it sees a prompt line whose remainder starts with ``source ``
(the ``seen_source_cmd`` handshake). abc's readline echoes that ``source``
command back. Because klt captures output, abc's stdout is a pipe, not a
tty, so readline cannot query a window size and falls back to 80 columns.
When the echoed line (``abc 01> `` + ``source `` + the script path =
``len(TMPDIR) + 43`` columns) reaches column 79, readline wraps it: the
echo arrives as ``abc 01> `` + ``\\r`` + only the *tail* of the path, with
no ``source `` token anywhere. The handshake can never arm; yosys blocks on
``read`` forever while abc polls stdin -- both sides wait until klt's 300 s
watchdog kills the run. 79 - 43 = 36: the ``TMPDIR`` length at which this
flips, matching the reports on #5780/#5798/#5830 to the character.

The fix: `_run_yosys` hands the spawn primitive an explicit ``env`` that is
a *copy* of ``os.environ`` with ``COLUMNS`` pinned to a generous width, so
the echo can never wrap regardless of ``$TMPDIR`` length. The pin is
applied unconditionally -- an inherited ``COLUMNS=80`` from an interactive
shell is exactly the failing case, so a pre-set ``COLUMNS`` must not win.

These tests stub the spawn primitive (``subprocess.run`` today; ``Popen``
if #5798 lands second and rebases this call) and assert on the ``env``
keyword `_run_yosys` passes, so they stay correct across that refactor.
"""

from __future__ import annotations

import os

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import synthesize

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _record_yosys_env(monkeypatch) -> list:
    """Stub the spawn primitive; record the ``env`` each ``yosys -s`` got."""
    seen: list = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            seen.append(kwargs.get("env"))
        return fake_completed(returncode=0)

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)
    return seen


def test_run_yosys_pins_columns_above_the_wrap_cliff(tmp_path, monkeypatch):
    """The child env carries ``COLUMNS`` > 200 (the 80-column readline wrap
    is the defect; any pin comfortably above ``PATH_MAX`` removes it)."""
    seen = _record_yosys_env(monkeypatch)

    synthesize._run_yosys(str(tmp_path / "s.ys"))

    assert seen and len(seen) == 1
    env = seen[0]
    assert env is not None
    assert "COLUMNS" in env
    assert int(env["COLUMNS"]) > 200


def test_run_yosys_env_is_a_copy_of_os_environ(tmp_path, monkeypatch):
    """``PATH`` and the rest of klt's environment still reach Yosys: the pin
    must be built as a copy of ``os.environ``, never a replacement env."""
    monkeypatch.setenv("KLT_TEST_SENTINEL", "reaches-yosys")
    seen = _record_yosys_env(monkeypatch)

    synthesize._run_yosys(str(tmp_path / "s.ys"))

    env = seen[0]
    assert env["PATH"] == os.environ["PATH"]
    assert env["KLT_TEST_SENTINEL"] == "reaches-yosys"


def test_run_yosys_overrides_an_inherited_columns(tmp_path, monkeypatch):
    """An inherited ``COLUMNS=80`` (an interactive shell's export) must not
    win: the pin is applied unconditionally for the child."""
    monkeypatch.setenv("COLUMNS", "80")
    seen = _record_yosys_env(monkeypatch)

    synthesize._run_yosys(str(tmp_path / "s.ys"))

    env = seen[0]
    assert env["COLUMNS"] != "80"
    assert int(env["COLUMNS"]) > 200
