"""`gh api` and `git` transport for ``release_ci.py`` (issue #6911).

``release_ci.py`` holds the release policy; this file only talks to GitHub and
to the remote. Every call raises :class:`ForgeError` / :class:`GitError`
instead of returning a sentinel, so a caller can tell "the answer is no" (an
empty PR list, a missing ref) from "I could not find out" (a 403, a timeout).
The branch-recovery logic turns on exactly that difference: it deletes nothing
unless both answers are conclusive.

Tests substitute a stub with the same methods for :class:`GhForge`, and use a
real temporary bare remote for :class:`Git`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

_HTTP_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)")


class ForgeError(RuntimeError):
    """A GitHub call failed; ``status`` is the HTTP status when `gh` reported one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitError(RuntimeError):
    """A `git` invocation against the checkout or the remote failed."""


class GhForge:
    """The subset of the GitHub REST API the release workflow uses, via `gh api`."""

    def __init__(self, repo: str, *, gh: str = "gh", timeout: int = 90) -> None:
        self.repo = repo
        self.gh = gh
        self.timeout = timeout

    def _gh(self, *args: str) -> str:
        try:
            proc = subprocess.run(
                [self.gh, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ForgeError(f"gh could not run: {exc}") from exc
        if proc.returncode != 0:
            text = (
                proc.stderr or proc.stdout
            ).strip() or f"gh exited {proc.returncode}"
            match = _HTTP_STATUS_RE.search(text)
            raise ForgeError(text, int(match.group(1)) if match else None)
        return proc.stdout

    def _api(self, path: str, *fields: str, method: str = "GET") -> object:
        args = ["api", "--method", method, f"repos/{self.repo}/{path}"]
        for field in fields:
            args += ["-f", field]  # -f is a literal string; -F would expand @file
        try:
            return json.loads(self._gh(*args) or "null")
        except json.JSONDecodeError as exc:
            raise ForgeError(f"unparseable response for {path}: {exc}") from exc

    def workflow_permissions(self) -> dict:
        """Repository workflow permissions (admin-only; a 403 means unreadable)."""
        data = self._api("actions/permissions/workflow")
        return data if isinstance(data, dict) else {}

    def open_pulls(self, head: str) -> list[dict]:
        """Open PRs whose head is ``owner:branch``."""
        data = self._api(f"pulls?state=open&per_page=100&head={quote(head, safe='')}")
        if not isinstance(data, list):
            raise ForgeError("pull request lookup returned a non-list response")
        return data

    def create_pull(self, *, base: str, head: str, title: str, body: str) -> dict:
        data = self._api(
            "pulls",
            f"base={base}",
            f"head={head}",
            f"title={title}",
            f"body={body}",
            method="POST",
        )
        return data if isinstance(data, dict) else {}

    def open_issues(self) -> list[dict]:
        """Open issues (not PRs) as ``{number, title}`` dicts, every page."""
        out = self._gh(
            "api",
            "--paginate",
            f"repos/{self.repo}/issues?state=open&per_page=100",
            "--jq",
            ".[] | select(.pull_request == null) | {number, title}",
        )
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def create_issue(self, title: str, body: str) -> dict:
        data = self._api("issues", f"title={title}", f"body={body}", method="POST")
        return data if isinstance(data, dict) else {}

    def update_issue(self, number: int, title: str, body: str) -> dict:
        data = self._api(
            f"issues/{number}", f"title={title}", f"body={body}", method="PATCH"
        )
        return data if isinstance(data, dict) else {}

    def run_jobs(self, run_id: str) -> list[dict]:
        data = self._api(f"actions/runs/{run_id}/jobs?per_page=100")
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise ForgeError("run jobs lookup returned no jobs list")
        return jobs

    def job_log(self, job_id: int) -> str:
        # Raw logs carry terminal escapes, which `gh api` refuses to print
        # unless told to; the caller strips them before the text is reused.
        return self._gh(
            "api",
            "--allow-escape-sequences",
            f"repos/{self.repo}/actions/jobs/{job_id}/logs",
        )


class Git:
    """The few git operations the release branch recovery needs."""

    def __init__(self, cwd: Path | str, remote: str = "origin") -> None:
        self.cwd = Path(cwd)
        self.remote = remote

    def _git(self, *args: str) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        except OSError as exc:
            raise GitError(f"git could not run: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise GitError(f"git {args[0]} failed: {detail}")
        return proc.stdout

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def push(self, branch: str) -> None:
        """Push ``branch`` without force; an existing remote branch is kept."""
        ref = f"refs/heads/{branch}"
        self._git("push", self.remote, f"{ref}:{ref}")

    def remote_sha(self, branch: str) -> str | None:
        """The remote tip of ``branch``; ``None`` only if the remote says absent."""
        out = self._git("ls-remote", "--heads", self.remote, f"refs/heads/{branch}")
        line = out.strip().splitlines()[0] if out.strip() else ""
        return line.split()[0] if line else None

    def delete_if_at(self, branch: str, sha: str) -> None:
        """Delete the remote branch only while it still points at ``sha``."""
        ref = f"refs/heads/{branch}"
        self._git("push", self.remote, f"--force-with-lease={ref}:{sha}", f":{ref}")
