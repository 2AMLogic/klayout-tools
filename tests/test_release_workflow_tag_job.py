"""Guards for ``release.yml``'s ``tag`` job (issue #5005).

Two layers:

* ``run_tag_checks`` -- comment-blind text checks that the tag job only tags a
  green, same-repo push to main, gates on the native extras, scopes its push to
  ``refs/tags/`` and keeps its dispatch-recovery wiring.
* behavioural tests that execute the workflow's OWN ``run:`` blocks (and
  evaluate its OWN ``if:`` conditions) against a local bare origin and a stub
  ``gh``. They exist because of a real failure: the first run pushed the tag
  and then the dispatch failed; the rerun saw the tag, resolved an empty
  version and skipped the dispatch forever -- a release with a tag and no
  publish run. Zero GitHub or PyPI writes: ``gh`` is a stub and the origin is a
  directory under ``tmp_path``.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

from _release_workflow_support import (
    TAG_JOB,
    WORKFLOW,
    Result,
    comment_out_first,
    expect_failure,
    job_block,
    strip_comments,
)

RESOLVE = "Resolve the release this green commit carries"
NATIVE = "Gate on native extras"
TAGGING = "Tag the green commit and dispatch publish"
DISPATCH = "gh workflow run publish.yml --repo o/r --ref v0.9.3"


def run_tag_checks(text: str) -> Result:
    r = Result()
    tag_text = "\n".join(job_block(strip_comments(text), TAG_JOB))

    # --- Case 11: the tag job guards ---------------------------------------
    for guard in (
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        r.check(
            guard in tag_text,
            f"11: the tag job guards on {guard}",
            "a red, PR or fork CI completion must never be tagged",
        )
    r.check(
        "check_native_extras.py" in tag_text,
        "11: the native-extras gate runs before tagging",
    )
    r.check(
        "gh workflow run publish.yml" in tag_text,
        "11: publish.yml is dispatched on the tag ref",
    )
    r.check(
        "refs/tags/$v" in tag_text,
        "11: the tag push is scoped to refs/tags/",
    )
    resolve_step = re.search(
        rf"name: {RESOLVE}\n((?:(?!      - ).*\n)*)",
        tag_text,
    )
    resolve = resolve_step.group(1) if resolve_step else ""
    r.check(
        "already exists; not tagging" in resolve,
        "11: a tag that already exists is skipped",
    )
    r.check(
        "no longer on main" in resolve,
        "11: a commit no longer on main is not tagged",
    )

    # --- Case 12: a stranded tag (tagged, never dispatched) recovers --------
    r.check(
        all(
            part in resolve
            for part in ("gh run list", "--workflow publish.yml", '--branch "$tag"')
        ),
        "12: the resolve step asks the forge whether publish.yml ran on the tag",
    )
    return r


def test_tag_job_identity() -> None:
    result = run_tag_checks(WORKFLOW.read_text())
    assert not result.failures, "\n".join(
        f"FAIL {case}: {detail}" for case, detail in result.failures
    )


def test_tag_job_mutation_controls() -> None:
    text = WORKFLOW.read_text()
    expect_failure(
        run_tag_checks, comment_out_first(text, "check_native_extras.py"), "11:"
    )
    expect_failure(run_tag_checks, comment_out_first(text, "gh run list"), "12:")


# --------------------------------------------------------------------------- #
# Running the workflow's own steps
# --------------------------------------------------------------------------- #


def _steps() -> dict[str, list[str]]:
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


def _script(step: list[str]) -> str:
    start = next(n for n, line in enumerate(step) if re.match(r"^ +run: \|$", line))
    indent = len(step[start]) - len(step[start].lstrip())
    body = []
    for line in step[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def _step_if(step: list[str]) -> str | None:
    for line in step:
        found = re.match(r"^ +if: (.+)$", line)
        if found:
            return found.group(1)
    return None


def _step_env(step: list[str]) -> dict[str, str]:
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


def _truthy(expr: str | None, outputs: dict[str, dict[str, str]]) -> bool:
    """Evaluate the ``if:`` shapes the tag job uses (``steps.X.outputs.Y ==|!=
    'lit'`` joined by ``&&`` / ``||``); anything else is a loud test error."""
    if expr is None:
        return True

    def clause(text: str) -> bool:
        m = re.fullmatch(
            r"\s*steps\.(\w+)\.outputs\.(\w+)\s*(==|!=)\s*'([^']*)'\s*", text
        )
        assert m, f"unsupported if: clause {text!r}; teach _truthy about it"
        got = outputs.get(m.group(1), {}).get(m.group(2), "")
        return (got == m.group(4)) == (m.group(3) == "==")

    return any(all(clause(c) for c in alt.split("&&")) for alt in expr.split("||"))


def _expand(text: str, outputs: dict[str, dict[str, str]]) -> str:
    return re.sub(
        r"\$\{\{\s*steps\.(\w+)\.outputs\.(\w+)\s*\}\}",
        lambda m: outputs.get(m.group(1), {}).get(m.group(2), ""),
        text,
    )


@dataclass
class Flow:
    outputs: dict[str, dict[str, str]]
    resolve: subprocess.CompletedProcess
    native_ran: bool = False
    tag: subprocess.CompletedProcess | None = None


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@e.x", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _origin(tmp_path: Path, version: str = "0.9.3") -> tuple[Path, str]:
    """A bare origin whose main carries ``base-version = version``."""
    bare, seed = tmp_path / "origin.git", tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "pyproject.toml").write_text(
        f'[tool.hatch.version]\nbase-version = "{version}"\n'
    )
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", f"chore: bump version to {version}")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-q", "origin", "main")
    return bare, _git(seed, "rev-parse", "HEAD")


def _clone(tmp_path: Path, bare: Path, name: str) -> Path:
    """A fresh full-history checkout with tags (the job's `fetch-depth: 0`)."""
    work = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    return work


def _push_tag(tmp_path: Path, bare: Path, sha: str, tag: str) -> None:
    work = _clone(tmp_path, bare, f"tagger-{tag}")
    _git(work, "tag", "-a", tag, "-m", tag, sha)
    _git(work, "push", "-q", "origin", f"refs/tags/{tag}")


def _tags(bare: Path) -> str:
    return _git(bare, "for-each-ref", "refs/tags")


def _stub_gh(tmp_path: Path) -> Path:
    """`gh run list` prints $GH_RUN_COUNT; `gh workflow run` succeeds. Both can
    be made to fail; every call is logged to ``gh.log``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
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
        ).replace("@LOG@", str(tmp_path / "gh.log"))
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _gh_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh.log"
    return log.read_text().splitlines() if log.exists() else []


def _run_flow(tmp_path: Path, work: Path, head_sha: str, **env_extra: str) -> Flow:
    """resolve -> (native gate, assumed to pass if it would run) -> tag step,
    each run only when the workflow's own ``if:`` says so."""
    steps = _steps()
    outputs: dict[str, dict[str, str]] = {}
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        PATH=f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}",
        GH_TOKEN="x",
        HEAD_SHA=head_sha,
        GITHUB_REPOSITORY="o/r",
        GITHUB_STEP_SUMMARY=str(work.parent / f"{work.name}.summary"),
        **env_extra,
    )

    def run(name: str, sid: str) -> subprocess.CompletedProcess:
        step = steps[name]
        out = work.parent / f"{work.name}.{sid}.out"
        out.write_text("")
        step_env = {"GITHUB_OUTPUT": str(out)}
        step_env.update({k: _expand(v, outputs) for k, v in _step_env(step).items()})
        proc = subprocess.run(
            ["bash", "-c", _expand(_script(step), outputs)],
            cwd=work,
            env={**env, **step_env},
            capture_output=True,
            text=True,
        )
        outputs[sid] = dict(
            line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
        )
        return proc

    flow = Flow(outputs, run(RESOLVE, "resolve"))
    if flow.resolve.returncode != 0:
        return flow
    if _truthy(_step_if(steps[NATIVE]), outputs):
        flow.native_ran = True
        outputs["native"] = {"ok": "true"}  # the real gate needs uv + PyPI
    if _truthy(_step_if(steps[TAGGING]), outputs):
        flow.tag = run(TAGGING, "tag")
    return flow


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #


