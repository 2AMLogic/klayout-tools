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
import json
import os

from ..precheck import PrecheckError, run_precheck
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

    if os.path.isfile(value):
        try:
            with open(value, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise PrecheckError(
                f"could not read --allowed-layers file '{value}': {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise PrecheckError(
                f"--allowed-layers file '{value}' is not valid JSON: {exc}"
            ) from exc
    else:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PrecheckError(
                "--allowed-layers must be a path to a JSON file or an inline "
                f"JSON array of [layer, datatype] pairs: {exc}"
            ) from exc

    if not isinstance(data, list):
        raise PrecheckError("--allowed-layers must decode to a JSON array")

    layers: list[tuple[int, int]] = []
    for entry in data:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(v, int) for v in entry)
        ):
            raise PrecheckError(
                "--allowed-layers entries must each be a [layer, datatype] "
                f"pair of integers, got {entry!r}"
            )
        layers.append((entry[0], entry[1]))

    return layers


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
