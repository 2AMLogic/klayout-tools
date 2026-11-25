#!/usr/bin/env python3
"""Compute the version-bump file edits for ``release.yml`` (issue #5005).

``.github/workflows/release.yml``'s ``bump`` job calls this after the cadence
gate (``scripts/version-bump-gate.sh``) says *bump*. Nothing is committed or
pushed here: the job diffs the two files and POSTs them to the fleet
publisher, which commits exactly this shape to ``main`` (2AMLogic/2am
``infra/cloudflare/github-write/src/publish/``).

Subcommands::

    release_prep.py prepare [--version X.Y.Z] [--date YYYY-MM-DD]

``prepare`` rewrites, in place:

- ``pyproject.toml``: ``[tool.hatch.version]`` ``base-version`` -- exactly one
  patch step (``0.9.N`` -> ``0.9.N+1``) by default, or ``--version`` forwarded
  verbatim (a deliberate minor via the workflow's dispatch input; the
  publisher refuses any step it does not allow, so this script does not try to
  second-guess it);
- ``CHANGELOG.md``: inserts an empty line and ``## X.Y.Z (date)`` directly
  under the file's one ``## Unreleased`` line, leaving every other byte
  untouched. Whatever ``## Unreleased`` held becomes the new section's body;
  ``changelog.d/`` fragments are NOT folded in (they are rendered into the
  GitHub Release notes at publish time by ``scripts/release_notes.py``).

The CHANGELOG edit is byte-for-byte the shape the publisher's validator
(``version-sites.js`` ``changelogBump``) builds from the parent itself, so a
bump computed here is accepted there or refused loudly here first.

Exit codes: 0 ok; 2 bad input (bad version, no/ambiguous ``## Unreleased``
heading, the release heading already present, a version that does not
increase).
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
# Exactly the line the publisher's changelogBump matches: `## Unreleased`,
# optionally ended by a CR (a CRLF file), nothing else -- trailing spaces make
# the publisher refuse the bump, so they are refused here first.
_UNRELEASED_RE = re.compile(r"^## Unreleased(\r?)$", re.MULTILINE)
_DATED_RELEASE_RE = re.compile(rf"^## ({_VERSION}) \([^()\n]+\)[ \t]*$", re.MULTILINE)


class PrepError(Exception):
    """Input the script cannot act on (maps to exit code 2)."""


def parse_version(text: str) -> tuple[int, int, int]:
    if not re.fullmatch(_VERSION, text):
        raise PrepError(f"not an X.Y.Z version: {text!r}")
    major, minor, patch = (int(p) for p in text.split("."))
    return major, minor, patch


def next_patch(version: str) -> str:
    major, minor, patch = parse_version(version)
    return f"{major}.{minor}.{patch + 1}"


def read_base_version(pyproject_text: str) -> str:
    match = _BASE_VERSION_RE.search(pyproject_text)
    if match is None:
        raise PrepError('no `base-version = "X.Y.Z"` line in pyproject.toml')
    return match.group(2)


def bump_pyproject(text: str, new: str) -> str:
    read_base_version(text)  # raises if absent
    return _BASE_VERSION_RE.sub(rf"\g<1>{new}\g<3>", text, count=1)


def add_release_heading(text: str, version: str, date: str) -> str:
    """Insert the release heading under ``## Unreleased``, publisher-shaped.

    Mirrors ``changelogBump`` in 2am's ``version-sites.js``: the parent text
    with the one ``## Unreleased`` line followed by an empty line and the
    ``## version (date)`` line, every other byte unchanged. A CRLF file keeps
    CRLF. Refuses (before writing anything) when the heading is ambiguous or
    the release heading already exists.
    """
    if not re.fullmatch(_VERSION, version):
        raise PrepError(f"not an X.Y.Z version: {version!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise PrepError(f"not a YYYY-MM-DD date: {date!r}")
    headings = _UNRELEASED_RE.findall(text)
    if len(headings) != 1:
        raise PrepError(
            f"CHANGELOG.md has {len(headings)} '## Unreleased' headings, expected exactly 1"
        )
    if re.search(rf"^## {re.escape(version)} \([^\n]*\)[ \t]*$", text, re.MULTILINE):
        raise PrepError(
            f"CHANGELOG.md already has a '## {version}' heading; not bumping to it again"
        )
    crlf = "\r\n" in text
    nl = "\r\n" if crlf else "\n"
    cr = "\r" if crlf else ""
    return _UNRELEASED_RE.sub(
        f"## Unreleased{nl}{nl}## {version} ({date}){cr}", text, count=1
    )


def _read(path: Path) -> str:
    # newline="" so a CRLF file keeps its CRs: the publisher validates bytes,
    # and the translation a plain text-mode read does would silently rewrite
    # every line ending in CHANGELOG.md.
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def prepare(repo: Path, version: str | None, date: str) -> dict[str, str]:
    pyproject = repo / "pyproject.toml"
    changelog = repo / "CHANGELOG.md"

    py_text = _read(pyproject)
    current = read_base_version(py_text)
    new = version or next_patch(current)
    if parse_version(new) <= parse_version(current):
        raise PrepError(
            f"version {new} is not newer than the current base-version {current}"
        )

    new_py_text = bump_pyproject(py_text, new)
    new_log_text = add_release_heading(_read(changelog), new, date)

    _write(pyproject, new_py_text)
    _write(changelog, new_log_text)
    return {"previous": current, "version": new, "date": date}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    prep = parser.add_subparsers(dest="cmd", required=True)
    p = prep.add_parser("prepare", help="apply the version-bump file edits")
    p.add_argument(
        "--version",
        help="override (default: one patch step; a minor is forwarded and the"
        " publisher refuses it unless a reviewed 2am change lists it)",
    )
    p.add_argument(
        "--date", default=_dt.datetime.now(_dt.timezone.utc).date().isoformat()
    )
    args = parser.parse_args(argv)

    try:
        if args.cmd == "prepare":
            print(json.dumps(prepare(args.repo, args.version, args.date)))
    except PrepError as exc:
        print(f"release_prep.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
