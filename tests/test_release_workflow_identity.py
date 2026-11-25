"""Workflow-identity guard for ``release.yml``'s publishing path
(issue #5005): drives the checks in ``_release_workflow_checks.py`` against the
real workflow, then re-runs them against deliberately broken copies and
requires each mutation to fail on the specific check that guards it, so a
vacuous guard cannot hide.

The tag job's own guards and behaviour live in
``test_release_workflow_tag_job.py``; the shared helpers in
``_release_workflow_support.py``.
"""

from __future__ import annotations

import re

from _release_workflow_checks import run_all_checks
from _release_workflow_support import (
    WORKFLOW,
    comment_out_first,
    expect_failure,
    strip_comments,
)


def test_workflow_identity() -> None:
    result = run_all_checks(WORKFLOW.read_text())
    assert not result.failures, "\n".join(
        f"FAIL {case}: {detail}" for case, detail in result.failures
    )


# --------------------------------------------------------------------------- #
# Mutation controls: each mutation must be caught by the named check.
# --------------------------------------------------------------------------- #


def _expect_failure(mutation: str, needle_case: str) -> None:
    expect_failure(run_all_checks, mutation, needle_case)


def test_mutation_controls() -> None:
    text = WORKFLOW.read_text()

    # Publisher wiring.
    _expect_failure(
        text.replace(
            "PUBLISH_URL: https://fleet-publish.2amlogic.com/v1/publish_version_bump",
            "PUBLISH_URL: https://evil.example.com/v1/publish_version_bump",
        ),
        "1:",
    )
    _expect_failure(
        text.replace(
            "&audience=https://fleet-publish.2amlogic.com",
            "&audience=https://other.example.com",
        ),
        "1:",
    )
    # Least privilege: id-token at workflow level.
    _expect_failure(
        re.sub(
            r"(^permissions:\n  contents: read\n)",
            r"\1  id-token: write\n",
            text,
            count=1,
            flags=re.M,
        ),
        "2:",
    )
    # A direct-push fallback after a refusal.
    _expect_failure(
        text.replace(
            'die "the publisher refused the bump: $outcome"',
            "git push origin HEAD:main",
        ),
        "3:",
    )
    # An action smuggled into the publish job.
    smuggled = text.replace(
        "      - name: Publish version bump (re-checks the gate on the current tip)",
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7\n"
        "      - name: Publish version bump (re-checks the gate on the current tip)",
    )
    _expect_failure(smuggled, "4:")
    # A secrets. read reappears.
    _expect_failure(
        text.replace(
            "GH_TOKEN: ${{ github.token }}", "GH_TOKEN: ${{ secrets.SOMETHING }}"
        ),
        "1:",
    )
    # The push path filter widened past src/**.
    _expect_failure(
        text.replace('      - "src/**"', '      - "src/**"\n      - "pyproject.toml"'),
        "6:",
    )
    # A gh call leaks into a bump-publishing job (the check had matched only a
    # `gh` at the very start of a line, so it could never fail).
    anchor = "          # The only paths the publisher accepts."
    _expect_failure(
        text.replace(anchor, '          x="$(gh api user)"\n' + anchor),
        "9:",
    )
    # A commented-out guard line (comment-blindness).
    for needle, case in [
        ("id-token: write", "2:"),
        ("persist-credentials: false", "4:"),
        ('echo "::add-mask::$oidc"', "5:"),
        ("attempts=5", "7:"),
        ("bash scripts/version-bump-gate.sh", "9:"),
    ]:
        _expect_failure(comment_out_first(text, needle), case)


def test_comment_stripper() -> None:
    assert strip_comments("      # id-token: write")[0] == ""
    assert (
        strip_comments("          fetch-depth: 1 # id-token: write")[0]
        == "          fetch-depth: 1"
    )
    assert (
        strip_comments('          run: echo "a # b" # c')[0]
        == '          run: echo "a # b"'
    )
    assert (
        strip_comments('          run: echo \'x#y\' "q\\"#z" a#b # gone')[0]
        == '          run: echo \'x#y\' "q\\"#z" a#b'
    )
