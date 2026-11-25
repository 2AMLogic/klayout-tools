"""Run ``release.yml``'s ``tag`` job steps for real (issue #5005).

Executes the workflow's OWN ``run:`` blocks (and evaluates its OWN ``if:``
conditions) against a local bare origin and a stub ``gh``. Shared by
``test_release_workflow_tag_job.py`` (tag / dispatch-recovery behaviour) and
``test_release_workflow_tag_report.py`` (failure reporting). Zero GitHub or PyPI
writes: ``gh`` is a stub and the origin is a directory under ``tmp_path``.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

from _release_workflow_support import TAG_JOB, WORKFLOW, job_block

RESOLVE = "Resolve the release this green commit carries"
NATIVE = "Gate on native extras"
TAGGING = "Tag the green commit and dispatch publish"
REPORT = "Report a failed tag job (single tracking issue)"
DISPATCH = "gh workflow run publish.yml --repo o/r --ref v0.9.3"


def workflow_steps() -> dict[str, list[str]]:
    """Step name -> raw lines, for the tag job."""
    steps: dict[str, list[str]] = {}
    current: list[str] = []
    for line in job_block(WORKFLOW.read_text().split("\n"), TAG_JOB):
        named = re.match(r"^      - name: (.+)$", line)
        if named:
            current = steps.setdefault(named.group(1), [])
        elif re.match(r"^      - ", line):
            current = []
        current.append(line)
    return steps


def step_script(step: list[str]) -> str:
    start = next(n for n, line in enumerate(step) if re.match(r"^ +run: \|$", line))
    indent = len(step[start]) - len(step[start].lstrip())
    body = []
    for line in step[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def step_if(step: list[str]) -> str | None:
    for line in step:
        found = re.match(r"^ +if: (.+)$", line)
        if found:
            return found.group(1)
    return None


def step_env(step: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    in_env = False
    for line in step:
        if re.match(r"^ {8}env:$", line):
            in_env = True
        elif in_env:
            entry = re.match(r"^ {10}(\w+): (.+)$", line)
            if not entry:
                break
            env[entry.group(1)] = entry.group(2)
    return env


def truthy(
    expr: str | None, outputs: dict[str, dict[str, str]], failed: bool = False
) -> bool:
    """Evaluate the ``if:`` shapes the tag job uses (``steps.X.outputs.Y ==|!=
    'lit'`` and ``failure()``, joined by ``&&`` / ``||``); anything else is a
    loud test error. ``failed``: an earlier step of the job failed."""
    if expr is None:
        return True

    def clause(text: str) -> bool:
        if text.strip() == "failure()":
            return failed
        m = re.fullmatch(
            r"\s*steps\.(\w+)\.outputs\.(\w+)\s*(==|!=)\s*'([^']*)'\s*", text
        )
        assert m, f"unsupported if: clause {text!r}; teach truthy about it"
        got = outputs.get(m.group(1), {}).get(m.group(2), "")
        return (got == m.group(4)) == (m.group(3) == "==")

    return any(all(clause(c) for c in alt.split("&&")) for alt in expr.split("||"))


def expand(
    text: str, outputs: dict[str, dict[str, str]], outcomes: dict[str, str]
) -> str:
    """``${{ steps.X.outputs.Y }}`` and ``${{ steps.X.outcome }}`` (a step that
    has not run is ``skipped``, as on a runner)."""
    text = re.sub(
        r"\$\{\{\s*steps\.(\w+)\.outputs\.(\w+)\s*\}\}",
        lambda m: outputs.get(m.group(1), {}).get(m.group(2), ""),
        text,
    )
    return re.sub(
        r"\$\{\{\s*steps\.(\w+)\.outcome\s*\}\}",
        lambda m: outcomes.get(m.group(1), "skipped"),
        text,
    )


@dataclass
class Flow:
    outputs: dict[str, dict[str, str]]
    resolve: subprocess.CompletedProcess
    native_ran: bool = False
    tag: subprocess.CompletedProcess | None = None
    report: subprocess.CompletedProcess | None = None


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@e.x", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def origin(tmp_path: Path, version: str = "0.9.3") -> tuple[Path, str]:
    """A bare origin whose main carries ``base-version = version``."""
    bare, seed = tmp_path / "origin.git", tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "pyproject.toml").write_text(
        f'[tool.hatch.version]\nbase-version = "{version}"\n'
    )
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", f"chore: bump version to {version}")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    return bare, git(seed, "rev-parse", "HEAD")


def clone(tmp_path: Path, bare: Path, name: str) -> Path:
    """A fresh full-history checkout with tags (the job's `fetch-depth: 0`)."""
    work = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    return work


def push_tag(tmp_path: Path, bare: Path, sha: str, tag: str) -> None:
    work = clone(tmp_path, bare, f"tagger-{tag}")
    git(work, "tag", "-a", tag, "-m", tag, sha)
    git(work, "push", "-q", "origin", f"refs/tags/{tag}")


def tags(bare: Path) -> str:
    return git(bare, "for-each-ref", "refs/tags")


def reject_pushes(bare: Path) -> None:
    """Make the origin refuse every push (a failed tag push, not a failed dispatch)."""
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'remote: rejected (test)' >&2\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)


