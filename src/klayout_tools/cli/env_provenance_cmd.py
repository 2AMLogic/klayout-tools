"""``klt env-provenance`` commands: emit committable environment provenance,
and scan committed records for the identifiers it exists to keep out
(issue #1254).

- ``emit`` -- the reference environment-provenance block an evidence-record
  writer should embed: repo-relative paths only, a pseudonymous
  ``host-<8hex>`` id, no login. For a Python harness, importing
  :func:`klayout_tools.env_provenance.environment_provenance` directly is the
  same payload without a subprocess; this subcommand is for shell/`jq`
  harnesses and for eyeballing what would be recorded.
- ``scan`` -- report home-directory-shaped absolute paths in existing files
  (e.g. newly-added ``sim/**/records/*.md`` in a PR), so a regression is
  caught before it is committed rather than by a disclosure audit afterwards.

Both emit through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md``.

Exit codes:
    0 - emitted, or scanned with no leaks found
    1 - failed to run (a malformed ``--path``, an unreadable file, or a
        payload that would have leaked) -- ``output.ERROR_EXIT_CODE``
    3 - ``scan`` only: the scan ran fine and found leaks (a successful run
        with findings, mirroring ``klt drc``'s exit 3)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

from __future__ import annotations

import argparse

from ..env_provenance import (
    LEAKS_FOUND_EXIT_CODE,
    EnvironmentProvenanceError,
    environment_provenance,
    render_text_lines,
    scan_files,
)
from .output import emit_error, emit_success


def run_emit(args: argparse.Namespace) -> int:
    paths: dict[str, str] = {}
    for raw in args.paths or []:
        label, separator, value = raw.partition("=")
        if not separator or not label:
            return emit_error(
                "env-provenance emit",
                f"--path must be given as LABEL=PATH (got {raw!r})",
                args.format,
            )
        paths[label] = value

    try:
        report = environment_provenance(paths=paths)
    except EnvironmentProvenanceError as exc:
        return emit_error("env-provenance emit", str(exc), args.format)

    emit_success(report, args.format, _print_emit_text)
    return 0


def _print_emit_text(report: dict) -> None:
    for line in render_text_lines(report):
        print(line)


def run_scan(args: argparse.Namespace) -> int:
    try:
        report = scan_files(args.files)
    except EnvironmentProvenanceError as exc:
        return emit_error("env-provenance scan", str(exc), args.format)

    emit_success(report, args.format, _print_scan_text)
    return LEAKS_FOUND_EXIT_CODE if report["leak_count"] else 0


def _print_scan_text(report: dict) -> None:
    for entry in report["files"]:
        for leak in entry["leaks"]:
            print(f"{entry['file']}:{leak['line']}: {leak['kind']}: {leak['match']}")
    print(
        f"{report['status']}: {report['leak_count']} leak(s) in "
        f"{len(report['files'])} file(s)"
    )
