#!/usr/bin/env python3
"""Mechanical edits for an automated release (issue #4920).

``.github/workflows/release.yml`` calls this to do the text edits of the
release sequence in ``RELEASING.md`` (steps 1, 4 and the CHANGELOG half of
step 6); the workflow itself runs the generators (``uv lock``,
``gen_site_cli_data.py``, ``generate_deck_history.py``) afterwards.

Subcommands::

    release_prep.py next                      # print the next minor version
    release_prep.py prepare [--version X.Y.Z] [--date YYYY-MM-DD]
    release_prep.py refresh --version X.Y.Z [--check]
    release_prep.py detect --subject "<commit subject>"

``prepare`` edits, in place and idempotently (re-running it for the same
version changes nothing):

- ``pyproject.toml``: ``[tool.hatch.version] base-version``;
- ``CHANGELOG.md``: inserts ``## X.Y.Z (date)`` directly below
  ``## Unreleased``. Existing entries and fragments from ``changelog.d/``
  become the release's entries, with fragments appended in filename order
  and removed after assembly. ``## Unreleased`` stays empty for the next
  cycle. With no entries from either source, the run refuses (``PrepError``,
  before any file is written) to cut an empty release (issue #6086);
- ``docs/cli/signoff.md``, each pending ``changelog.d/`` fragment, and the
  ``## Unreleased`` body of ``CHANGELOG.md``: "first release after `vPREV`"
  availability notes become "`klt X.Y.Z`" (PREV is the version being
  superseded). A placeholder anchored on a release older than PREV can never
  be resolved again; the prep run refuses (``PrepError``) naming the source
  file and its physical line rather than let it ship stale. Historical
  ``CHANGELOG.md`` sections are left untouched.

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


def _unreleased_span(text: str) -> tuple[int, int]:
    """Offsets of the Unreleased body in the original changelog."""
    match = _UNRELEASED_RE.search(text)
    if match is None:
        raise PrepError("CHANGELOG.md has no '## Unreleased' heading")
    start = match.end()
    next_heading = re.compile(r"^## ", re.MULTILINE).search(text, start)
    return start, next_heading.start() if next_heading else len(text)


def _has_release_heading(text: str, version: str) -> bool:
    return re.search(rf"^## {re.escape(version)} \(", text, re.MULTILINE) is not None


def _read_changelog_fragments(directory: Path) -> dict[Path, str]:
    """Read pending entries in filename order, keyed by the files to remove."""
    fragments: dict[Path, str] = {}
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md" or not path.is_file():
            continue
        entry = path.read_text(encoding="utf-8")
        if entry.strip():
            fragments[path] = entry
    return fragments


def add_changelog_heading(
    text: str, new: str, date: str, *, fragments: str = "", previous: str | None = None
) -> str:
    """Insert ``## new (date)`` below ``## Unreleased``; no-op if present.

    The already-present early return runs first so an idempotent re-run never
    trips the empty-section gate (issue #6086). The new section must contain
    entries from ``## Unreleased`` or from fragments.
    """
    if _has_release_heading(text, new):
        return text
    heading_end, section_end = _unreleased_span(text)
    entries = text[heading_end:section_end]
    if not entries.strip() and not fragments:
        raise PrepError(
            "CHANGELOG.md '## Unreleased' section has no entries --"
            " nothing to release; refusing to insert an empty release section"
        )
    remaining = text[section_end:]
    if previous is not None and previous != new:
        entries = update_signoff_notes(entries, previous, new)
        check_signoff_note_anchors(
            entries,
            previous,
            source="CHANGELOG.md",
            start_line=text.count("\n", 0, heading_end) + 1,
        )
    if fragments:
        legacy = entries.strip("\n")
        body = f"{legacy}\n\n{fragments}" if legacy.strip() else fragments
        entries = f"\n\n{body}\n" + ("\n" if remaining else "")
    return f"{text[:heading_end]}\n\n## {new} ({date}){entries}{remaining}"


def update_signoff_notes(text: str, previous: str, new: str) -> str:
    """Resolve "the first release after `vPREV`" notes to the release now cut."""

    def _resolve(match: re.Match[str]) -> str:
        return f"`klt {new}`" if match.group(1) == previous else match.group(0)

    return _NOTE_PLACEHOLDER_RE.sub(_resolve, text)


def _resolve_notes(
    text: str, previous: str, new: str, source: str, start_line: int = 1
) -> str:
    """Check anchors at their original lines before resolving this release."""
    check_signoff_note_anchors(text, previous, source=source, start_line=start_line)
    return update_signoff_notes(text, previous, new)


def check_signoff_note_anchors(
    text: str,
    previous: str,
    *,
    source: str = "docs/cli/signoff.md",
    start_line: int = 1,
) -> None:
    """Refuse availability-note placeholders the resolver can never match again.

    A placeholder is only resolvable for the release being bumped *from*
    (``previous``); once ``base-version`` moves past its anchor it is orphaned
    for good. Raise instead of letting it ship silently stale (issue #5582).
    """

    for lineno, line in enumerate(text.splitlines(), start=start_line):
        for match in _NOTE_PLACEHOLDER_RE.finditer(line):
            if parse_version(match.group(1)) < parse_version(previous):
                raise PrepError(
                    f"{source}:{lineno}: availability note anchored"
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
    # section, issue #6086, or a stale availability-note anchor, issue #5582)
    # leaves the tree untouched, pyproject.toml unbumped and fragments in place.
    new_py_text = bump_pyproject(py_text, new)
    changelog_text = changelog.read_text(encoding="utf-8")
    # An existing release heading means pending fragments are for a future cut.
    fragments = (
        {}
        if _has_release_heading(changelog_text, new)
        else _read_changelog_fragments(repo / "changelog.d")
    )
    new_log_text = add_changelog_heading(
        changelog_text,
        new,
        date,
        previous=current,
        fragments="\n\n".join(
            _resolve_notes(entry, current, new, f"changelog.d/{path.name}").strip("\n")
            for path, entry in fragments.items()
        ),
    )
    # On an idempotent re-run `current == new`; the notes were already resolved.
    new_signoff_text = None
    if signoff.exists() and current != new:
        new_signoff_text = update_signoff_notes(
            signoff.read_text(encoding="utf-8"), current, new
        )
        check_signoff_note_anchors(new_signoff_text, current)

    pyproject.write_text(new_py_text, encoding="utf-8")
    changelog.write_text(new_log_text, encoding="utf-8")
    for path in fragments:
        path.unlink()
    if new_signoff_text is not None:
        signoff.write_text(new_signoff_text, encoding="utf-8")
    return {"previous": current, "version": new, "date": date}


_DATED_RELEASE = re.compile(rf"^## ({_VERSION}) \([^()\n]+\)[ \t]*$", re.MULTILINE)


def _refresh_changelog(
    text: str, version: str, fragments: dict[Path, str]
) -> tuple[str, str]:
    """Collect late release inputs between Unreleased and the previous release."""
    heading_end, section_end = _unreleased_span(text)
    entries = text[heading_end:section_end]
    current = _DATED_RELEASE.match(text, section_end)
    if current is None or current[1] != version:
        raise PrepError(
            f"expected the prepared ## {version} heading immediately after Unreleased"
        )
    older = _DATED_RELEASE.search(text, current.end())
    if older is None or parse_version(older[1]) >= parse_version(version):
        raise PrepError(f"cannot determine the previous release below ## {version}")
    previous = older[1]

    body = _resolve_notes(
        text[current.end() : older.start()],
        previous,
        version,
        "CHANGELOG.md",
        text.count("\n", 0, current.end()) + 1,
    )
    additions = [
        _resolve_notes(
            entries,
            previous,
            version,
            "CHANGELOG.md",
            text.count("\n", 0, heading_end) + 1,
        )
    ]
    additions.extend(
        _resolve_notes(entry, previous, version, f"changelog.d/{path.name}")
        for path, entry in fragments.items()
    )
    for entry in additions:
        entry = entry.strip("\n")
        # A rerun after the changelog write may still see a consumed fragment.
        existing = body.strip("\n")
        if entry.strip() and f"\n\n{entry}\n\n" not in f"\n\n{existing}\n\n":
            body = body.rstrip("\n") + "\n\n" + entry + "\n\n"
    before = text[:heading_end] + ("\n\n" if entries.strip() else entries)
    return before + current[0] + body + text[older.start() :], previous


def refresh(repo: Path, version: str, *, check: bool = False) -> bool:
    """Refresh an already prepared release. Return whether any input was pending."""
    current = read_base_version((repo / "pyproject.toml").read_text(encoding="utf-8"))
    if version != current:
        raise PrepError(
            f"release branch version {version} disagrees with base-version {current}"
        )
    changelog = repo / "CHANGELOG.md"
    original = changelog.read_text(encoding="utf-8")
    fragments = _read_changelog_fragments(repo / "changelog.d")
    refreshed, previous = _refresh_changelog(original, version, fragments)
    edits = {changelog: refreshed} if refreshed != original else {}
    signoff = repo / "docs/cli/signoff.md"
    if signoff.is_file():
        text = signoff.read_text(encoding="utf-8")
        check_signoff_note_anchors(text, previous)
        updated = update_signoff_notes(text, previous, version)
        if updated != text:
            edits[signoff] = updated
    if not check:
        for path, content in edits.items():
            path.write_text(content, encoding="utf-8")
        for path in fragments:
            path.unlink()
    return bool(edits or fragments)


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
    ref = sub.add_parser("refresh", help="collect late notes into a prepared release")
    ref.add_argument("--version", required=True, help="version of the release branch")
    ref.add_argument(
        "--check",
        action="store_true",
        help="exit 1 for pending updates without writing",
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
        elif args.cmd == "refresh":
            changed = refresh(args.repo, args.version, check=args.check)
            if args.check and changed:
                print(
                    "Pending release notes. Run python3 scripts/release_prep.py "
                    f"refresh --version {args.version} and commit the result.",
                    file=sys.stderr,
                )
                return 1
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
