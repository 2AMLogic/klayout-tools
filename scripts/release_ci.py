#!/usr/bin/env python3
"""Release-workflow helper: setup check, push-and-open-PR, failure report (#6911).

``.github/workflows/release.yml`` calls this so the three behaviours that
decide whether a failed automated release is *recoverable* are tested code, not
inline shell::

    release_ci.py check-pr-setup --repo OWNER/NAME [--release-token]
    release_ci.py publish --repo OWNER/NAME --version X.Y.Z [--base main]
    release_ci.py report-failure --repo OWNER/NAME --run-id N [--run-url URL]

``check-pr-setup`` runs before anything is pushed. A readable
``can_approve_pull_request_reviews: false`` is a *confirmed* denial: it exits 1
and names the setting and who can change it. A 403 reading the setting leaves
the value *unknown* and proceeds (the push-time recovery below still applies).
A configured ``RELEASE_BOT_TOKEN`` uses its own permissions, so no probe runs.

``publish`` pushes ``release/vX.Y.Z`` and opens the PR. When PR creation
reports an error it reconciles the exact ref it pushed: an open PR on that head
(GitHub may have created it and lost the response) means success; otherwise the
branch is deleted *only if* the PR lookup and the remote-ref lookup both
answered and the remote is still at the recorded SHA, using an expected-SHA
lease. Anything inconclusive keeps the branch.

``report-failure`` is the shared reporter for a failed ``prepare`` or ``tag``
job. It reads the failed run's jobs and log with the workflow's own token (not
the release-PR credential) and creates or refreshes the one open "Automated
release blocked" issue with the failed step, the diagnostic and the run URL.

Exit codes: 0 ok; 1 the step's own failure; 2 bad usage.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_forge import ForgeError, GhForge, Git, GitError

SETTING_PATH = (
    "Settings → Actions → General → Workflow permissions → "
    '"Allow GitHub Actions to create and approve pull requests"'
)
ADMIN_GUIDANCE = (
    f"A repository administrator can enable {SETTING_PATH} (an organization owner "
    "must change it first when an organization policy enforces it), or add a "
    "`RELEASE_BOT_TOKEN` repository secret holding a token that may open pull "
    'requests, e.g. a GitHub App token. See RELEASING.md, "Administrator setup".'
)
_DENIAL_TEXT = "not permitted to create or approve pull requests"

BLOCKED = "Automated release blocked"
NATIVE_STEP = "Gate on native extras"
NATIVE_TITLE = f"{BLOCKED}: klt-*-native pin not published"
# Job display names in release.yml (tests/test_release_ci.py keeps them in sync).
JOB_KINDS = {
    "Open the release PR when a release is due": "prepare",
    "Tag and publish a merged release commit": "tag",
}
GUIDANCE = {
    "native": (
        "No PR and no tag were made. Publish the pin (docs/releasing-native.md), "
        "then re-run the Release workflow; this issue is refreshed in place, "
        "not re-filed."
    ),
    "prepare": (
        "No release PR was opened. Fix the cause above, then re-run the Release "
        "workflow (a `dry_run` dispatch first if unsure). A branch this run "
        "pushed is removed again when no PR exists for it; if the log says it "
        'was kept, see RELEASING.md, "When the automated release fails".'
    ),
    "tag": (
        "The tag job did not finish. If the tag was already pushed but "
        '`publish.yml` was not dispatched, run `publish.yml` via "Run workflow" '
        "on that tag; do not re-tag. Otherwise fix the cause above and re-run "
        "this job."
    ),
    "release": "Fix the cause above, then re-run the Release workflow.",
}

_FAILED_JOB = {"failure", "timed_out"}
_FAILED_STEP = {"failure", "timed_out", "cancelled"}
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _line(text: str) -> str:
    return " ".join(str(text).split())


def _annotate(level: str, message: str) -> None:
    print(f"::{level}::{_line(message)}", flush=True)


# --- check-pr-setup --------------------------------------------------------


@dataclass(frozen=True)
class SetupCheck:
    status: str  # ok | denied | unknown | release_token
    message: str


def check_pr_setup(forge, repo: str, *, release_token: bool) -> SetupCheck:
    """Classify whether the default token may open the release PR."""
    if release_token:
        return SetupCheck(
            "release_token",
            "RELEASE_BOT_TOKEN is configured; the release PR is opened with that "
            "token's own permissions, so the workflow pull-request setting does "
            "not apply.",
        )
    try:
        perms = forge.workflow_permissions()
    except ForgeError as exc:
        where = f"HTTP {exc.status}" if exc.status else "an API error"
        return SetupCheck(
            "unknown",
            f"Could not read the workflow permissions of {repo} ({where}); the "
            "setting's value is unknown, so the release will try to open the PR. "
            f"If that fails: {ADMIN_GUIDANCE}",
        )
    allowed = perms.get("can_approve_pull_request_reviews")
    if allowed is False:
        return SetupCheck(
            "denied",
            f"GitHub Actions is not permitted to create pull requests in {repo} "
            "(workflow permission can_approve_pull_request_reviews is false), so "
            f"no release branch was pushed. {ADMIN_GUIDANCE}",
        )
    if allowed is True:
        return SetupCheck("ok", f"GitHub Actions may create pull requests in {repo}.")
    return SetupCheck(
        "unknown",
        f"The workflow permissions of {repo} did not report "
        f"can_approve_pull_request_reviews; treating it as unknown. {ADMIN_GUIDANCE}",
    )


def cmd_check_pr_setup(args, forge, _git) -> int:
    result = check_pr_setup(forge, args.repo, release_token=args.release_token)
    level = {"denied": "error", "unknown": "warning"}.get(result.status, "notice")
    _annotate(level, result.message)
    return 1 if result.status == "denied" else 0


# --- publish ---------------------------------------------------------------


@dataclass(frozen=True)
class Reconciled:
    pr_url: str | None  # an open PR exists on the pushed head
    message: str  # what happened to the branch when there is no PR


def reconcile_branch(forge, git, *, owner: str, branch: str, sha: str) -> Reconciled:
    """Decide what to do with ``branch`` after PR creation reported an error."""
    try:
        prs = forge.open_pulls(f"{owner}:{branch}")
    except ForgeError as exc:
        return Reconciled(
            None,
            f"The open-PR lookup for {branch} was inconclusive ({exc}); kept the "
            f"remote branch at {sha}. Check for an open PR, then delete it by hand "
            "if there is none.",
        )
    if prs:
        return Reconciled(prs[0].get("html_url") or f"#{prs[0].get('number')}", "")
    try:
        remote = git.remote_sha(branch)
    except GitError as exc:
        return Reconciled(
            None, f"Could not read the remote {branch} ({exc}); kept it at {sha}."
        )
    if remote is None:
        return Reconciled(
            None, f"{branch} is already gone from the remote; a retry can push it."
        )
    if remote != sha:
        return Reconciled(
            None,
            f"{branch} is at {remote} on the remote, not the {sha} this run pushed; "
            "left it alone because someone else's commits are on it.",
        )
    try:
        git.delete_if_at(branch, sha)
    except GitError as exc:
        return Reconciled(None, f"Could not delete {branch} at {sha}; kept it ({exc}).")
    return Reconciled(
        None,
        f"No open PR exists for {branch}; deleted it at {sha} (expected-SHA lease) "
        "so a retry can push it again.",
    )


def publish(forge, git, *, repo: str, version: str, base: str = "main") -> int:
    """Push the release branch and open its PR, recovering the branch on failure."""
    branch = f"release/v{version}"
    title = f"chore(release): bump version to {version}"
    body = (
        "Automated release PR (RELEASING.md, Automated release). Merging it makes "
        f"the tag job tag `v{version}` and dispatch `publish.yml`. Not auto-merged."
    )
    try:
        sha = git.create_branch(branch)
    except GitError as exc:
        _annotate(
            "error",
            f"Pushing {branch} failed, so this run left nothing on the remote. If "
            f"{branch} already exists there, a person must delete or merge it ({exc})",
        )
        return 1
    print(f"Pushed refs/heads/{branch} at {sha}.", flush=True)
    try:
        pr = forge.create_pull(base=base, head=branch, title=title, body=body)
    except ForgeError as exc:
        return _after_failed_create(forge, git, repo, branch, sha, exc)
    print(f"Opened {pr.get('html_url', branch)}", flush=True)
    return 0


def _after_failed_create(forge, git, repo, branch, sha, error) -> int:
    owner = repo.split("/", 1)[0]
    outcome = reconcile_branch(forge, git, owner=owner, branch=branch, sha=sha)
    if outcome.pr_url:
        _annotate(
            "notice",
            f"Opening the PR reported an error ({error}) but {outcome.pr_url} is "
            f"open for {branch}; nothing to retry.",
        )
        return 0
    hint = f" {ADMIN_GUIDANCE}" if _DENIAL_TEXT in str(error) else ""
    _annotate(
        "error", f"Opening the release PR failed after pushing {branch}: {error}{hint}"
    )
    _annotate("error", outcome.message)
    return 1


def cmd_publish(args, forge, git) -> int:
    return publish(forge, git, repo=args.repo, version=args.version, base=args.base)


# --- report-failure --------------------------------------------------------


def extract_diagnostic(log: str, *, tail: int = 40, limit: int = 6000) -> str:
    """The failing step's last output plus the first error annotations of a job log.

    Workflow logs are timestamped, may carry terminal escapes, and put each
    step's command text between ``##[group]`` and ``##[endgroup]``; the real
    error is the output just before the first ``##[error]`` line.
    """
    lines: list[str] = []
    in_group = False
    for raw in log.lstrip("﻿").splitlines():
        text = _TIMESTAMP_RE.sub("", _ANSI_RE.sub("", raw)).rstrip()
        if text.startswith("##[group]"):
            in_group = True
        elif text.startswith("##[endgroup]"):
            in_group = False
        elif not in_group:
            lines.append(text)
    first = next((i for i, t in enumerate(lines) if t.startswith("##[error]")), None)
    if first is None:
        picked = lines[-tail:]
    else:
        errors = [t for t in lines[first:] if t.startswith("##[error]")][:10]
        picked = lines[max(0, first - tail) : first] + errors
    text = "\n".join(t.replace("##[error]", "error: ", 1) for t in picked)
    return text[-limit:].replace("```", "'''").strip()


def _first_failed(forge, run_id: str) -> tuple[dict, dict] | None:
    jobs = [j for j in forge.run_jobs(run_id) if j.get("conclusion") in _FAILED_JOB]
    if not jobs:
        return None
    steps = jobs[0].get("steps") or []
    step = next((s for s in steps if s.get("conclusion") in _FAILED_STEP), {})
    return jobs[0], step


def _diagnostic(forge, job: dict) -> str:
    try:
        text = extract_diagnostic(forge.job_log(job["id"]))
    except (ForgeError, KeyError) as exc:
        return f"(the job log could not be read: {exc})"
    return text or "(the job log held no error output)"


def render_issue(
    *, job_name: str, step: str, run_url: str, job_url: str, diagnostic: str
) -> tuple[str, str]:
    """The (title, body) of the tracker for one failed job."""
    kind = "native" if step == NATIVE_STEP else JOB_KINDS.get(job_name, "release")
    title = NATIVE_TITLE if kind == "native" else f'{BLOCKED}: "{step}" failed'
    body = (
        f"The release workflow stopped in the `{job_name}` job.\n\n"
        f"- **Failed step:** {step}\n"
        f"- **Run:** {run_url}\n"
        f"- **Job:** {job_url}\n\n"
        f"```\n{diagnostic}\n```\n\n"
        f"{GUIDANCE[kind]}\n\n"
        "This issue is refreshed in place by the next failure, not re-filed. "
        "Close it once the release has gone out."
    )
    return title, body


def upsert_issue(forge, title: str, body: str) -> tuple[int, str]:
    """Refresh the one open tracker (native-pin tracker first), or create it."""
    open_trackers = [
        i for i in forge.open_issues() if str(i.get("title", "")).startswith(BLOCKED)
    ]
    open_trackers.sort(key=lambda i: (i["title"] != NATIVE_TITLE, i["number"]))
    if open_trackers:
        number = open_trackers[0]["number"]
        forge.update_issue(number, title, body)
        return number, "updated"
    return forge.create_issue(title, body)["number"], "created"


def report_failure(forge, *, run_id: str, run_url: str) -> tuple[int, str]:
    """File or refresh the tracking issue for the failed run's first failed step."""
    job: dict = {"name": "unknown", "html_url": run_url}
    step: dict = {}
    diagnostic = "(the failed job could not be identified from the run)"
    try:
        found = _first_failed(forge, run_id)
    except ForgeError as exc:
        diagnostic = f"(the run's jobs could not be read: {exc})"
        found = None
    if found:
        job, step = found
        diagnostic = _diagnostic(forge, job)
    title, body = render_issue(
        job_name=job.get("name", "unknown"),
        step=step.get("name", "unknown"),
        run_url=run_url,
        job_url=job.get("html_url", run_url),
        diagnostic=diagnostic,
    )
    return upsert_issue(forge, title, body)


