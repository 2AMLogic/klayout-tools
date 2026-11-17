"""Native-crash stage diagnostics for ``klt lvs`` (issue #5630).

Issue #5630 reports a deterministic SIGSEGV (exit 139, empty stdout and
stderr) on a routed ~1070-instance ASAP7 block under the documented
"Routed multi-cell blocks" recipe (``finfet_bulk`` + ``flatten_layout`` +
``flatten_reference``), where each half of the recipe completes. A native
crash cannot be caught in-process, so
:mod:`klayout_tools.lvs_crash_diagnostics` makes faulthandler dump the
Python traceback (naming the ``klt_lvs_stage_*`` wrappers) to stderr, and
these tests pin:

* the stage wrapper really names its stage in a crash dump (a subprocess
  kills itself with SIGSEGV inside a stage, like the reported crash);
* ``klt lvs`` enables the diagnostics before running;
* the three-option recipe itself completes to a verdict on a routed
  multi-type synthetic block at the issue's scale -- the no-repro record:
  44 cell types x 1056 instances built from the pinned corpus. It never
  crashed here (``match``), which is why the fix is diagnostics, not a
  located call. See ``docs/cli/lvs.md``'s #5630 note. The block fixture
  lives in ``helpers/asap7_routed_chain.py`` (test-scoped, not shipped).
"""

from __future__ import annotations

import signal
import subprocess
import sys
import traceback
from argparse import Namespace
from pathlib import Path

import pytest

from helpers.asap7_routed_chain import GDS as _GDS
from helpers.asap7_routed_chain import build_routed_chain
from klayout_tools.lvs_crash_diagnostics import current_stage, stage

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

_corpus = pytest.mark.skipif(
    not _GDS.is_file(),
    reason="pinned lambdapdk ASAP7 libraries not found under pdks/lambdapdk "
    "-- run scripts/fetch-pdks.sh to enable this test",
)


def test_stage_tracks_current_and_restores():
    @stage("outer")
    def outer():
        assert current_stage() == "outer"

        @stage("inner")
        def inner():
            assert current_stage() == "inner"

        inner()
        assert current_stage() == "outer"
        with pytest.raises(ValueError):
            inner_raise()
        assert current_stage() == "outer"

    @stage("inner")
    def inner_raise():
        raise ValueError("boom")

    assert current_stage() is None
    outer()
    assert current_stage() is None


def test_stage_names_its_frames_in_traceback():
    @stage("flatten_layout_combine")
    def explodes():
        raise RuntimeError("native-crash stand-in")

    with pytest.raises(RuntimeError) as caught:
        explodes()
    text = "\n".join(traceback.format_exception(caught.value))
    assert "klt_lvs_stage_flatten_layout_combine" in text


def test_native_crash_dumps_stage_named_traceback(tmp_path):
    """A real SIGSEGV (the reported failure mode) must leave a stderr
    traceback naming the stage -- not two empty streams."""
    script = tmp_path / "crash.py"
    script.write_text(
        "import os, signal, sys\n"
        f"sys.path.insert(0, {str(_REPOSITORY_ROOT / 'src')!r})\n"
        "from klayout_tools.lvs_crash_diagnostics import enable, stage\n"
        "assert enable()\n"
        "\n"
        "@stage('finfet_bulk_join')\n"
        "def native_call():\n"
        "    os.kill(os.getpid(), signal.SIGSEGV)\n"
        "\n"
        "native_call()\n"
    )
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == -signal.SIGSEGV
    assert proc.stdout == ""
    assert "Segmentation fault" in proc.stderr
    assert "klt_lvs_stage_finfet_bulk_join" in proc.stderr


def test_lvs_cmd_enables_crash_diagnostics(monkeypatch, capsys):
    from klayout_tools.cli import lvs_cmd

    calls: list[bool] = []
    monkeypatch.setattr(lvs_cmd, "enable_crash_diagnostics", lambda: calls.append(True))

    def refuse(_request, **_kwargs):
        raise lvs_cmd.LvsError("request refused (test)")

    monkeypatch.setattr(lvs_cmd, "run_lvs", refuse)
    exit_code = lvs_cmd.run(
        Namespace(
            request="irrelevant",
            check=None,
            rerun=False,
            layout=None,
            deck=None,
            deck_file=None,
            deck_var=None,
            progress=None,
            format="text",
            output=None,
        )
    )
    assert exit_code == 1
    assert calls == [True]


@_corpus
def test_routed_multitype_block_recipe_reaches_verdict(tmp_path):
    """The #4610 three-option recipe on a routed 44-type, 1056-instance
    block (the issue's scale) must reach a verdict, never die silently.
    This is the no-repro record for #5630: measured ``match``."""
    from klayout_tools.lvs import run_lvs

    request_path = build_routed_chain(tmp_path, n=1056)
    report = run_lvs(request_path.read_text())
    assert report["status"] == "match", report.get("category_counts")
    counts = report["category_counts"]
    assert counts["device.finfet_bulk_tied"] > 0
    assert counts["topology.flattened"] == 2