def test_fresh_release_tags_and_dispatches(tmp_path):
    bare, sha = _origin(tmp_path)
    _stub_gh(tmp_path)

    flow = _run_flow(tmp_path, _clone(tmp_path, bare, "run1"), sha)

    assert flow.outputs["resolve"] == {"version": "0.9.3", "tag_exists": "false"}
    assert flow.native_ran
    assert flow.tag is not None and flow.tag.returncode == 0, flow.tag
    assert "refs/tags/v0.9.3" in _tags(bare)
    assert _gh_calls(tmp_path) == [DISPATCH]  # no run-list lookup on the fresh path


def test_rerun_after_a_failed_dispatch_redispatches_without_retagging(tmp_path):
    """The reported failure: tag pushed, dispatch failed, rerun skipped it."""
    bare, sha = _origin(tmp_path)
    _stub_gh(tmp_path)

    first = _run_flow(
        tmp_path, _clone(tmp_path, bare, "run1"), sha, GH_DISPATCH_FAIL="1"
    )
    assert first.tag is not None and first.tag.returncode != 0
    stranded = _tags(bare)
    assert "refs/tags/v0.9.3" in stranded  # tagged, publish never dispatched

    second = _run_flow(tmp_path, _clone(tmp_path, bare, "run2"), sha, GH_RUN_COUNT="0")

    assert second.outputs["resolve"] == {"version": "0.9.3", "tag_exists": "true"}
    assert not second.native_ran  # it passed when the tag was cut
    assert second.tag is not None and second.tag.returncode == 0, second.tag
    assert _tags(bare) == stranded  # never re-tagged
    dispatches = [c for c in _gh_calls(tmp_path) if c.startswith("gh workflow run")]
    assert dispatches == [DISPATCH, DISPATCH]


