"""Wasm-trap hint tests for the shared yosys error formatters (issue #5804).

Moved out of ``test_equiv.py``/``test_provenance.py``/
``test_synthesize.py`` because those files are under the per-file size
ratchet; the formatters themselves now live in
``klayout_tools._yosys_error_messages``.
"""

from __future__ import annotations

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import synthesize
from klayout_tools._yosys_error_messages import (
    _yosys_error_message,
    wasm_trap_hint_if_applicable,
)
from klayout_tools.synthesize import SynthesizeError, run_synthesize
from test_synthesize import _ABC_HELP_WITH_DONT_USE, _setup_success_env

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


# --------------------------------------------------------------------------- #
# wasm_trap_hint_if_applicable (issue #5804)
# --------------------------------------------------------------------------- #
#
# A WASI/wasm yosys build (e.g. yowasp-yosys) runs inside a wasm32 guest
# whose linear memory is capped at ~4 GiB; a heavy `sat -seq <depth>` query
# over a large flattened netlist can exceed that ceiling and die as a bare
# wasmtime trap -- no yosys `ERROR:` line anywhere. The helper is a pure
# string test over the captured output, like the WASI-sandbox hint.
# Precedence between the trap hint and a yosys `ERROR:` line lives at the
# call sites (both formatters consult this helper only in their
# no-`ERROR:` tail branch), so the helper itself is signature-only by
# design: a trap co-occurring with an `ERROR:` line still matches here.

_WASM_TRAP_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/usr/local/bin/yowasp-yosys", line 8, in <module>\n'
    "    sys.exit(main())\n"
    "wasmtime._trap.Trap: unreachable\n"
    "\n"
    "Caused by:\n"
    "    thrown Wasm exception\n"
)


@pytest.mark.parametrize(
    ("output", "expect_hint"),
    [
        (_WASM_TRAP_TRACEBACK, True),
        (_WASM_TRAP_TRACEBACK + "ERROR: Prove terminated abnormally.\n", True),
        ("wasmtime._trap.Trap: unreachable\n", True),
        ("Caused by:\n    thrown Wasm exception\n", True),
        ("ERROR: Prove terminated abnormally.\n", False),
        ("warning: Dumping selected design.\n", False),
        ("", False),
    ],
    ids=[
        "trap_traceback",
        "trap_plus_error_line_is_signature_only",
        "trap_class_alone",
        "caused_by_line_alone",
        "error_line_only",
        "unrelated_stderr",
        "empty",
    ],
)
def test_wasm_trap_hint_if_applicable(output, expect_hint):
    hint = wasm_trap_hint_if_applicable(output)
    if expect_hint:
        assert hint
        assert "wasm" in hint.lower()
        assert "4 GiB" in hint
        assert "request.yosys_binary" in hint
        assert "KLT_YOSYS_BINARY" in hint
    else:
        assert hint == ""


# --------------------------------------------------------------------------- #
# equiv's _yosys_error_message (issue #5804)
# --------------------------------------------------------------------------- #


def test_yosys_error_message_wasm_trap_hint():
    """A trap traceback with no yosys ``ERROR:`` line gets the wasm memory
    ceiling hint naming the native-build remedy -- issue #5804."""
    message = _yosys_error_message("", _WASM_TRAP_TRACEBACK, 1)

    assert message.startswith("yosys exited with code 1:")
    assert "wasm" in message.lower()
    assert "4 GiB" in message
    assert "request.yosys_binary" in message
    assert "KLT_YOSYS_BINARY" in message


def test_yosys_error_message_wasm_trap_on_stdout_hint():
    """The trap signature on stdout (not stderr) is still recognised."""
    message = _yosys_error_message(_WASM_TRAP_TRACEBACK, "", 1)

    assert message.startswith("yosys exited with code 1:")
    assert "4 GiB" in message
    assert "KLT_YOSYS_BINARY" in message


def test_yosys_error_message_error_line_still_wins_over_trap():
    """A run carrying both a trap traceback and a yosys ``ERROR:`` line
    keeps today's ``ERROR:``-line message -- no trap hint appended."""
    stderr = _WASM_TRAP_TRACEBACK + "ERROR: Prove terminated abnormally.\n"

    message = _yosys_error_message("", stderr, 1)

    assert message == (
        "yosys equivalence check failed: ERROR: Prove terminated abnormally."
    )
    assert "4 GiB" not in message