def cmd_report_failure(args, forge, _git) -> int:
    run_url = (
        args.run_url or f"https://github.com/{args.repo}/actions/runs/{args.run_id}"
    )
    try:
        number, action = report_failure(forge, run_id=args.run_id, run_url=run_url)
    except ForgeError as exc:
        _annotate(
            "error", f"Could not file or refresh the blocked-release issue: {exc}"
        )
        return 1
    print(f"{action.capitalize()} issue #{number} for run {args.run_id}.", flush=True)
    return 0


# --- CLI -------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("check-pr-setup", help="probe PR-creation permission")
    setup.add_argument("--repo", required=True)
    setup.add_argument("--release-token", action="store_true")
    setup.set_defaults(handler=cmd_check_pr_setup)
    pub = sub.add_parser("publish", help="push the release branch and open its PR")
    pub.add_argument("--repo", required=True)
    pub.add_argument("--version", required=True)
    pub.add_argument("--base", default="main")
    pub.add_argument("--remote", default="origin")
    pub.set_defaults(handler=cmd_publish)
    rep = sub.add_parser("report-failure", help="file/refresh the blocked issue")
    rep.add_argument("--repo", required=True)
    rep.add_argument("--run-id", required=True)
    rep.add_argument("--run-url", default="")
    rep.set_defaults(handler=cmd_report_failure)
    return parser


def main(
    argv: list[str] | None = None,
    forge_factory: Callable[[str], object] = GhForge,
    git_factory: Callable[..., object] = Git,
) -> int:
    args = _parser().parse_args(argv)
    git = git_factory(Path.cwd(), getattr(args, "remote", "origin"))
    return args.handler(args, forge_factory(args.repo), git)


if __name__ == "__main__":
    sys.exit(main())
