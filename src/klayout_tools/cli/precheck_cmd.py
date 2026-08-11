"""``klt precheck`` command: run the layout-hygiene check battery and
serialise the report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/precheck.md`` for the full table):
    0 - every check passed or was skipped, none failed
    1 - failed to run (bad file, unknown deck, bad --grid-um/--allowed-layers)
        -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, at least one check failed
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse

from ..precheck import PrecheckError, run_precheck
from ._parsing import load_json_path_or_inline, parse_layer_pairs
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_FAIL = 3


def run(args: argparse.Namespace) -> int:
    try:
        allowed_layers = _load_allowed_layers(args.allowed_layers)
    except PrecheckError as exc:
        return emit_error("precheck", str(exc), args.format)

    try:
        report = run_precheck(
            args.file,
            grid_um=args.grid_um,
            allowed_layers=allowed_layers,
            deck=args.deck,
            top=args.top,
        )
    except PrecheckError as exc:
        return emit_error("precheck", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_FAIL if report["status"] == "fail" else EXIT_PASS


def _load_allowed_layers(value: str | None) -> list[tuple[int, int]] | None:
    """Resolve a ``--allowed-layers`` value into a list of ``(layer,
    datatype)`` pairs, or ``None`` when the flag was omitted (the
    ``layer_whitelist`` check is then skipped -- see ``precheck.py``).

    ``value`` is either a path to a JSON file or an inline JSON string, each
    holding a JSON array of two-element ``[layer, datatype]`` arrays, e.g.
    ``[[65, 20], [66, 20]]`` -- matching the ``--params``/``--allowed-layers``
    "path-or-inline-JSON" convention used elsewhere in this CLI (see
    ``klt gen``'s ``--params``).
    """
    if value is None:
        return None

    data = load_json_path_or_inline(
        value,
        "--allowed-layers",
        PrecheckError,
        array_kind="array of [layer, datatype] pairs",
    )
    return parse_layer_pairs(data, "--allowed-layers", PrecheckError)


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print()
    for check in report["checks"]:
        if check["status"] == "skipped":
            print(f"{check['name']}: skipped ({check['skip_reason']})")
        else:
            print(f"{check['name']}: {check['status']} ({check['violation_count']})")

    for check in report["checks"]:
        if not check["violations"]:
            continue
        print()
        print(f"{check['name']} violations:")
        for entry in check["violations"]:
            print(f"  {entry}")