def stub_gh(tmp_path: Path) -> Path:
    """`gh run list` prints $GH_RUN_COUNT; `gh workflow run` succeeds; both can
    be made to fail. `gh issue list` applies the step's own ``--jq`` to
    $GH_ISSUES_JSON; `gh issue create|edit` record their argv. Every call is
    logged to ``gh.log`` (one line per call)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [ "$1" = issue ]; then
              echo "gh issue $2" >> "@LOG@"
              case "$2" in
                list)
                  expr=""; prev=""
                  for a in "$@"; do [ "$prev" = --jq ] && expr="$a"; prev="$a"; done
                  printf '%s' "${GH_ISSUES_JSON:-[]}" | jq -r "$expr" ;;
                create|edit) printf '%s\\0' "$@" > "@DIR@/issue-$2.argv" ;;
                *) echo "unexpected: gh $*" >&2; exit 99 ;;
              esac
              exit 0
            fi
            echo "gh $*" >> "@LOG@"
            case "$1 $2" in
              "run list")
                [ -z "${GH_RUN_LIST_FAIL:-}" ] || { echo "HTTP 502" >&2; exit 1; }
                echo "${GH_RUN_COUNT:-0}" ;;
              "workflow run")
                [ -z "${GH_DISPATCH_FAIL:-}" ] || { echo "HTTP 502" >&2; exit 1; } ;;
              *) echo "unexpected: gh $*" >&2; exit 99 ;;
            esac
            """
        )
        .replace("@LOG@", str(tmp_path / "gh.log"))
        .replace("@DIR@", str(tmp_path))
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def gh_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh.log"
    return log.read_text().splitlines() if log.exists() else []


def issue_call(tmp_path: Path, verb: str) -> dict[str, str] | None:
    """The ``--flag value`` pairs of the last ``gh issue <verb>``, or None."""
    argv = tmp_path / f"issue-{verb}.argv"
    if not argv.exists():
        return None
    items = argv.read_bytes().decode().split("\0")[:-1]
    return {a: b for a, b in zip(items, items[1:], strict=False) if a.startswith("--")}


def run_flow(tmp_path: Path, work: Path, head_sha: str, **env_extra: str) -> Flow:
    """resolve -> (native gate, assumed to pass if it would run) -> tag step ->
    failure report, each run only when the workflow's own ``if:`` says so."""
    steps = workflow_steps()
    outputs: dict[str, dict[str, str]] = {}
    outcomes = {"checkout": "success", "uv": "success"}  # not run here
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        PATH=f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}",
        GH_TOKEN="x",
        HEAD_SHA=head_sha,
        GITHUB_REPOSITORY="o/r",
        GITHUB_SERVER_URL="https://forge.test",
        GITHUB_RUN_ID="4242",
        GITHUB_STEP_SUMMARY=str(work.parent / f"{work.name}.summary"),
        **env_extra,
    )

    def run(name: str, sid: str) -> subprocess.CompletedProcess:
        step = steps[name]
        out = work.parent / f"{work.name}.{sid}.out"
        out.write_text("")
        step_environ = {"GITHUB_OUTPUT": str(out)}
        step_environ.update(
            {k: expand(v, outputs, outcomes) for k, v in step_env(step).items()}
        )
        proc = subprocess.run(
            ["bash", "-c", expand(step_script(step), outputs, outcomes)],
            cwd=work,
            env={**env, **step_environ},
            capture_output=True,
            text=True,
        )
        outputs[sid] = dict(
            line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
        )
        outcomes[sid] = "success" if proc.returncode == 0 else "failure"
        return proc

    flow = Flow(outputs, run(RESOLVE, "resolve"))
    failed = flow.resolve.returncode != 0
    if not failed:
        if truthy(step_if(steps[NATIVE]), outputs):
            flow.native_ran = True
            outputs["native"] = {"ok": "true"}  # the real gate needs uv + PyPI
            outcomes["native"] = "success"
        if truthy(step_if(steps[TAGGING]), outputs):
            flow.tag = run(TAGGING, "tag")
            failed = flow.tag.returncode != 0
    if truthy(step_if(steps[REPORT]), outputs, failed=failed):
        flow.report = run(REPORT, "report")
    return flow