def test_yosys_error_message_unrelated_tail_unchanged():
    """Output with neither a trap signature nor an ``ERROR:`` line keeps
    today's tail-of-output message verbatim."""
    stderr = "warning: Dumping selected design.\nsecond line\nthird line\n"

    message = _yosys_error_message("", stderr, 2)

    assert message == (
        "yosys exited with code 2: warning: Dumping selected design."
        " second line third line"
    )


def test_yosys_error_message_empty_output_unchanged():
    """Empty captured output keeps the ``no output captured`` path, and no
    hint is appended (the trap signature cannot be present)."""
    message = _yosys_error_message("", "", 1)

    assert message == "yosys exited with code 1: no output captured"


# --------------------------------------------------------------------------- #
# synthesize's _synthesis_error_message via run_synthesize (issue #5804)
# --------------------------------------------------------------------------- #


def test_run_synthesize_stubbed_wasm_trap_hint(tmp_path, monkeypatch):
    """A trap traceback with no yosys ``ERROR:`` line gets the wasm memory
    ceiling hint naming the native-build remedy -- issue #5804."""
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
        assert cmd[:2] == ["yosys", "-s"]
        return fake_completed(returncode=1, stderr=_WASM_TRAP_TRACEBACK)

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError) as exc_info:
        run_synthesize(request_path)

    message = str(exc_info.value)
    assert message.startswith("yosys exited with code 1:")
    assert "wasm" in message.lower()
    assert "4 GiB" in message
    assert "request.yosys_binary" in message
    assert "KLT_YOSYS_BINARY" in message


def test_run_synthesize_stubbed_wasm_trap_on_stdout_hint(tmp_path, monkeypatch):
    """The trap signature on stdout (not stderr) is still recognised."""
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
        assert cmd[:2] == ["yosys", "-s"]
        return fake_completed(returncode=1, stdout=_WASM_TRAP_TRACEBACK)

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError) as exc_info:
        run_synthesize(request_path)

    message = str(exc_info.value)
    assert message.startswith("yosys exited with code 1:")
    assert "4 GiB" in message
    assert "KLT_YOSYS_BINARY" in message


def test_run_synthesize_stubbed_error_line_still_wins_over_trap(tmp_path, monkeypatch):
    """A run carrying both a trap traceback and a yosys ``ERROR:`` line
    keeps today's ``ERROR:``-line message -- no trap hint appended."""
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
        assert cmd[:2] == ["yosys", "-s"]
        return fake_completed(
            returncode=1,
            stderr=_WASM_TRAP_TRACEBACK + "ERROR: Prove terminated abnormally.\n",
        )

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError) as exc_info:
        run_synthesize(request_path)

    message = str(exc_info.value)
    assert message == ("yosys synthesis failed: ERROR: Prove terminated abnormally.")
    assert "4 GiB" not in message


def test_run_synthesize_stubbed_unrelated_tail_unchanged(tmp_path, monkeypatch):
    """Output with neither a trap signature nor an ``ERROR:`` line keeps
    today's tail-of-output message verbatim."""
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
        assert cmd[:2] == ["yosys", "-s"]
        return fake_completed(
            returncode=2,
            stderr="warning: Dumping selected design.\nsecond line\nthird line\n",
        )

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError) as exc_info:
        run_synthesize(request_path)

    assert str(exc_info.value) == (
        "yosys exited with code 2: warning: Dumping selected design."
        " second line third line"
    )


def test_run_synthesize_stubbed_empty_output_unchanged(tmp_path, monkeypatch):
    """Empty captured output keeps the ``no output captured`` path, and no
    hint is appended (the trap signature cannot be present)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
        assert cmd[:2] == ["yosys", "-s"]
        return fake_completed(returncode=1)

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError) as exc_info:
        run_synthesize(request_path)

    assert str(exc_info.value) == "yosys exited with code 1: no output captured"
