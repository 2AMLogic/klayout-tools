#!/usr/bin/env python3
"""Mechanical edits for an automated release (issue #4920).

``.github/workflows/release.yml`` calls this to do the text edits of the
release sequence in ``RELEASING.md`` (steps 1, 4 and the CHANGELOG half of
step 6); the workflow itself runs the generators (``uv lock``,
``gen_site_cli_data.py``, ``generate_deck_history.py``) afterwards.

Subcommands::

    release_prep.py next                      # print the next minor version
    release_prep.py prepare [--version X.Y.Z] [--date YYYY-MM-DD]
    release_prep.py detect --subject "<commit subject>"

``prepare`` edits, in place and idempotently (re-running it for the same
version changes nothing):

- ``pyproject.toml``: ``[tool.hatch.version] base-version``;
- ``CHANGELOG.md``: inserts ``## X.Y.Z (date)`` directly below
  ``## Unreleased`` (the unreleased entries become the release's entries and
  ``## Unreleased`` stays, empty, for the next cycle). A section with no
  entries has nothing to release; the run refuses (``PrepError``, before any
  file is written) rather than cut an empty release section (issue #6086);
- ``docs/cli/signoff.md``: "first release after `vPREV`" availability notes
  become "`klt X.Y.Z`" (PREV is the version being superseded). A placeholder
  anchored on a release older than PREV can never be resolved again; the prep
  run refuses (``PrepError``) naming the line rather than let it ship stale.

The version defaults to the next *minor* (``vX.(Y+1).0``): that satisfies
RELEASING.md's batch-runner rule (a contract change never ships in a patch)
by construction. A patch is a manual override via ``--version``.

``detect`` reads a commit subject and prints the release version when it is a
release-bump commit (``chore(release): bump version to X.Y.Z``, optionally
with the ``(#NNN)`` suffix a squash merge appends, or the merge-commit subject
``Merge pull request #N from <owner>/release/vX.Y.Z`` this repo's merge style
produces) -- the tag job's gate for
"tag this commit". It prints nothing and exits 0 otherwise.

Exit codes: 0 ok; 2 bad input (bad version, no ``## Unreleased`` heading, an
empty ``## Unreleased`` section, ...).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_VERSION = r"\d+\.\d+\.\d+"
_BASE_VERSION_RE = re.compile(rf'^(base-version\s*=\s*")({_VERSION})(")', re.MULTILINE)
_RELEASE_SUBJECT_RE = re.compile(
    rf"^chore\(release\): bump version to ({_VERSION})(?:\s|$)"
)
_MERGE_SUBJECT_RE = re.compile(
    rf"^Merge pull request #\d+ from \S+?/release/v({_VERSION})(?:\s|$)"
)
_UNRELEASED_RE = re.compile(r"^## Unreleased[ \t]*$", re.MULTILINE)
_NOTE_PLACEHOLDER_RE = re.compile(
    rf"(?:the )?(?:first|next) release after `?v?({_VERSION})`?"
)


class PrepError(Exception):
    """Input the script cannot act on (maps to exit code 2)."""


def parse_version(text: str) -> tuple[int, int, int]:
    if not re.fullmatch(_VERSION, text):
        raise PrepError(f"not an X.Y.Z version: {text!r}")
    major, minor, patch = (int(p) for p in text.split("."))
    return major, minor, patch


def next_minor(version: str) -> str:
    major, minor, _ = parse_version(version)
    return f"{major}.{minor + 1}.0"


def read_base_version(pyproject_text: str) -> str:
    match = _BASE_VERSION_RE.search(pyproject_text)
    if match is None:
        raise PrepError('no `base-version = "X.Y.Z"` line in pyproject.toml')
    return match.group(2)


def bump_pyproject(text: str, new: str) -> str:
    read_base_version(text)  # raises if absent
    return _BASE_VERSION_RE.sub(rf"\g<1>{new}\g<3>", text, count=1)


def _unreleased_entries(text: str) -> str | None:
    """Body of the ``## Unreleased`` section (to the next ``## `` or EOF).

    None when there is no ``## Unreleased`` heading at all.
    """
    match = _UNRELEASED_RE.search(text)
    if match is None:
        return None
    next_heading = re.search(r"^## ", text[match.end() :], re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(text)
    return text[match.end() : end]


def add_changelog_heading(text: str, new: str, date: str) -> str:
    """Insert ``## new (date)`` below ``## Unreleased``; no-op if present.

    The already-present early return runs first so an idempotent re-run never
    trips the empty-section gate (issue #6086). Inserting is only allowed when
    the ``## Unreleased`` section actually has entries: an empty section means
    there is nothing to release, and cutting anyway would ship an empty
    ``## X.Y.Z`` section and fail the release after the PyPI upload.
    """
    if re.search(rf"^## {re.escape(new)} \(", text, re.MULTILINE):
        return text
    entries = _unreleased_entries(text)
    if entries is None:
        raise PrepError("CHANGELOG.md has no '## Unreleased' heading")
    if not entries.strip():
        raise PrepError(
            "CHANGELOG.md '## Unreleased' section has no entries --"
            " nothing to release; refusing to insert an empty release section"
        )
    match = _UNRELEASED_RE.search(text)
    return f"{text[: match.end()]}\n\n## {new} ({date}){text[match.end() :]}"


def update_signoff_notes(text: str, previous: str, new: str) -> str:
    """Resolve "the first release after `vPREV`" notes to the release now cut."""

    def _resolve(match: re.Match[str]) -> str:
        return f"`klt {new}`" if match.group(1) == previous else match.group(0)

    return _NOTE_PLACEHOLDER_RE.sub(_resolve, text)


def check_signoff_note_anchors(text: str, previous: str) -> None:
    """Refuse availability-note placeholders the resolver can never match again.

    A placeholder is only resolvable for the release being bumped *from*
    (``previous``); once ``base-version`` moves past its anchor it is orphaned
    for good. Raise instead of letting it ship silently stale (issue #5582).
    """

    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _NOTE_PLACEHOLDER_RE.finditer(line):
            if parse_version(match.group(1)) < parse_version(previous):
                raise PrepError(
                    f"docs/cli/signoff.md:{lineno}: availability note anchored"
                    f" on v{match.group(1)}, older than the previous release"
                    f" {previous} -- the note resolver can never match it again;"
                    " resolve it by hand (e.g. '`klt X.Y.Z`')"
                )


def release_version_from_subject(subject: str) -> str | None:
    subject = subject.strip()
    match = _RELEASE_SUBJECT_RE.match(subject) or _MERGE_SUBJECT_RE.match(subject)
    return match.group(1) if match else None


def prepare(repo: Path, version: str | None, date: str) -> dict[str, str]:
    pyproject = repo / "pyproject.toml"
    changelog = repo / "CHANGELOG.md"
    signoff = repo / "docs" / "cli" / "signoff.md"

    py_text = pyproject.read_text(encoding="utf-8")
    current = read_base_version(py_text)
    new = version or next_minor(current)
    if parse_version(new) < parse_version(current):
        raise PrepError(
            f"version {new} is older than the current base-version {current}"
        )

    # Compute every write up front so a refusal (e.g. an empty '## Unreleased'
    # section, issue #6086) leaves the tree untouched, pyproject.toml unbumped.
    new_py_text = bump_pyproject(py_text, new)
    changelog_text = changelog.read_text(encoding="utf-8")
    new_log_text = add_changelog_heading(changelog_text, new, date)

    pyproject.write_text(new_py_text, encoding="utf-8")
    changelog.write_text(new_log_text, encoding="utf-8")
    # On an idempotent re-run `current == new`; the notes were already resolved.
    if signoff.exists() and current != new:
        signoff_text = update_signoff_notes(
            signoff.read_text(encoding="utf-8"), current, new
        )
        check_signoff_note_anchors(signoff_text, current)
        signoff.write_text(signoff_text, encoding="utf-8")
    return {"previous": current, "version": new, "date": date}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("next", help="print the next minor version")
    prep = sub.add_parser("prepare", help="apply the release text edits")
    prep.add_argument("--version", help="override (default: next minor)")
    prep.add_argument(
        "--date", default=_dt.datetime.now(_dt.timezone.utc).date().isoformat()
    )
    det = sub.add_parser(
        "detect", help="print the version if --subject is a release bump"
    )
    det.add_argument("--subject", required=True)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "next":
            current = read_base_version(
                (args.repo / "pyproject.toml").read_text(encoding="utf-8")
            )
            print(next_minor(current))
        elif args.cmd == "prepare":
            print(json.dumps(prepare(args.repo, args.version, args.date)))
        else:
            version = release_version_from_subject(args.subject)
            if version:
                print(version)
    except PrepError as exc:
        print(f"release_prep.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
