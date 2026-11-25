#!/usr/bin/env python3
"""Changelog-fragment shape gate (issue #7619).

`changelog.d/README.md` documents one format for a pending entry, and
`scripts/release_notes.py` renders each pending fragment verbatim into the
release's GitHub Release notes (issue #5005) -- it neither checks nor repairs
the shape. A fragment that is a bare paragraph, or a bullet with no issue
number, therefore lands in the published notes without the traceability every
other entry has. By publish time the author is long gone, so the check belongs
at PR time, as a CI gate.

A fragment passes when both hold:

* its first non-blank line starts with ``- `` (a Markdown bullet, the form
  `CHANGELOG.md` uses), and
* its text contains an issue reference matching ``#<digits>`` -- ``(#1234)``,
  ``(issue #1234)``, ``org/repo#1234`` all count. A date is *not* required:
  only 6 of the 1465 released bullets carry one, and the release heading
  already dates the section.

Not checked: that the reference equals the filename's issue number, that there
is a bold headline, or the shape of any bullet after the first.

Scope mirrors the fragment set `release_notes.py` renders: `*.md` files only,
skipping `README.md`, non-files and empty / whitespace-only files. The
standalone script is deliberate -- enforcing this inside `prepare` would hide
the defect in a release step and break its bare ``- first`` test fixtures.

Pure stdlib; reads only the fragment directory.

Usage::

    python scripts/check_changelog_fragments.py            # check changelog.d/
    python scripts/check_changelog_fragments.py --dir DIR  # check another directory

Exit codes: 0 clean, 1 at least one offending fragment (each is named on
stderr with the reason), 2 the directory could not be read.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = REPO_ROOT / "changelog.d"

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_ERROR = 2

ISSUE_REF_RE = re.compile(r"#\d+")
BULLET_PREFIX = "- "
_FILENAME_ISSUE_RE = re.compile(r"^(\d+)-")


def fragment_paths(directory: Path) -> list[Path]:
    """Pending fragments in filename order, the order notes render them in."""
    return [
        path
        for path in sorted(directory.glob("*.md"))
        if path.name != "README.md" and path.is_file()
    ]


def check_text(text: str, filename: str = "") -> list[str]:
    """Reasons ``text`` is not a valid fragment; empty for a valid one.

    A whitespace-only fragment is skipped at render time, so it is valid.
    """
    if not text.strip():
        return []
    reasons: list[str] = []
    first = next(line for line in text.splitlines() if line.strip())
    if not first.startswith(BULLET_PREFIX):
        reasons.append(
            f'first non-blank line must start with "{BULLET_PREFIX}" (a Markdown '
            f"bullet, like CHANGELOG.md entries); found {first.strip()[:50]!r}"
        )
    if ISSUE_REF_RE.search(text) is None:
        match = _FILENAME_ISSUE_RE.match(filename)
        hint = f", e.g. (#{match.group(1)})" if match else ""
        reasons.append(f"no issue reference: add the tracking issue as #<number>{hint}")
    return reasons


def check_directory(directory: Path) -> list[tuple[Path, list[str]]]:
    """Every offending fragment under ``directory`` with its reasons."""
    offenders: list[tuple[Path, list[str]]] = []
    for path in fragment_paths(directory):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            offenders.append(
                (path, [f"not valid UTF-8 ({exc.reason} at byte {exc.start})"])
            )
            continue
        reasons = check_text(text, path.name)
        if reasons:
            offenders.append((path, reasons))
    return offenders


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_DIR,
        help="fragment directory to check (default: the repo's changelog.d/)",
    )
    args = parser.parse_args(argv)
    if not args.dir.is_dir():
        print(f"error: fragment directory not found: {args.dir}", file=sys.stderr)
        return EXIT_ERROR
    offenders = check_directory(args.dir)
    for path, reasons in offenders:
        for reason in reasons:
            print(f"{_display(path)}: {reason}", file=sys.stderr)
    if offenders:
        print(
            f"{len(offenders)} changelog fragment(s) do not follow the format in "
            "changelog.d/README.md",
            file=sys.stderr,
        )
        return EXIT_VIOLATIONS
    print(f"ok: {len(fragment_paths(args.dir))} changelog fragment(s) checked")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
