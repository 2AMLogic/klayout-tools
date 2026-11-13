"""Shared `subprocess.CompletedProcess` test double (issue #831).

`fake_completed()` stands in for the stdlib's `subprocess.CompletedProcess`
wherever a test stubs out `subprocess.run`/`subprocess.Popen` and only needs
`returncode`/`stdout`/`stderr` back. It replaces a dozen near-identical local
`class _FakeCompleted:` / `_FakeCompletedProcess` / `_FakeNetgenCompleted`
definitions that had drifted across the test suite -- most byte-identical,
a few dropping a default or a field.

Parameter order is `(stdout, returncode, stderr)` -- `stdout` first, *not*
`returncode` first -- to match the several call sites inherited from the old
`_FakeCompleted`/`_FakeNetgenCompleted` classes that pass a single positional
string argument (e.g. `_FakeCompleted("** ngspice-99\\n")`). Putting
`returncode` first would silently rebind that positional string to
`returncode` instead of `stdout`.
"""

from __future__ import annotations

import subprocess

from klayout_tools import sim


def fake_completed(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    """Build a stand-in `subprocess.CompletedProcess` for mocked subprocess calls."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


_STUB_VERSION_BANNER = "ngspice-99 : stub banner\n"


def _stub_subprocess_run(
    monkeypatch,
    *,
    log_text: str = "",
    stdout: str = "** ngspice-99\n",
    side_effect=None,
    returncode: int = 0,
):
    """Patch `subprocess.run` so `run_sim` runs without the ngspice binary."""
    real_run = subprocess.run

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None, **kw):
        if "--version" in cmd:  # the once-per-sweep engine_build probe (#3095)
            return fake_completed(_STUB_VERSION_BANNER)
        if "-o" not in cmd:
            # `subprocess.run` is patched on the shared module object, so calls
            # that are not the ngspice run this stub stands in for land here
            # too -- e.g. `pdk._git_run`'s `git -C <variant> rev-parse` probe
            # of a git-backed PDK variant. ngspice always gets `-o <log>`
            # (issue #5512).
            kw.update(capture_output=capture_output, text=text, timeout=timeout)
            return real_run(cmd, cwd=cwd, env=env, **kw)
        log_path = cmd[cmd.index("-o") + 1]
        if side_effect is not None:
            raise side_effect
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(log_text)
        return fake_completed(stdout, returncode=returncode)

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
