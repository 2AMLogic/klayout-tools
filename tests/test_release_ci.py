"""Tests for scripts/release_ci.py's setup check and failure reporter, and for
how .github/workflows/release.yml wires them (issue #6911).

The forge is a stub (``tests/_release_ci_stub.py``); the branch-recovery half
runs against a real temporary bare remote in ``test_release_ci_publish.py``.
"""

from __future__ import annotations

import yaml

from _release_ci_stub import REPO_ROOT, ForgeError, StubForge, rc

WORKFLOW_TEXT = (REPO_ROOT / ".github/workflows/release.yml").read_text()
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)
JOBS = WORKFLOW["jobs"]

PREPARE_JOB = "Open the release PR when a release is due"
TAG_JOB = "Tag and publish a merged release commit"
PUSH_STEP = "Push the release branch and open the PR"
TAG_STEP = "Tag the bump commit"
DISPATCH_STEP = "Dispatch publish on the tag"


def _steps(job: str) -> list[dict]:
    """Steps of a job given its key (``prepare``) or its display name."""
    if job in JOBS:
        return JOBS[job]["steps"]
    return next(j for j in JOBS.values() if j.get("name") == job)["steps"]


def _step(job: str, name: str) -> dict:
    return next(s for s in _steps(job) if s.get("name") == name)


def _index(job: str, name: str) -> int:
    return [s.get("name") for s in _steps(job)].index(name)


# --------------------------------------------------------------------------
# Pre-push setup check: confirmed denial vs unreadable vs configured credential
# --------------------------------------------------------------------------


def test_disabled_setting_is_a_confirmed_denial_naming_the_fix() -> None:
    forge = StubForge()
    forge.permissions = {"can_approve_pull_request_reviews": False}
    result = rc.check_pr_setup(forge, "acme/widgets", release_token=False)
    assert result.status == "denied"
    for needle in (
        "Allow GitHub Actions to create and approve pull requests",
        "repository administrator",
        "organization owner",
        "RELEASE_BOT_TOKEN",
        "no release branch was pushed",
    ):
        assert needle in result.message


def test_denial_exits_one_before_anything_is_pushed(capsys) -> None:
    forge = StubForge()
    forge.permissions = {"can_approve_pull_request_reviews": False}
    code = rc.main(
        ["check-pr-setup", "--repo", "acme/widgets"], forge_factory=lambda _r: forge
    )
    assert code == 1
    assert capsys.readouterr().out.startswith("::error::")
    assert forge.calls == ["workflow_permissions"]  # the check pushes nothing


def test_unreadable_setting_is_unknown_not_a_denial(capsys) -> None:
    forge = StubForge()
    forge.permissions = ForgeError("Resource not accessible (HTTP 403)", status=403)
    result = rc.check_pr_setup(forge, "acme/widgets", release_token=False)
    assert result.status == "unknown"
    assert "unknown" in result.message and "HTTP 403" in result.message
    code = rc.main(
        ["check-pr-setup", "--repo", "acme/widgets"], forge_factory=lambda _r: forge
    )
    assert code == 0  # proceeds: the push-time recovery still applies
    assert capsys.readouterr().out.startswith("::warning::")


def test_missing_field_is_unknown_and_enabled_is_ok() -> None:
    forge = StubForge()
    forge.permissions = {"default_workflow_permissions": "read"}
    assert rc.check_pr_setup(forge, "a/b", release_token=False).status == "unknown"
    forge.permissions = {"can_approve_pull_request_reviews": True}
    assert rc.check_pr_setup(forge, "a/b", release_token=False).status == "ok"


def test_configured_release_token_skips_the_probe_entirely(capsys) -> None:
    forge = StubForge()
    forge.permissions = {"can_approve_pull_request_reviews": False}
    code = rc.main(
        ["check-pr-setup", "--repo", "acme/widgets", "--release-token"],
        forge_factory=lambda _r: forge,
    )
    assert code == 0
    assert forge.calls == []  # its own permissions apply; nothing is read
    assert "RELEASE_BOT_TOKEN is configured" in capsys.readouterr().out


def test_probe_runs_only_on_the_publishing_path() -> None:
    """No probe on a dry run, a release that is not due, or an existing-PR skip."""
    probe = _step(PREPARE_JOB, "Check the release PR can be created")
    assert "!inputs.dry_run" in probe["if"]
    assert "steps.native.outputs.ok == 'true'" in probe["if"]
    native_if = _step(PREPARE_JOB, "Gate on native extras")["if"]
    assert "steps.lag.outputs.due == 'true'" in native_if  # not due -> skipped
    assert "steps.existing.outputs.open == '0'" in native_if  # open PR -> skipped
    assert "check-pr-setup" in probe["run"]
    # Before anything is committed or pushed.
    assert _index(PREPARE_JOB, probe["name"]) < _index(
        PREPARE_JOB, "Prepare the release commit"
    )
    assert _index(PREPARE_JOB, probe["name"]) < _index(PREPARE_JOB, PUSH_STEP)


# --------------------------------------------------------------------------
# Shared failure reporter
# --------------------------------------------------------------------------

