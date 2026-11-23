"""``klt drc --check`` / ``--check --rerun`` CLI handler (issue #1106).

The full-mode dispatch forwards the rerun invocation's own PDK selection
(``--pdk``/``--pdk-root``, issue #6986) so the fresh run resolves
``provenance.pdk`` instead of always drifting ``fresh: null`` against a
PDK-resolved committed report. Kept out of :mod:`.drc_cmd` so that module
stays under its size ceiling -- the same split as
:mod:`._drc_list_categories`.

The engine side -- hash checks, the rerun itself, replay of a committed
report's recorded choices -- is :mod:`klayout_tools.drc_report_check` and
:func:`klayout_tools.drc.rerun_drc_report`.
"""

from __future__ import annotations

import argparse

from .. import pdk as pdk_module
from ..drc import DrcError, check_drc_report, rerun_drc_report
from .output import (
    emit_error,
    emit_success,
    render_check_text,
    render_rerun_drift,
)

#: The ``--check``/``--rerun`` 0/3 outcome split -- same numeric values as
#: a normal run's clean/violations split, named for the ``match``/
#: ``drifted`` vocabulary these modes report under.
EXIT_MATCH = 0
EXIT_DRIFTED = 3


def run_check(args: argparse.Namespace) -> int:
    if args.output is not None:
        # Issue #4393: --check emits a verification result, not a run report.
        return emit_error(
            "drc",
            "-o/--output cannot be combined with --check: --check emits no "
            "run report to write",
            args.format,
        )
    if args.waivers is not None:
        # Issue #3483: like --rules, the waivers a report was run with are
        # recorded in its provenance and replayed by `--check --rerun`.
        return emit_error(
            "drc",
            "--waivers cannot be combined with --check: --check --rerun "
            "replays the committed report's own recorded waivers file",
            args.format,
        )
    if args.tile is not None:
        # Issue #4226: the abutment context a report was run with is
        # recorded in its `tile` echo and replayed by `--check --rerun`.
        return emit_error(
            "drc",
            "--tile cannot be combined with --check: --check --rerun replays "
            "the committed report's own recorded tile",
            args.format,
        )
    if args.expect_deck_hash is not None:
        # Issue #5806: a fresh-run assertion; --check re-verifies the
        # committed report's own recorded deck hash instead.
        return emit_error(
            "drc",
            "--expect-deck-hash cannot be combined with --check: --check "
            "verifies the committed report's own recorded deck hash",
            args.format,
        )
    if args.rules is not None:
        # Issue #2768: `--check` verifies a committed report as it was run;
        # `--rerun` replays that report's own recorded `rule_selection`, so a
        # second, argv-supplied selection would be ambiguous.
        return emit_error(
            "drc",
            "--rules cannot be combined with --check: --check --rerun replays "
            "the committed report's own recorded rule selection",
            args.format,
        )
    try:
        if args.rerun:
            # Issue #6986: forward the rerun invocation's own PDK selection
            # so the fresh run resolves `provenance.pdk` the way a fresh run
            # would, instead of always drifting `fresh: null` against a
            # PDK-resolved committed report. Cheap mode below stays
            # hash-only: it resolves no PDK at all, so the flags have no
            # meaning there and are neither consumed nor rejected.
            result = rerun_drc_report(
                args.check,
                input_path=args.file,
                deck_path=args.deck_file,
                pdk_variant=args.pdk,
                pdk_root=args.pdk_root,
            )
        else:
            result = check_drc_report(
                args.check, input_path=args.file, deck_path=args.deck_file
            )
    except DrcError as exc:
        return emit_error("drc", str(exc), args.format)
    except pdk_module.PdkNotFoundError as exc:
        return emit_error("drc", str(exc), args.format)

    text_renderer = render_rerun_drift if args.rerun else render_check_text
    emit_success(result, args.format, text_renderer)

    return EXIT_MATCH if result["status"] == "match" else EXIT_DRIFTED
