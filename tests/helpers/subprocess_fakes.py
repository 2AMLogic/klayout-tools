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


def fake_completed(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    """Build a stand-in `subprocess.CompletedProcess` for mocked subprocess calls."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )
