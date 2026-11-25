"""The tag job reports its own failures (issue #5005, "keeps its single tracking
issue and the new ``tag`` job reports its own failure there").

Before this, only a failed native-extras gate told anyone: a publish dispatch
that failed AFTER the tag was pushed, or a publish-run lookup that failed while
recovering one, ended the job red and silent -- a stranded release found by
reading Actions. These tests run the workflow's own ``Report a failed tag job``
step (see ``_release_workflow_flow.py``) against a stub forge; the issue the
stub receives is what a maintainer would read.
"""

from __future__ import annotations

import json
import os
import subprocess

from _release_workflow_flow import (
    NATIVE,
    REPORT,
    RESOLVE,
    TAGGING,
    clone,
    gh_calls,
    issue_call,
    origin,
    push_tag,
    reject_pushes,
    run_flow,
    step_env,
    step_if,
    step_script,
    stub_gh,
    tags,
    truthy,
    workflow_steps,
)

TITLE = "Automated release blocked: tag job failed"
NATIVE_TITLE = "Automated release blocked: klt-*-native pin not published"
RUN_URL = "https://forge.test/o/r/actions/runs/4242"
NATIVE_REPORT = "Report blocked release (single tracking issue)"


def _issues(*rows: tuple[int, str]) -> str:
    return json.dumps([{"number": n, "title": t} for n, t in rows])


def test_a_dispatch_that_fails_after_the_tag_push_is_reported(tmp_path):
    bare, sha = origin(tmp_path)
    stub_gh(tmp_path)

    flow = run_flow(tmp_path, clone(tmp_path, bare, "run1"), sha, GH_DISPATCH_FAIL="1")

    assert flow.tag is not None and flow.tag.returncode != 0
    assert flow.report is not None and flow.report.returncode == 0, flow.report
    assert "refs/tags/v0.9.3" in tags(bare)
    created = issue_call(tmp_path, "create")
    assert created is not None and issue_call(tmp_path, "edit") is None
    assert created["--title"] == TITLE
    body = created["--body"]
    assert f'failed at step "{TAGGING}" for v0.9.3' in body
    assert "already pushed, but publish.yml was not dispatched" in body
    assert "never re-tags" in body
    assert f"Run: {RUN_URL}" in body


def test_a_failed_tag_push_is_told_apart_from_a_failed_dispatch(tmp_path):
    bare, sha = origin(tmp_path)
    stub_gh(tmp_path)
    work = clone(tmp_path, bare, "run1")
    reject_pushes(bare)

    flow = run_flow(tmp_path, work, sha)

    assert flow.tag is not None and flow.tag.returncode != 0
    assert tags(bare) == ""  # the push really failed
    assert not any(c.startswith("gh workflow run") for c in gh_calls(tmp_path))
    created = issue_call(tmp_path, "create")
    assert created is not None
    body = created["--body"]
    assert f'failed at step "{TAGGING}" for v0.9.3' in body
    assert "push did not complete" in body and "left no tag" in body
    assert "already pushed" not in body
    assert f"Run: {RUN_URL}" in body


def test_a_publish_run_lookup_that_fails_during_recovery_is_reported(tmp_path):
    bare, sha = origin(tmp_path)
    stub_gh(tmp_path)
    push_tag(tmp_path, bare, sha, "v0.9.3")

    flow = run_flow(tmp_path, clone(tmp_path, bare, "run1"), sha, GH_RUN_LIST_FAIL="1")

    assert flow.resolve.returncode != 0 and flow.tag is None
    created = issue_call(tmp_path, "create")
    assert created is not None
    body = created["--body"]
    assert f'failed at step "{RESOLVE}" for v0.9.3' in body  # named although
    assert "This run pushed no tag." in body  # `version` was never set
    assert f"Run: {RUN_URL}" in body


def test_a_second_failure_refreshes_the_single_tracking_issue(tmp_path):
    bare, sha = origin(tmp_path)
    stub_gh(tmp_path)
    # An open issue the native-extras report filed earlier, and an unrelated one.
    existing = _issues((5, "Something unrelated"), (77, NATIVE_TITLE))

    run_flow(
        tmp_path,
        clone(tmp_path, bare, "run1"),
        sha,
        GH_DISPATCH_FAIL="1",
        GH_ISSUES_JSON=existing,
    )

    assert issue_call(tmp_path, "create") is None  # never a second issue
    edited = issue_call(tmp_path, "edit")
    assert edited is not None and edited["--title"] == TITLE
    assert "already pushed" in edited["--body"]
    argv = (tmp_path / "issue-edit.argv").read_bytes().decode().split("\0")
    assert argv[:3] == ["issue", "edit", "77"]


def test_the_native_gate_report_finds_the_issue_this_report_filed(tmp_path):
    """The other direction: a native-extras failure after a tag-job failure must
    refresh that issue, not open a second one."""
    stub_gh(tmp_path)
    (tmp_path / "native.log").write_text("pin klt-yield-native==0.1.0 missing\n")
    step = workflow_steps()[NATIVE_REPORT]
    script = step_script(step).replace("${{ steps.resolve.outputs.version }}", "0.9.3")
    assert "${{" not in script, "teach this test the new expression"

    proc = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_SERVER_URL": "https://forge.test",
            "GITHUB_RUN_ID": "4242",
            "RUNNER_TEMP": str(tmp_path),
            "GH_ISSUES_JSON": _issues((9, TITLE)),
        },
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1  # reports, then still fails the job
    assert issue_call(tmp_path, "create") is None
    edited = issue_call(tmp_path, "edit")
    assert edited is not None and edited["--title"] == NATIVE_TITLE
    assert "pin klt-yield-native==0.1.0 missing" in edited["--body"]


def test_a_healthy_run_files_nothing(tmp_path):
    bare, sha = origin(tmp_path)
    stub_gh(tmp_path)

    flow = run_flow(tmp_path, clone(tmp_path, bare, "run1"), sha)

    assert flow.tag is not None and flow.tag.returncode == 0
    assert flow.report is None
    assert not any(c.startswith("gh issue") for c in gh_calls(tmp_path))


def test_the_native_gate_failure_is_not_reported_twice():
    """The native gate reports itself (and exits 1); the generic report must not
    then overwrite that pin-specific report."""
    condition = step_if(workflow_steps()[REPORT])

    assert truthy(condition, {"native": {"ok": "false"}}, failed=True) is False
    assert truthy(condition, {"native": {"ok": "true"}}, failed=True) is True
    assert truthy(condition, {}, failed=True) is True  # native never ran
    assert truthy(condition, {"native": {"ok": "true"}}, failed=False) is False


def test_report_step_wiring():
    steps = workflow_steps()
    report = steps[REPORT]
    script = step_script(report)
    prefix = 'prefix="Automated release blocked:"'

    # Both reports share one title prefix: that is what makes it ONE issue.
    assert prefix in script and prefix in step_script(steps[NATIVE_REPORT])
    # It lists the failed step by the names the job really has.
    for name in (RESOLVE, NATIVE, TAGGING):
        assert f'"{name}"' in script, name
        assert name in steps
    # Every outcome / output it reads is wired from a real step id.
    wired = " ".join(step_env(report).values())
    for expr in (
        "steps.resolve.outputs.candidate",
        "steps.tag.outputs.tag_pushed",
        "steps.tag.outcome",
        "steps.resolve.outcome",
        "steps.native.outcome",
        "steps.checkout.outcome",
        "steps.uv.outcome",
    ):
        assert expr in wired, expr
    # It is the job's last step, so it sees every earlier outcome.
    assert list(steps)[-1] == REPORT
