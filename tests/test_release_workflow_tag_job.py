"""Guards for ``release.yml``'s ``tag`` job (issue #5005).

Two layers:

* ``run_tag_checks`` -- comment-blind text checks that the tag job only tags a
  green, same-repo push to main, gates on the native extras, scopes its push to
  ``refs/tags/`` and keeps its dispatch-recovery wiring.
* behavioural tests that execute the workflow's OWN ``run:`` blocks (and
  evaluate its OWN ``if:`` conditions) against a local bare origin and a stub
  ``gh`` (``_release_workflow_flow.py``). They exist because of a real
  failure: the first run pushed the tag and then the dispatch failed; the rerun
  saw the tag, resolved an empty version and skipped the dispatch forever -- a
  release with a tag and no publish run. Zero GitHub or PyPI writes: ``gh`` is
  a stub and the origin is a directory under ``tmp_path``.
"""

from __future__ import annotations

import re

from _release_workflow_flow import (
    DISPATCH,
    RESOLVE,
    clone,
    gh_calls,
    origin,
    push_tag,
    run_flow,
    stub_gh,
    tags,
)
from _release_workflow_support import (
    TAG_JOB,
    WORKFLOW,
    Result,
    comment_out_first,
    expect_failure,
    job_block,
    strip_comments,
)


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

    # --- Case 13: the tag push has Git credentials --------------------------
    # `GH_TOKEN` authenticates `gh`, never `git`. With checkout's
    # `persist-credentials: false` and no auth on the push itself, the first
    # real release fails at `git push` (the behavioural tests push to a local
    # bare origin, which needs no credentials, so they cannot see this).
    checkout = re.search(
        r"- uses: actions/checkout@\S+\n((?:(?!      - ).*\n)*)", tag_text
    )
    r.check(checkout is not None, "13: the tag job checks the repository out")
    keeps_credentials = (
        checkout is not None and "persist-credentials: false" not in checkout.group(1)
    )
    explicit_auth = bool(re.search(r"extraheader|x-access-token", tag_text))
    r.check(
        keeps_credentials or explicit_auth,
        "13: the tag push is authenticated",
        "checkout sets persist-credentials: false and nothing else gives "
        "`git push` a credential",
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
    # The reported defect: credentials stripped from the job that pushes.
    stripped = text.replace(
        "persist-credentials: true", "persist-credentials: false", 1
    )
    assert stripped != text, "stale control: the tag checkout no longer persists"
    expect_failure(run_tag_checks, stripped, "13:")


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #


def test_fresh_release_tags_and_dispatches(tmp_path):
    bare, sha = origin(tmp_path)
    stub_gh(tmp_path)

    flow = run_flow(tmp_path, clone(tmp_path, bare, "run1"), sha)

    assert flow.outputs["resolve"] == {
        "version": "0.9.3",
        "tag_exists": "false",
        "candidate": "0.9.3",
    }
    assert flow.native_ran
    assert flow.tag is not None and flow.tag.returncode == 0, flow.tag
    assert "refs/tags/v0.9.3" in tags(bare)
    assert gh_calls(tmp_path) == [DISPATCH]  # no run-list lookup on the fresh path


def test_rerun_after_a_failed_dispatch_redispatches_without_retagging(tmp_path):
    """The reported failure: tag pushed, dispatch failed, rerun skipped it."""
    bare, sha = origin(tmp_path)
    stub_gh(tmp_path)

    first = run_flow(tmp_path, clone(tmp_path, bare, "run1"), sha, GH_DISPATCH_FAIL="1")
    assert first.tag is not None and first.tag.returncode != 0
    stranded = tags(bare)
    assert "refs/tags/v0.9.3" in stranded  # tagged, publish never dispatched

    second = run_flow(tmp_path, clone(tmp_path, bare, "run2"), sha, GH_RUN_COUNT="0")

    assert second.outputs["resolve"] == {
        "version": "0.9.3",
        "tag_exists": "true",
        "candidate": "0.9.3",
    }
    assert not second.native_ran  # it passed when the tag was cut
    assert second.tag is not None and second.tag.returncode == 0, second.tag
    assert tags(bare) == stranded  # never re-tagged
    dispatches = [c for c in gh_calls(tmp_path) if c.startswith("gh workflow run")]
    assert dispatches == [DISPATCH, DISPATCH]


def test_a_tag_whose_publish_already_ran_is_left_alone(tmp_path):
    bare, sha = origin(tmp_path)
    stub_gh(tmp_path)
    push_tag(tmp_path, bare, sha, "v0.9.3")

    flow = run_flow(tmp_path, clone(tmp_path, bare, "run1"), sha, GH_RUN_COUNT="1")

    assert flow.outputs["resolve"] == {
        "version": "",
        "tag_exists": "false",
        "candidate": "0.9.3",
    }
    assert flow.tag is None
    assert not any(c.startswith("gh workflow run") for c in gh_calls(tmp_path))


def test_an_older_tag_a_newer_release_has_passed_is_never_revived(tmp_path):
    bare, sha = origin(tmp_path)
    stub_gh(tmp_path)
    push_tag(tmp_path, bare, sha, "v0.9.3")
    push_tag(tmp_path, bare, sha, "v0.9.4")

    flow = run_flow(tmp_path, clone(tmp_path, bare, "run1"), sha, GH_RUN_COUNT="0")

    assert flow.outputs["resolve"]["version"] == ""
    assert flow.tag is None
    assert gh_calls(tmp_path) == []


def test_an_unreadable_run_list_fails_red_instead_of_reading_as_released(tmp_path):
    bare, sha = origin(tmp_path)
    stub_gh(tmp_path)
    push_tag(tmp_path, bare, sha, "v0.9.3")

    failing = run_flow(
        tmp_path, clone(tmp_path, bare, "run1"), sha, GH_RUN_LIST_FAIL="1"
    )
    garbled = run_flow(
        tmp_path, clone(tmp_path, bare, "run2"), sha, GH_RUN_COUNT="oops"
    )

    assert failing.resolve.returncode != 0 and failing.tag is None
    assert garbled.resolve.returncode != 0 and garbled.tag is None
    assert "cannot tell whether publish.yml ran" in garbled.resolve.stdout
