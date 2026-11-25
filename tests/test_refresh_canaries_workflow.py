"""The daily canary refresh must repair drift as one reviewable PR (issue #7882).

`refresh-canaries.yml` is the only mechanism that reads inventory drift that
arrives with no commit attached (another repo changes visibility, a new
`<pdk>-*` canary repo appears). These tests pin the properties that make a
scheduled run a notification *and* a repair: it runs daily, it syncs the
manifest before ingesting it, it keeps a single stable PR branch, it never
pushes to `main`, and a failure stays red. A second test group runs the PR
step's real shell against a scratch remote with a stub `gh`, because that
path has never run on the forge and the day-two update is the part most
likely to be wrong.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "refresh-canaries.yml"
BLOCKS_README = REPO_ROOT / "blocks" / "README.md"
BRANCH = "auto/refresh-canaries"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML reads an unquoted `on` key as True.
    return workflow.get("on", workflow.get(True))


def _steps() -> list[dict]:
    return _workflow()["jobs"]["refresh"]["steps"]


def _command(step: dict) -> str:
    """The step's script with `\\`-newline continuations joined."""
    return re.sub(r"\\\n\s*", " ", step.get("run", ""))


def _pr_step() -> dict:
    (step,) = [s for s in _steps() if "gh pr create" in s.get("run", "")]
    return step


# --- workflow shape ----------------------------------------------------------


def test_runs_daily_and_on_demand() -> None:
    triggers = _triggers(_workflow())
    assert "workflow_dispatch" in triggers
    (entry,) = triggers["schedule"]
    minute, hour, day, month, weekday = entry["cron"].split()
    assert (day, month, weekday) == ("*", "*", "*"), "must be a daily schedule"
    assert minute.isdigit() and hour.isdigit(), "pick one fixed off-peak minute"
    # Not the slot of the read-only drift check, so the two never collide.
    assert (minute, hour) != ("43", "5")
    drift_check = WORKFLOWS / "canary-manifest.yml"
    if drift_check.exists():
        other = _triggers(yaml.safe_load(drift_check.read_text()))["schedule"]
        assert entry["cron"] not in [e["cron"] for e in other]


def test_sync_manifest_runs_before_the_manifest_ingest() -> None:
    steps = _steps()
    # Match the `uv run` invocation, not prose: the PR body quotes both flags.
    sync = [
        i
        for i, s in enumerate(steps)
        if re.search(r"uv run\s.*ingest-canary\.py\s+--sync-manifest\b", _command(s))
    ]
    ingest = [
        i
        for i, s in enumerate(steps)
        if re.search(
            r"uv run\s.*ingest-canary\.py\s+--manifest\s+blocks/canaries\.json",
            _command(s),
        )
    ]
    assert len(sync) == 1, "exactly one step syncs the manifest"
    assert len(ingest) == 1, "exactly one step ingests the manifest"
    assert sync[0] < ingest[0], "ingesting before syncing never repairs drift"


def test_drift_uses_one_stable_branch() -> None:
    run = _pr_step()["run"]
    # A dated name (the pre-#7882 `...-$(date -u +%Y%m%d)`) would open one PR
    # per day while an earlier drift PR sits unmerged.
    assert re.findall(r'^\s*branch="([^"]*)"', run, re.MULTILINE) == [BRANCH]
    assert 'gh pr list --head "$branch" --state open' in run
    assert "--force-with-lease" in run
    # A fresh checkout has no remote-tracking ref for yesterday's branch; the
    # lease only works when it is fetched first.
    assert run.index("git fetch") < run.index("git push")


def test_no_drift_exits_before_touching_git() -> None:
    run = _pr_step()["run"]
    guard = run.index("git status --porcelain -- blocks/")
    first_write = run.index("git checkout")
    assert guard < first_write
    assert "exit 0" in run[guard:first_write]