# The tail of the real failed prepare job log (run 43981896573, 2026-11-16).
LIVE_PREPARE_LOG = "\n".join(
    [
        '2026-11-16T23:45:03.2487631Z ##[group]Run v="0.10.0"',
        '2026-11-16T23:45:03.2488000Z v="0.10.0"',
        "2026-11-16T23:45:03.2489675Z env:",
        "2026-11-16T23:45:03.2489675Z   GH_TOKEN: ***",
        "2026-11-16T23:45:03.2490000Z ##[endgroup]",
        "2026-11-16T23:45:05.0214868Z remote: Create a pull request for "
        "'release/v0.10.0' on GitHub by visiting:        ",
        "2026-11-16T23:45:05.0235063Z To https://github.com/2AMLogic/klayout-tools",
        "2026-11-16T23:45:05.0235535Z  * [new branch]          "
        "release/v0.10.0 -> release/v0.10.0",
        "2026-11-16T23:45:06.2729612Z pull request create failed: GraphQL: GitHub "
        "Actions is not permitted to create or approve pull requests "
        "(createPullRequest)",
        "2026-11-16T23:45:06.2762349Z ##[error]Process completed with exit code 1.",
    ]
)


def _failed_job(job_id: int, name: str, failed_step: str, run: int) -> dict:
    return {
        "id": job_id,
        "name": name,
        "conclusion": "failure",
        "html_url": f"https://github.com/acme/widgets/actions/runs/{run}/job/{job_id}",
        "steps": [
            {"name": "Set up job", "conclusion": "success"},
            {"name": failed_step, "conclusion": "failure"},
            {"name": "Post Run actions/checkout", "conclusion": "success"},
        ],
    }


def _log(*lines: str) -> str:
    stamped = [f"2026-11-20T10:00:0{i}.0000000Z {t}" for i, t in enumerate(lines)]
    return "\n".join([*stamped, "2026-11-20T10:00:09.0000000Z ##[error]exit code 1"])


def _report(forge: StubForge, run: int) -> int:
    return rc.main(
        ["report-failure", "--repo", "acme/widgets", "--run-id", str(run)],
        forge_factory=lambda _r: forge,
    )


def test_repeated_failures_refresh_one_tracker_with_the_newest_failure(capsys) -> None:
    forge = StubForge()
    run_url = "https://github.com/acme/widgets/actions/runs/{}"

    # 1. prepare: PR creation denied after the push (the live failure).
    forge.jobs = [_failed_job(11, PREPARE_JOB, PUSH_STEP, 100)]
    forge.logs[11] = LIVE_PREPARE_LOG
    assert _report(forge, 100) == 0
    [tracker] = forge.open_trackers()
    assert PUSH_STEP in tracker["title"] and PUSH_STEP in tracker["body"]
    assert "not permitted to create or approve pull requests" in tracker["body"]
    assert run_url.format(100) in tracker["body"]
    assert "GH_TOKEN" not in tracker["body"]  # step command/env text is dropped
    first = tracker["number"]

    # 2. prepare: native pin unpublished. Same issue, now the native diagnostic.
    forge.jobs = [_failed_job(12, PREPARE_JOB, "Gate on native extras", 101)]
    forge.logs[12] = _log("klt-core-native==9.9.9 is not on PyPI")
    assert _report(forge, 101) == 0
    [tracker] = forge.open_trackers()
    assert tracker["number"] == first
    assert tracker["title"] == rc.NATIVE_TITLE
    assert "klt-core-native==9.9.9 is not on PyPI" in tracker["body"]
    assert (
        run_url.format(101) in tracker["body"]
        and run_url.format(100) not in (tracker["body"])
    )
    assert "docs/releasing-native.md" in tracker["body"]

    # 3. tag: the tag was pushed but the publish dispatch failed.
    forge.jobs = [_failed_job(13, TAG_JOB, DISPATCH_STEP, 102)]
    forge.logs[13] = _log("HTTP 422: Workflow does not have 'workflow_dispatch'")
    assert _report(forge, 102) == 0
    [tracker] = forge.open_trackers()
    assert tracker["number"] == first
    assert DISPATCH_STEP in tracker["title"]
    assert "workflow_dispatch" in tracker["body"]
    assert run_url.format(102) in tracker["body"]
    assert "do not re-tag" in tracker["body"]
    assert len(forge.issues) == 1  # one tracker across all three failures
    assert "Created issue" in capsys.readouterr().out


def test_an_existing_native_pin_tracker_is_reused() -> None:
    forge = StubForge()
    forge.issues = [
        {"number": 3, "title": "Unrelated bug", "body": "", "state": "open"},
        {"number": 9, "title": rc.BLOCKED + ": old note", "body": "", "state": "open"},
        {"number": 7, "title": rc.NATIVE_TITLE, "body": "old", "state": "open"},
    ]
    forge.jobs = [_failed_job(11, PREPARE_JOB, PUSH_STEP, 100)]
    forge.logs[11] = LIVE_PREPARE_LOG
    assert _report(forge, 100) == 0
    assert "update_issue:7" in forge.calls and "create_issue" not in forge.calls
    assert forge.issues[0]["body"] == ""  # unrelated issue untouched


