"""Regression test: `kb-drift-canary.yml` must be able to go red (issue #6678).

The workflow's only check step pipes `klt kb validate --recheck-measured`
into `tee kb-drift-canary.json`. A step with no `shell:` key runs as
`bash -e {0}`, which does NOT enable `pipefail`, so the pipeline's status is
`tee`'s (0) and `klt kb validate`'s exit 3 ("ran successfully but found one
or more invalid entries") was swallowed -- the nightly job stayed green no
matter how far a `kb/` measured figure drifted. A comment in the step
asserted the opposite ("GitHub Actions' default bash shell runs with
`-eo pipefail`"), and that comment is also what let #6463's file-level
`grep -q pipefail` audit file the workflow as already defended.

So these tests deliberately do NOT ask "does the word `pipefail` appear in
the file". They slice out *this step's* `run:` block as text (the dev extra
has no YAML library -- same approach as `test_ci_pdk_provisioning.py`),
execute it under the shell GitHub Actions would actually pick for it, with a
stub `uv` that emits a JSON report and exits 3, and assert on **exit
status**: is `pipefail` in effect for this block, observably.

The contrast case strips the `set -o pipefail` line and runs the block under
plain `bash -e` (what a `shell:`-less step gets) and asserts it exits 0 --
proving the test would have caught the original bug rather than passing
vacuously.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "kb-drift-canary.yml"

STEP_NAME = "Recheck kb/ measured figures against a fresh klt sim"

#: The exit status the stub `klt kb validate` reports: exit 3 is documented
#: in docs/cli/kb.md as "ran successfully but found one or more invalid
#: entries" -- i.e. drift.
STUB_EXIT = 3

#: What the stub prints as the JSON report; `tee` must still capture it.
STUB_REPORT = '{"ok": false, "drift": true}'

#: GitHub Actions' documented shell invocations for `bash`. With no
#: `shell:` key the step runs `bash -e {0}` (no pipefail); an explicit
#: `shell: bash` runs `bash --noprofile --norc -eo pipefail {0}`.
DEFAULT_SHELL = ["bash", "-e"]
SHELLS = {
    None: DEFAULT_SHELL,
    "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail"],
}

#: `set -o pipefail`, `set -eo pipefail`, `set -euo pipefail`, ... as a command.
_PIPEFAIL_LINE = re.compile(r"^\s*set\s+-\w*o\s+pipefail\b.*$")


def _step_text(workflow_text: str, name: str) -> str:
    """The workflow-file text of the step named *name*, comments included."""
    lines = workflow_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^\s+- name: {re.escape(name)}\s*$", line):
            start = i
            break
    assert start is not None, f"step {name!r} not found in {WORKFLOW.name}"
    step_indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line.strip() and len(line) - len(line.lstrip()) <= step_indent:
            end = j
            break
    return "\n".join(lines[start:end]) + "\n"


def _run_block(step: str) -> str:
    """The dedented body of the step's `run: |` block -- the shell text only.

    Comments in the YAML *above* `run:` (where the step documents itself) are
    not part of this text, so a comment mentioning `pipefail` cannot satisfy
    an assertion about the command.
    """
    lines = step.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s+)run: \|\s*$", line)
        if m:
            key_indent = len(m.group(1))
            body: list[str] = []
            for body_line in lines[i + 1 :]:
                if (
                    body_line.strip()
                    and len(body_line) - len(body_line.lstrip()) <= key_indent
                ):
                    break
                body.append(body_line)
            indent = key_indent + 2
            return (
                "\n".join(b[indent:] if b.strip() else "" for b in body).strip("\n")
                + "\n"
            )
    raise AssertionError(f"no `run: |` block in step {STEP_NAME!r}")


def _step_shell(step: str) -> str | None:
    """The step's `shell:` value, or None when it has no `shell:` key."""
    m = re.search(r"^\s+shell:\s*(\S+)\s*$", step, re.M)
    return m.group(1) if m else None


