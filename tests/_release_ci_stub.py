"""Shared fixtures for the release-workflow helper tests (issue #6911).

``StubForge`` implements the methods ``scripts/release_forge.GhForge`` exposes,
over in-memory state, so the release policy runs without a network. Failures
are injected by setting an attribute to a ``ForgeError``; ``on_create`` runs
inside ``create_pull`` so a test can change the world at the moment the PR
call happens (push a commit to the branch, open the PR server-side).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def load_release_ci():
    """Import scripts/release_ci.py (it adds scripts/ to sys.path itself)."""
    spec = importlib.util.spec_from_file_location(
        "release_ci_under_test", SCRIPTS / "release_ci.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = load_release_ci()
ForgeError = rc.ForgeError
GitError = rc.GitError
Git = rc.Git


class StubForge:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.permissions: dict | ForgeError = {"can_approve_pull_request_reviews": True}
        self.pulls: list[dict] = []
        self.create_error: ForgeError | None = None
        self.pull_lookup_error: ForgeError | None = None
        self.on_create: Callable[[StubForge, str], None] | None = None
        self.created: list[dict] = []
        self.issues: list[dict] = []
        self.issue_write_error: ForgeError | None = None
        self.jobs: list[dict] | ForgeError = []
        self.logs: dict[int, str] = {}
        self.log_error: ForgeError | None = None
        self._next_issue = 100

    # -- setup check
    def workflow_permissions(self) -> dict:
        self.calls.append("workflow_permissions")
        if isinstance(self.permissions, ForgeError):
            raise self.permissions
        return self.permissions

    # -- pull requests
    def open_pulls(self, head: str) -> list[dict]:
        self.calls.append(f"open_pulls:{head}")
        if self.pull_lookup_error:
            raise self.pull_lookup_error
        return [p for p in self.pulls if p["head"] == head]

    def create_pull(self, *, base: str, head: str, title: str, body: str) -> dict:
        self.calls.append(f"create_pull:{head}")
        if self.on_create:
            self.on_create(self, head)
        if self.create_error:
            raise self.create_error
        pr = {
            "number": 7,
            "html_url": "https://github.com/acme/widgets/pull/7",
            "head": head,
            "base": base,
            "title": title,
            "body": body,
        }
        self.pulls.append(pr)
        self.created.append(pr)
        return pr

    # -- issues
    def open_issues(self) -> list[dict]:
        self.calls.append("open_issues")
        return [
            {"number": i["number"], "title": i["title"]}
            for i in self.issues
            if i["state"] == "open"
        ]

    def create_issue(self, title: str, body: str) -> dict:
        self.calls.append("create_issue")
        if self.issue_write_error:
            raise self.issue_write_error
        issue = {"number": self._next_issue, "title": title, "body": body}
        self._next_issue += 1
        self.issues.append({**issue, "state": "open"})
        return issue

    def update_issue(self, number: int, title: str, body: str) -> dict:
        self.calls.append(f"update_issue:{number}")
        if self.issue_write_error:
            raise self.issue_write_error
        issue = next(i for i in self.issues if i["number"] == number)
        issue.update(title=title, body=body)
        return issue

    # -- workflow run
    def run_jobs(self, run_id: str) -> list[dict]:
        self.calls.append(f"run_jobs:{run_id}")
        if isinstance(self.jobs, ForgeError):
            raise self.jobs
        return self.jobs

    def job_log(self, job_id: int) -> str:
        self.calls.append(f"job_log:{job_id}")
        if self.log_error:
            raise self.log_error
        return self.logs[job_id]

    def open_trackers(self) -> list[dict]:
        return [
            i
            for i in self.issues
            if i["state"] == "open" and i["title"].startswith(rc.BLOCKED)
        ]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
