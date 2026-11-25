"""Shared helpers for the release-workflow guards (issue #5005).

The comment-stripped executable view of ``release.yml``, job slicing, and the
pass/fail ``Result`` collector that ``_release_workflow_checks.py`` (the
publishing-path identity checks) and ``test_release_workflow_tag_job.py`` (the
tag job) both build on. Split out of ``test_release_workflow_identity.py``,
which outgrew the per-file size ratchet.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
GATE = ROOT / "scripts" / "version-bump-gate.sh"
PUBLISH_JOB = "publish-bump"
BUMP_JOB = "bump"
TAG_JOB = "tag"


def strip_comments(text: str) -> list[str]:
    """The executable view of the workflow: full-line and trailing ``#``
    comments blanked/cut (outside quotes; a ``#`` glued to a word stays),
    line count preserved so diagnostics point at the real file."""
    out = []
    for line in text.split("\n"):
        buf = ""
        in_single = in_double = False
        i = 0
        while i < len(line):
            ch = line[i]
            if in_double and ch == "\\" and i + 1 < len(line):
                buf += ch + line[i + 1]
                i += 2
                continue
            if ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "'" and not in_double:
                in_single = not in_single
            elif ch == "#" and not in_single and not in_double:
                if i == 0 or line[i - 1] in " \t":
                    break
            buf += ch
            i += 1
        out.append(buf.rstrip(" \t"))
    return out


def job_block(code: list[str], job: str) -> list[str]:
    """The executable lines of one job, from ``  <job>:`` under ``jobs:`` to
    the next job (or EOF)."""
    lines = []
    in_jobs = False
    started = False
    for line in code:
        if line == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line == f"  {job}:":
            started = True
            continue
        if started and re.match(r"^  [A-Za-z0-9_-]+:$", line):
            break
        if started:
            lines.append(line)
    return lines


@dataclass
class Result:
    failures: list[tuple[str, str]] = field(default_factory=list)
    passes: list[str] = field(default_factory=list)

    def check(self, ok: bool, case: str, detail: str = "") -> None:
        if ok:
            self.passes.append(case)
        else:
            self.failures.append((case, detail))


def line_of(code: list[str], needle: str) -> int | None:
    for n, line in enumerate(code, 1):
        if needle in line:
            return n
    return None


def comment_out_first(text: str, needle: str) -> str:
    out = []
    done = False
    for line in text.split("\n"):
        if not done and needle in line and not line.lstrip().startswith("#"):
            out.append("# " + line)
            done = True
        else:
            out.append(line)
    assert done, f"no executable line contains {needle!r} -- stale control"
    return "\n".join(out)


def expect_failure(
    checks: Callable[[str], Result], mutation: str, needle_case: str
) -> None:
    """``checks`` must report a failure whose case id starts with ``needle_case``."""
    result = checks(mutation)
    assert any(case.startswith(needle_case) for case, _ in result.failures), (
        f"mutation was not caught by any check starting with {needle_case!r};"
        f" failures were: {[c for c, _ in result.failures]}"
    )
