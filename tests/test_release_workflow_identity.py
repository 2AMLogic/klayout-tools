"""Workflow-identity guard for ``release.yml``'s publishing path
(issue #5005), mirroring Loom's
``.loom/scripts/tests/test-version-bump-workflow-identity.sh`` (issues
#7743/#7829/#12063 there).

Every property below is invisible at a glance and silently reopens a hole if
dropped: an App-token step put back "to be safe", a ``git push`` fallback
after a publisher refusal, ``id-token: write`` moved to a job that runs
third-party actions, or the push ``paths:`` filter widened past ``src/**``
(which is the real self-trigger guard -- the publisher's App commits DO
trigger workflows).

Comment-blindness: every assertion about what the workflow DOES runs against
a comment-stripped view of the file; a commented-out line cannot satisfy a
check. Mutation controls re-run the whole suite against deliberately broken
copies and require each to fail on the specific check that guards it, so a
vacuous guard cannot hide.

Scoping note (a deliberate divergence from Loom's suite): this file also
carries the ``tag`` job, which legitimately pushes a tag and uses
``github.token``. The strict assertions ("no git push", "no token") are
therefore scoped to the bump-publishing jobs (``bump``, ``publish-bump``);
the file-wide ones assert only what must hold everywhere (no ``secrets.``
read, one ``id-token: write``, read-only default).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"
)
PUBLISH_JOB = "publish-bump"
BUMP_JOB = "bump"
TAG_JOB = "tag"
GATE = Path(__file__).resolve().parent.parent / "scripts" / "version-bump-gate.sh"


def strip_comments(text: str) -> list[str]:
    """The executable view of the workflow: full-line and trailing ``#``
    comments blanked/cut (outside quotes; a ``#`` glued to a word stays),
    line count preserved so diagnostics point at the real file."""
    out = []
    for line in text.split("\n"):
        buf = ""
        in_single = in_double = False
        i = 0
        while i < len(line):
            ch = line[i]
            if in_double and ch == "\\" and i + 1 < len(line):
                buf += ch + line[i + 1]
                i += 2
                continue
            if ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "'" and not in_double:
                in_single = not in_single
            elif ch == "#" and not in_single and not in_double:
                if i == 0 or line[i - 1] in " \t":
                    break
            buf += ch
            i += 1
        out.append(buf.rstrip(" \t"))
    return out


def job_block(code: list[str], job: str) -> list[str]:
    """The executable lines of one job, from ``  <job>:`` under ``jobs:`` to
    the next job (or EOF)."""
    lines = []
    in_jobs = False
    started = False
    for line in code:
        if line == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line == f"  {job}:":
            started = True
            continue
        if started and re.match(r"^  [A-Za-z0-9_-]+:$", line):
            break
        if started:
            lines.append(line)
    return lines


@dataclass
class Result:
    failures: list[tuple[str, str]] = field(default_factory=list)
    passes: list[str] = field(default_factory=list)

    def check(self, ok: bool, case: str, detail: str = "") -> None:
        if ok:
            self.passes.append(case)
        else:
            self.failures.append((case, detail))


def _code(text: str) -> list[str]:
    return strip_comments(text)


def _line_of(code: list[str], needle: str) -> int | None:
    for n, line in enumerate(code, 1):
        if needle in line:
            return n
    return None


def run_all_checks(text: str) -> Result:
    r = Result()
    code = _code(text)
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
    gate_line = _line_of(code, "id: gate")
    compute_line = _line_of(code, "name: Compute the version bump (no commit, no push)")
    r.check(
        gate_line is not None and compute_line is not None and gate_line < compute_line,
        "9: the gate runs before the bump computation",
    )
    loop_start = _line_of(code, "for attempt in")
    post_line = _line_of(code, '"$PUBLISH_URL")"')
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
    gh_pat = (
        r"(^|[^[:alnum:]_.-])gh"
        r" (api|pr|issue|release|run|workflow|repo|label|search)( |$)"
    )
    gh_calls = re.findall(gh_pat, joined)
    r.check(
        not gh_calls
        or all(
            line.strip().startswith(("gh workflow run publish.yml", "gh issue"))
            for line in tag
            if re.search(r"(^|[^[:alnum:]_.-])gh (workflow|issue) ", line)
        ),
        "9: no gh call outside the tag job",
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

    # --- Case 11 (klayout-tools): the tag job guards ------------------------
    tag_text = "\n".join(tag)
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
        r"name: Resolve the release this green commit carries\n((?:(?!      - ).*\n)*)",
        tag_text,
    )
    r.check(
        resolve_step is not None
        and "already exists; not tagging" in resolve_step.group(1),
        "11: a tag that already exists is skipped",
    )
    r.check(
        resolve_step is not None and "no longer on main" in resolve_step.group(1),
        "11: a commit no longer on main is not tagged",
    )
    return r


def test_workflow_identity() -> None:
    result = run_all_checks(WORKFLOW.read_text())
    assert not result.failures, "\n".join(
        f"FAIL {case}: {detail}" for case, detail in result.failures
    )


# --------------------------------------------------------------------------- #
# Mutation controls: each mutation must be caught by the named check.
# --------------------------------------------------------------------------- #


def _comment_out_first(text: str, needle: str) -> str:
    out = []
    done = False
    for line in text.split("\n"):
        if not done and needle in line and not line.lstrip().startswith("#"):
            out.append("# " + line)
            done = True
        else:
            out.append(line)
    assert done, f"no executable line contains {needle!r} -- stale control"
    return "\n".join(out)


def _expect_failure(mutation: str, needle_case: str) -> None:
    result = run_all_checks(mutation)
    assert any(case.startswith(needle_case) for case, _ in result.failures), (
        f"mutation was not caught by any check starting with {needle_case!r};"
        f" failures were: {[c for c, _ in result.failures]}"
    )


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
    # A commented-out guard line (comment-blindness).
    for needle, case in [
        ("id-token: write", "2:"),
        ("persist-credentials: false", "4:"),
        ('echo "::add-mask::$oidc"', "5:"),
        ("attempts=5", "7:"),
        ("bash scripts/version-bump-gate.sh", "9:"),
        ("check_native_extras.py", "11:"),
    ]:
        _expect_failure(_comment_out_first(text, needle), case)


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
