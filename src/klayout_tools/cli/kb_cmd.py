"""``klt kb`` command: query the circuit-design knowledge base (``kb/``).

Four subcommands, all emitting through the shared envelope helpers in
:mod:`.output`, as with every other ``klt`` subcommand — see
``docs/json-contract.md``.

Exit codes (see ``docs/cli/kb.md`` for the full table):
    0 - success (``list``/``show``/``search``: found; ``validate``: every
        entry is valid)
    1 - failed to run (missing ``kb/`` directory, unreadable entry/schema
        file, unknown entry id for ``show``) — returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - ``validate`` ran successfully but found one or more invalid entries
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

import argparse

from ..kb import KbError, list_entries, search_entries, show_entry, validate_entries
from .output import emit_error, emit_success, render_table

EXIT_VALID = 0
EXIT_INVALID = 3


def run_list(args: argparse.Namespace) -> int:
    try:
        report = list_entries()
    except KbError as exc:
        return emit_error("kb list", str(exc), args.format)

    emit_success(report, args.format, _print_summary_text)
    return 0


def run_show(args: argparse.Namespace) -> int:
    try:
        report = show_entry(args.id)
    except KbError as exc:
        return emit_error("kb show", str(exc), args.format)

    emit_success(report, args.format, _print_show_text)
    return 0


def run_search(args: argparse.Namespace) -> int:
    try:
        report = search_entries(args.query)
    except KbError as exc:
        return emit_error("kb search", str(exc), args.format)

    emit_success(report, args.format, _print_summary_text)
    return 0


def run_validate(args: argparse.Namespace) -> int:
    try:
        report = validate_entries()
    except KbError as exc:
        return emit_error("kb validate", str(exc), args.format)

    emit_success(report, args.format, _print_validate_text)
    return EXIT_VALID if report["valid"] else EXIT_INVALID


def _print_summary_text(report: dict) -> None:
    entries = report["entries"]
    if "query" in report:
        print(f"query: {report['query']}")
    print(f"count: {report['count']}")
    if not entries:
        return

    rows = [
        (entry["id"] or "-", entry["title"] or "-", entry["spec_class"] or "-")
        for entry in entries
    ]
    headers = ("id", "title", "spec_class")
    render_table(headers, rows, left_aligned={0, 1, 2})


def _print_show_text(report: dict) -> None:
    entry = report["entry"]
    print(f"id: {entry.get('id')}")
    print(f"title: {entry.get('title')}")
    print(f"topology: {entry.get('topology')}")
    print(f"spec_class: {entry.get('spec_class')}")

    pdk_portability = entry.get("pdk_portability")
    if pdk_portability:
        print(f"pdk_portability.primary_pdk: {pdk_portability.get('primary_pdk')}")
        print(f"pdk_portability.notes: {pdk_portability.get('notes')}")

    sizing_approach = entry.get("sizing_approach")
    if sizing_approach:
        print(f"sizing_approach: {sizing_approach}")

    layout_idioms = entry.get("layout_idioms")
    if layout_idioms:
        print(f"layout_idioms: {', '.join(layout_idioms)}")

    source = entry.get("source") or {}
    print(f"source.citation: {source.get('citation')}")
    if source.get("url"):
        print(f"source.url: {source['url']}")
    if source.get("license_or_openness"):
        print(f"source.license_or_openness: {source['license_or_openness']}")

    notes = entry.get("notes")
    if notes:
        print(f"notes: {notes}")


def _print_validate_text(report: dict) -> None:
    print(f"entry_count: {report['entry_count']}")
    print(f"valid: {'yes' if report['valid'] else 'no'}")

    invalid = [entry for entry in report["entries"] if not entry["valid"]]
    if not invalid:
        return

    print()
    for entry in invalid:
        print(f"{entry['id']}:")
        for error in entry["errors"]:
            print(f"  - {error}")
