"""The publishing-path identity checks for ``release.yml`` (issue #5005),
mirroring Loom's ``.loom/scripts/tests/test-version-bump-workflow-identity.sh``
(issues #7743/#7829/#12063 there). Driven by
``test_release_workflow_identity.py``, which also runs the mutation controls.

Every property below is invisible at a glance and silently reopens a hole if
dropped: an App-token step put back "to be safe", a ``git push`` fallback
after a publisher refusal, ``id-token: write`` moved to a job that runs
third-party actions, or the push ``paths:`` filter widened past ``src/**``
(which is the real self-trigger guard -- the publisher's App commits DO
trigger workflows).

Comment-blindness: every assertion about what the workflow DOES runs against
a comment-stripped view of the file; a commented-out line cannot satisfy a
check.

Scoping note (a deliberate divergence from Loom's suite): the workflow also
carries the ``tag`` job, which legitimately pushes a tag and uses
``github.token`` (its own checks live in ``test_release_workflow_tag_job.py``).
The strict assertions ("no git push", "no token") are therefore scoped to the
bump-publishing jobs (``bump``, ``publish-bump``); the file-wide ones assert
only what must hold everywhere (no ``secrets.`` read, one ``id-token: write``,
read-only default).
"""

from __future__ import annotations

import re

from _release_workflow_support import (
    BUMP_JOB,
    GATE,
    PUBLISH_JOB,
    TAG_JOB,
    Result,
    job_block,
    line_of,
    strip_comments,
)