def test_never_pushes_to_main_or_uses_another_credential() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert _workflow()["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert "secrets." not in text, "the PR is the notification; no PAT/App token"
    assert "gh pr merge" not in text, "a human merges the repair PR"
    pushes = [
        line.strip()
        for step in _steps()
        for line in step.get("run", "").splitlines()
        if "git push" in line
    ]
    assert pushes
    assert all(line.endswith('"$branch"') for line in pushes), pushes
    assert "--base main" in _pr_step()["run"]


def test_a_failed_sync_or_entry_still_fails_the_run() -> None:
    steps = _steps()
    by_id = {s["id"]: s for s in steps if "id" in s}
    for step_id in ("sync", "ingest"):
        run = by_id[step_id]["run"]
        assert "set +e" in run, f"{step_id}: record the failure, don't abort"
        assert 'echo "rc=$?" >>"$GITHUB_OUTPUT"' in run
    last = steps[-1]
    assert "steps.sync.outputs.rc != '0'" in last["if"]
    assert "steps.ingest.outputs.rc != '0'" in last["if"]
    assert "exit 1" in last["run"]
    # After the PR step, so the entries that did succeed still get their PR.
    assert steps.index(last) > steps.index(_pr_step())


def test_readme_says_the_schedule_only_proposes_a_reviewed_pr() -> None:
    text = " ".join(BLOCKS_README.read_text(encoding="utf-8").split())
    assert "no schedule" not in text
    assert "do not auto-discover repos" not in text
    assert BRANCH in text


# --- the PR step's real shell, against a scratch remote ----------------------

_STUB_GH = """#!/usr/bin/env bash
# Records every call; `pr list` reports an open PR once `pr create` has run.
echo "$*" >>"$GH_STUB_LOG"
case "$1 $2" in
  "pr create") touch "$GH_STUB_STATE" ;;
  "pr list") if [ -e "$GH_STUB_STATE" ]; then echo 1; fi ;;
esac
exit 0
"""


class _Forge:
    """A bare remote plus a way to mimic `actions/checkout` against it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = root / "remote.git"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(_STUB_GH, encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.gh_log = root / "gh.log"
        self.env = {
            **os.environ,
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_TOKEN": "unused",
            "TRIGGER": "schedule",
            "GH_STUB_LOG": str(self.gh_log),
            "GH_STUB_STATE": str(root / "gh.pr-open"),
        }
        self.git(root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        seed = root / "seed"
        self.git(root, "init", "-q", "-b", "main", str(seed))
        (seed / "blocks").mkdir()
        (seed / "blocks" / "canaries.json").write_text("[]\n", encoding="utf-8")
        self.git(seed, "add", "-A")
        self.git(
            seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"
        )
        self.git(seed, "push", "-q", str(self.remote), "main")

    def git(self, cwd: Path, *args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return done.stdout

    def checkout(self, name: str) -> Path:
        """A fresh clone that fetched only `main`, like `actions/checkout`."""
        work = self.root / name
        self.git(self.root, "init", "-q", "-b", "main", str(work))
        self.git(work, "remote", "add", "origin", str(self.remote))
        self.git(
            work, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main"
        )
        self.git(work, "checkout", "-q", "-B", "main", "refs/remotes/origin/main")
        return work

    def run_pr_step(
        self, work: Path, manifest: str | None
    ) -> subprocess.CompletedProcess:
        if manifest is not None:
            (work / "blocks" / "canaries.json").write_text(manifest, encoding="utf-8")
        script = self.root / "step.sh"
        script.write_text(_pr_step()["run"], encoding="utf-8")
        # The shell GitHub Actions uses for `run:`.
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            cwd=work,
            env=self.env,
            capture_output=True,
            text=True,
        )

    def pr_creations(self) -> int:
        if not self.gh_log.exists():
            return 0
        lines = self.gh_log.read_text(encoding="utf-8").splitlines()
        return sum(1 for line in lines if line.startswith("pr create"))

    def remote_manifest(self, branch: str) -> str:
        return self.git(self.remote, "show", f"{branch}:blocks/canaries.json")

    def branch_exists(self) -> bool:
        return bool(self.git(self.remote, "branch", "--list", BRANCH).strip())


@pytest.fixture
def forge(tmp_path: Path) -> _Forge:
    return _Forge(tmp_path)


def test_no_drift_opens_nothing(forge: _Forge) -> None:
    result = forge.run_pr_step(forge.checkout("day1"), manifest=None)
    assert result.returncode == 0, result.stderr
    assert not forge.branch_exists()
    assert forge.pr_creations() == 0


def test_second_drifting_day_updates_the_open_pr_in_place(forge: _Forge) -> None:
    main_tip = forge.git(forge.remote, "rev-parse", "main")
    first = forge.run_pr_step(forge.checkout("day1"), manifest='[{"day": 1}]\n')
    assert first.returncode == 0, first.stderr
    assert forge.branch_exists()
    assert forge.pr_creations() == 1
    assert forge.remote_manifest(BRANCH) == '[{"day": 1}]\n'

    # Day two: the first PR is unmerged, and a fresh checkout has no
    # remote-tracking ref for its branch.
    second = forge.run_pr_step(forge.checkout("day2"), manifest='[{"day": 2}]\n')
    assert second.returncode == 0, second.stderr
    assert forge.remote_manifest(BRANCH) == '[{"day": 2}]\n'
    assert forge.pr_creations() == 1, "an open PR is updated, never joined by another"
    # The branch is rebuilt from main, not stacked on yesterday's commit.
    assert (
        forge.git(forge.remote, "rev-list", "--count", f"main..{BRANCH}").strip() == "1"
    )
    assert forge.git(forge.remote, "rev-parse", "main") == main_tip, "main is untouched"