def test_a_tag_whose_publish_already_ran_is_left_alone(tmp_path):
    bare, sha = _origin(tmp_path)
    _stub_gh(tmp_path)
    _push_tag(tmp_path, bare, sha, "v0.9.3")

    flow = _run_flow(tmp_path, _clone(tmp_path, bare, "run1"), sha, GH_RUN_COUNT="1")

    assert flow.outputs["resolve"] == {"version": "", "tag_exists": "false"}
    assert flow.tag is None
    assert not any(c.startswith("gh workflow run") for c in _gh_calls(tmp_path))


def test_an_older_tag_a_newer_release_has_passed_is_never_revived(tmp_path):
    bare, sha = _origin(tmp_path)
    _stub_gh(tmp_path)
    _push_tag(tmp_path, bare, sha, "v0.9.3")
    _push_tag(tmp_path, bare, sha, "v0.9.4")

    flow = _run_flow(tmp_path, _clone(tmp_path, bare, "run1"), sha, GH_RUN_COUNT="0")

    assert flow.outputs["resolve"]["version"] == ""
    assert flow.tag is None
    assert _gh_calls(tmp_path) == []


def test_an_unreadable_run_list_fails_red_instead_of_reading_as_released(tmp_path):
    bare, sha = _origin(tmp_path)
    _stub_gh(tmp_path)
    _push_tag(tmp_path, bare, sha, "v0.9.3")

    failing = _run_flow(
        tmp_path, _clone(tmp_path, bare, "run1"), sha, GH_RUN_LIST_FAIL="1"
    )
    garbled = _run_flow(
        tmp_path, _clone(tmp_path, bare, "run2"), sha, GH_RUN_COUNT="oops"
    )

    assert failing.resolve.returncode != 0 and failing.tag is None
    assert garbled.resolve.returncode != 0 and garbled.tag is None
    assert "cannot tell whether publish.yml ran" in garbled.resolve.stdout