def run_all_checks(text: str) -> Result:
    r = Result()
    code = strip_comments(text)
    joined = "\n".join(code)
    publish = job_block(code, PUBLISH_JOB)
    bump = job_block(code, BUMP_JOB)
    tag = job_block(code, TAG_JOB)
    publish_text = "\n".join(publish)
    bump_text = "\n".join(bump)

    # --- Case 1: publisher wiring -----------------------------------------
    url_match = re.search(
        r"PUBLISH_URL: (https://[a-z0-9.-]+)/v1/publish_version_bump", publish_text
    )
    r.check(
        url_match is not None,
        "1: the publish job targets an https /v1/publish_version_bump URL",
        "no PUBLISH_URL: https://…/v1/publish_version_bump in the publish job",
    )
    origin = url_match.group(1) if url_match else "https://<none>"
    r.check(
        f'"$ACTIONS_ID_TOKEN_REQUEST_URL&audience={origin}"' in publish_text,
        "1: the OIDC token is requested with the publisher's origin as audience",
        f'no "$ACTIONS_ID_TOKEN_REQUEST_URL&audience={origin}" request',
    )
    r.check(
        '"$PUBLISH_URL")"' in publish_text,
        "1: the publish request goes to PUBLISH_URL",
    )
    r.check(
        "create-github-app-token" not in joined,
        "1: no App token is minted in the workflow",
    )
    r.check(
        "secrets." not in joined,
        "1: the workflow reads no secret (the publisher holds the only key)",
    )
    r.check(
        re.search(r"(4486636|BEGIN [A-Z ]*PRIVATE KEY)", joined) is None,
        "1: no hardcoded App id or private-key material in the workflow",
    )

    # --- Case 2: least privilege ------------------------------------------
    default_perms = ""
    m = re.search(r"^permissions:\n((?:  .*\n)+)", joined, re.M)
    if m:
        default_perms = m.group(1)
    r.check(
        "contents: read" in default_perms,
        "2: workflow-level default token is read-only (contents: read)",
        f"entries under permissions: were {default_perms!r}",
    )
    contents_write_jobs = [
        job
        for job in (PUBLISH_JOB, BUMP_JOB, TAG_JOB)
        if re.search(r"^ +contents: write$", "\n".join(job_block(code, job)), re.M)
    ]
    r.check(
        contents_write_jobs == [TAG_JOB],
        "2: contents: write is granted only to the tag job",
        f"contents: write found in: {contents_write_jobs}",
    )
    all_idtoken = re.findall(r"^ *id-token: write$", joined, re.M)
    publish_idtoken = re.findall(r"^ *id-token: write$", publish_text, re.M)
    r.check(
        len(all_idtoken) == 1 and len(publish_idtoken) == 1,
        "2: id-token: write is granted exactly once, to the publish job",
        f"{len(all_idtoken)} in the file, {len(publish_idtoken)} in the publish job",
    )

    # --- Case 3: no direct push path in the bump path ---------------------
    r.check(
        "git push" not in publish_text and "git push" not in bump_text,
        "3: the bump path pushes nothing (no fallback to a direct push)",
    )
    r.check(
        "secrets.GITHUB_TOKEN" not in joined,
        "3: no executable reference to secrets.GITHUB_TOKEN",
    )
    r.check(
        "github.token" not in publish_text and "github.token" not in bump_text,
        "3: the bump path reads no github.token",
    )
    r.check(
        "github-actions[bot]" not in bump_text
        and "github-actions[bot]" not in publish_text,
        "3: the bump path never commits as github-actions[bot]",
    )
    tag_pushes = [
        line.strip() for line in tag if "git push" in line and "refs/tags/" not in line
    ]
    r.check(
        not tag_pushes,
        "3: the tag job pushes only tag refs",
        f"non-tag pushes: {tag_pushes}",
    )
    r.check(
        "git commit" not in joined,
        "3: the workflow commits nothing itself (the publisher builds the commit)",
    )

    # --- Case 4: the minting job is minimal -------------------------------
    r.check(
        bool(publish) and not re.search(r"^ *(- )?uses:", publish_text, re.M),
        "4: the publish job runs no action (no uses:)",
    )
    r.check(
        any(line.strip() == f"needs: {BUMP_JOB}" for line in publish),
        "4: the publish job needs bump",
    )
    r.check(
        any(
            line.strip() == f"if: needs.{BUMP_JOB}.outputs.decision == 'bump'"
            for line in publish
        ),
        "4: the publish job runs only on a bump verdict",
    )
    r.check(
        "uses: actions/checkout@" in bump_text,
        "4: checkout is in the bump job (the job with no id-token)",
    )
    r.check(
        "persist-credentials: false" in bump_text,
        "4: checkout does not persist a token (nothing in the bump path pushes)",
    )

    # --- Case 5: failure behaviour and credentials -------------------------
    run_blocks = re.findall(r"^ *run: \|\n((?:(?! *run: ).*\n)*)", joined, re.M)
    strict = sum(
        1
        for block in run_blocks
        if block.split("\n", 1)[0].strip() == "set -euo pipefail"
    )
    r.check(
        bool(run_blocks) and strict == len(run_blocks),
        "5: every run: script starts with set -euo pipefail",
        f"{strict} of {len(run_blocks)} run: blocks start with set -euo pipefail",
    )
    config_stdin = len(re.findall(r"curl .*--config -", publish_text))
    r.check(
        config_stdin == 2,
        "5: both curl calls read their Authorization header from stdin (--config -)",
        f"found {config_stdin}, expected 2",
    )
    r.check(
        re.search(r"(-H|--header) +[\"']?Authorization", joined) is None,
        "5: no Authorization header is passed to curl in argv",
    )
    for bearer in ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "oidc"):
        r.check(
            "printf 'header = \"Authorization" in publish_text
            and f'"${bearer}"' in publish_text,
            f"5: ${bearer} reaches curl's stdin through printf, not argv",
        )
    r.check(
        'echo "::add-mask::$oidc"' in publish_text,
        "5: the minted OIDC token is masked in the log",
    )

    # --- Case 6: the no-self-trigger invariant -----------------------------
    trigger_paths = []
    m = re.search(r"^on:\n(.*?)(?=^  \w|^\w)", joined + "\n", re.M | re.S)
    r.check(
        any(line.strip() == "branches: [main]" for line in code),
        "6: trigger is restricted to pushes on main",
    )
    in_paths = False
    for line in code:
        if re.match(r"^ {4}paths:$", line):
            in_paths = True
            continue
        if in_paths:
            m2 = re.match(r'^ {6}- "?([^"#\s]+)"?$', line)
            if m2:
                trigger_paths.append(m2.group(1))
            else:
                in_paths = False
    r.check(
        trigger_paths == ["src/**"],
        "6: the push path filter is exactly src/**",
        f"parsed: {trigger_paths}; the bump commit touches only pyproject.toml"
        " and CHANGELOG.md, but this filter is the self-trigger loop guard"
        " and must not grow them",
    )
    gate_paths = re.search(r"^RELEASE_PATHS=\((.*)\)$", GATE.read_text(), re.M)
    r.check(
        gate_paths is not None and gate_paths.group(1) == "src/ pyproject.toml",
        "6: the gate's RELEASE_PATHS is src/ and pyproject.toml",
        f"scripts/version-bump-gate.sh RELEASE_PATHS was {gate_paths!r}",
    )
    version_file_sets = re.findall(r"files=\((.*?)\)", publish_text + bump_text, re.S)
    r.check(
        len(version_file_sets) == 2
        and all(s.strip() == "pyproject.toml CHANGELOG.md" for s in version_file_sets),
        "6: the version-file set is exactly pyproject.toml CHANGELOG.md",
        f"parsed: {version_file_sets!r}",
    )
    r.check(
        "CHANGELOG.md" not in (gate_paths.group(1) if gate_paths else ""),
        "6: CHANGELOG.md is not a release path (the bump commit rewrites it)",
    )

    # --- Case 7: concurrency and retry bound -------------------------------
    r.check(
        "group:" in joined and "'bump'" in joined and "'tag-main'" in joined,
        "7: runs serialize in per-regime concurrency groups",
    )
    r.check(
        "cancel-in-progress: false" in joined,
        "7: queued runs are never cancelled (each re-checks the gate)",
    )
    r.check(
        "attempts=5" in publish_text,
        "7: the bounded publish retry is still 5 attempts",
    )
    r.check(
        "git checkout --quiet -f -B main origin/main" in publish_text,
        "7: each attempt re-syncs to the current tip of main",
    )

    # --- Case 9: the release-cadence gate ----------------------------------
    r.check(
        re.search(r"^  schedule:$", joined, re.M) is not None,
        "9: a schedule trigger exists (catch-up for a quiet day)",
    )
    r.check(
        re.search(r'^    - cron: "[0-9]+ \* \* \* \*"$', joined, re.M) is not None,
        "9: the catch-up cron is hourly (worst case = interval + ~1 h)",
    )
    r.check(
        re.search(r"^  workflow_dispatch:$", joined, re.M) is not None,
        "9: a workflow_dispatch trigger exists (operator paths)",
    )
    r.check(
        'workflows: ["CI"]' in joined and "types: [completed]" in joined,
        "9: the tag trigger is workflow_run on CI completing",
    )
    r.check(
        "fetch-depth: 0" in bump_text,
        "9: checkout fetches full history (the last bump may be weeks back)",
    )
    r.check(
        "filter: blob:none" in bump_text,
        "9: the full-history checkout is a blob:none partial clone",
    )
    r.check(
        re.search(r"fetch-depth: [1-9]", joined) is None
        and re.search(r"git clone .*--depth", joined) is None,
        "9: no shallow checkout or clone anywhere",
    )
    gate_step = re.search(r"id: gate\n((?:(?!      - ).*\n)*)", bump_text)
    r.check(
        gate_step is not None
        and "bash scripts/version-bump-gate.sh" in gate_step.group(1),
        "9: the gate step runs scripts/version-bump-gate.sh",
    )
    gate_line = line_of(code, "id: gate")
    compute_line = line_of(code, "name: Compute the version bump (no commit, no push)")
    r.check(
        gate_line is not None and compute_line is not None and gate_line < compute_line,
        "9: the gate runs before the bump computation",
    )
    loop_start = line_of(code, "for attempt in")
    post_line = line_of(code, '"$PUBLISH_URL")"')
    in_loop = code[loop_start - 1 : post_line] if loop_start and post_line else []
    r.check(
        any("bash scripts/version-bump-gate.sh" in line for line in in_loop),
        "9: the gate is re-checked inside the publish loop, before each POST",
    )
    reason_hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(code, 1)
        if "inputs.reason" in line or "inputs.version" in line
    ]
    bad_reason = [
        hit for hit in reason_hits if not re.match(r"^\d+: \w+: \$\{\{.*\}\}$", hit)
    ]
    r.check(
        not bad_reason,
        "9: inputs.reason / inputs.version reach a shell only through env:",
        f"template injection: {bad_reason}",
    )
    interval_hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(code, 1)
        if "RELEASE_MIN_INTERVAL:" in line
    ]
    bad_interval = [
        hit
        for hit in interval_hits
        if not re.match(
            r"^\d+: RELEASE_MIN_INTERVAL: \$\{\{ vars\.RELEASE_MIN_INTERVAL \}\}$", hit
        )
    ]
    r.check(
        not bad_interval,
        "9: RELEASE_MIN_INTERVAL comes only from vars.RELEASE_MIN_INTERVAL",
        f"{bad_interval}",
    )
    r.check(
        'message="chore: bump version to $BUMP_VERSION"' in publish_text,
        "9: the message's first line is exactly the publisher's fixed subject",
    )
    r.check(
        "message+=$'\\n\\n'\"Forced-by: $ACTOR\"" in publish_text,
        "9: a forced bump's message records the actor",
    )
    r.check(
        '>>"$GITHUB_STEP_SUMMARY"' in bump_text,
        "9: the gate verdict is written to the job summary",
    )
    r.check(
        '--ref origin/main)" || rc=$?' in bump_text,
        "9: a failing gate's reason is surfaced (exit code captured, not set -e)",
    )
    # A lookbehind, not a POSIX class (`[[:alnum:]]` is not Python regex and
    # made this pattern match only at the very start of a line): `ghost` and
    # `x.gh` stay out, `$(gh issue list)` and an indented `gh pr view` count.
    gh_call = re.compile(
        r"(?<![\w.-])gh (api|pr|issue|release|run|workflow|repo|label|search)\b"
    )
    gh_outside_tag = [line.strip() for line in bump + publish if gh_call.search(line)]
    r.check(
        not gh_outside_tag,
        "9: no gh call outside the tag job",
        f"gh calls in the bump-publishing jobs: {gh_outside_tag}",
    )

    # --- Case 10: the publisher's response contract ------------------------
    for arm in (
        r"200:\*\)",
        r"409:\* \| 503:upstream_ambiguous \| 000:\*\)",
        r"\*\)",
    ):
        r.check(
            re.search(rf"^ +{arm}$", publish_text, re.M) is not None,
            f"10: case arm present: {arm}",
        )
    catchall = re.search(r"^ +\*\)\n(.+)$", publish_text, re.M)
    r.check(
        catchall is not None
        and 'die "the publisher refused the bump' in catchall.group(1),
        "10: an answer the contract does not name fails red",
    )

    return r