def test_a_closed_tracker_is_not_reopened_or_reused() -> None:
    forge = StubForge()
    forge.issues = [
        {"number": 5, "title": rc.NATIVE_TITLE, "body": "old", "state": "closed"}
    ]
    forge.jobs = [_failed_job(11, PREPARE_JOB, PUSH_STEP, 100)]
    forge.logs[11] = LIVE_PREPARE_LOG
    assert _report(forge, 100) == 0
    assert forge.issues[0]["body"] == "old"
    assert len(forge.open_trackers()) == 1


def test_unreadable_log_or_jobs_still_report_with_the_run_link() -> None:
    forge = StubForge()
    forge.jobs = [_failed_job(11, PREPARE_JOB, PUSH_STEP, 100)]
    forge.log_error = ForgeError("HTTP 403", status=403)
    assert _report(forge, 100) == 0
    [tracker] = forge.open_trackers()
    assert PUSH_STEP in tracker["body"] and "could not be read" in tracker["body"]

    forge = StubForge()
    forge.jobs = ForgeError("HTTP 502", status=502)
    assert _report(forge, 100) == 0
    [tracker] = forge.open_trackers()
    assert "actions/runs/100" in tracker["body"]


def test_a_failing_issue_write_fails_the_reporting_step(capsys) -> None:
    forge = StubForge()
    forge.jobs = [_failed_job(11, PREPARE_JOB, PUSH_STEP, 100)]
    forge.logs[11] = LIVE_PREPARE_LOG
    forge.issue_write_error = ForgeError("HTTP 403", status=403)
    assert _report(forge, 100) == 1
    assert capsys.readouterr().out.startswith("::error::")


def test_extract_diagnostic_strips_noise_and_neutralizes_fences() -> None:
    log = (
        "﻿2026-11-20T10:00:00.0000000Z ##[group]Run secret-looking command\n"
        "2026-11-20T10:00:00.0000001Z script text inside the group\n"
        "2026-11-20T10:00:00.0000002Z ##[endgroup]\n"
        "2026-11-20T10:00:01.0000000Z \x1b[31mboom\x1b[0m ``` fenced\n"
        "2026-11-20T10:00:02.0000000Z ##[error]first\n"
        "2026-11-20T10:00:03.0000000Z ##[error]second\n"
    )
    text = rc.extract_diagnostic(log)
    assert "script text inside the group" not in text
    assert "\x1b" not in text and "```" not in text
    assert text.splitlines() == ["boom ''' fenced", "error: first", "error: second"]


# --------------------------------------------------------------------------
# Workflow wiring
# --------------------------------------------------------------------------


def test_reporter_job_names_match_the_workflow() -> None:
    assert JOBS["prepare"]["name"] == PREPARE_JOB
    assert JOBS["tag"]["name"] == TAG_JOB
    assert rc.JOB_KINDS == {PREPARE_JOB: "prepare", TAG_JOB: "tag"}
    for job in ("prepare", "tag"):
        assert rc.NATIVE_STEP in [s.get("name") for s in JOBS[job]["steps"]]


def test_reporter_job_runs_on_failure_with_the_workflow_issues_token() -> None:
    report = JOBS["report-failure"]
    assert report["needs"] == ["prepare", "tag"]
    assert "always()" in report["if"]
    assert "needs.prepare.result == 'failure'" in report["if"]
    assert "needs.tag.result == 'failure'" in report["if"]
    assert report["permissions"]["issues"] == "write"
    assert report["permissions"]["actions"] == "read"
    # The workflow token, never the release-PR credential that may be the thing
    # that failed.
    assert report["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "RELEASE_BOT_TOKEN" not in yaml.safe_dump(report)
    assert "release_ci.py report-failure" in yaml.safe_dump(report)


def test_native_gate_keeps_the_failed_job_conclusion_in_both_jobs() -> None:
    for job in ("prepare", "tag"):
        gate = _step(job, rc.NATIVE_STEP)
        assert 'exit "$status"' in gate["run"], f"{job}: gate must fail the job"
    assert "Report blocked release (single tracking issue)" not in WORKFLOW_TEXT
    assert "Report blocked tag (single tracking issue)" not in WORKFLOW_TEXT


def test_prepare_publishes_through_the_recovering_helper() -> None:
    step = _step(PREPARE_JOB, PUSH_STEP)
    assert "release_ci.py publish" in step["run"]
    assert "!inputs.dry_run" in step["if"]
    assert "gh pr create" not in WORKFLOW_TEXT
    assert 'git push origin "release/v$v"' not in WORKFLOW_TEXT
    assert JOBS["prepare"]["permissions"]["actions"] == "read"  # lag lookup


def test_tag_push_and_publish_dispatch_are_separate_steps() -> None:
    assert _index(TAG_JOB, TAG_STEP) < _index(TAG_JOB, DISPATCH_STEP)
    assert "git push origin" in _step(TAG_JOB, TAG_STEP)["run"]
    assert "gh workflow run publish.yml" in _step(TAG_JOB, DISPATCH_STEP)["run"]