def _strip_pipefail(block: str) -> str:
    """The block with every `set -o pipefail`-style line removed."""
    return "".join(
        line
        for line in block.splitlines(keepends=True)
        if not _PIPEFAIL_LINE.match(line.rstrip("\n"))
    )


def _run(
    block: str, shell: list[str], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Run *block* as GitHub does -- written to a file, passed to *shell* --
    with a stub `uv` that logs its argv, prints a JSON report, exits 3."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "uv-argv.txt"
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >"{argv_log}"\n'
        f"echo '{STUB_REPORT}'\n"
        f"exit {STUB_EXIT}\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)

    cwd = tmp_path / "work"
    cwd.mkdir()
    script = tmp_path / "step.sh"
    script.write_text(block)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        [*shell, str(script)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture(scope="module")
def step() -> str:
    return _step_text(WORKFLOW.read_text(), STEP_NAME)


@pytest.fixture(scope="module")
def block(step: str) -> str:
    return _run_block(step)


def test_run_block_invokes_recheck_measured_through_tee(block: str) -> None:
    """Sanity-check the slice is the step we mean (guards the other tests
    against silently exercising an empty or wrong block)."""
    assert "klt kb validate --recheck-measured" in block
    assert "--format json" in block
    assert "| tee kb-drift-canary.json" in block


def test_pipefail_is_a_command_in_the_run_block(block: str) -> None:
    """`set -o pipefail` is the first command of the block -- as a shell line,
    not a word in a comment (the #6463 audit's error)."""
    first = next(line for line in block.splitlines() if line.strip())
    assert _PIPEFAIL_LINE.match(first), f"first command of run block is {first!r}"


def test_drift_exit_status_fails_the_step(
    step: str, block: str, tmp_path: Path
) -> None:
    """The post-fix contract: `klt kb validate` exiting 3 under the `tee`
    pipe fails the step (nonzero), under the shell GitHub picks for it."""
    shell_key = _step_shell(step)
    assert shell_key in SHELLS, (
        f"step uses `shell: {shell_key}`; teach this test how GitHub invokes it "
        "and whether pipefail is on, rather than assuming"
    )
    proc = _run(block, SHELLS[shell_key], tmp_path)
    assert proc.returncode != 0, (
        f"step exited {proc.returncode} though klt exited {STUB_EXIT}: "
        "the canary cannot go red\n"
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert proc.returncode == STUB_EXIT


def test_report_still_written_when_klt_fails(
    step: str, block: str, tmp_path: Path
) -> None:
    """`tee` still runs on a red step, so the `if: always()` artifact upload
    has the drift report to collect."""
    _run(block, SHELLS[_step_shell(step)], tmp_path)
    report = tmp_path / "work" / "kb-drift-canary.json"
    assert report.is_file()
    assert report.read_text().strip() == STUB_REPORT
    argv = (tmp_path / "uv-argv.txt").read_text()
    assert "klt kb validate --recheck-measured --format json" in argv


def test_without_pipefail_the_same_block_goes_green(block: str, tmp_path: Path) -> None:
    """The pre-fix contrast: strip the `set -o pipefail` line and run under
    plain `bash -e` (what a `shell:`-less step gets) -- the stubbed exit 3 is
    swallowed and the step exits 0, with the report still written. This is
    the bug in #6678; it proves the assertions above can fail."""
    unpatched = _strip_pipefail(block)
    assert unpatched != block, "block has no `set -o pipefail` line to strip"
    proc = _run(unpatched, DEFAULT_SHELL, tmp_path)
    assert proc.returncode == 0
    assert (
        tmp_path / "work" / "kb-drift-canary.json"
    ).read_text().strip() == STUB_REPORT


def test_upload_step_runs_always_and_collects_the_report() -> None:
    """The artifact upload stays `if: always()` over `kb-drift-canary.json`,
    so a red run still uploads its drift report."""
    text = WORKFLOW.read_text()
    upload = text[text.rindex("- uses: actions/upload-artifact") :]
    assert "if: always()" in upload
    assert "path: kb-drift-canary.json" in upload
