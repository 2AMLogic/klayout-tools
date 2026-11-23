"""klt — CLI entry point.

Subcommands live in ``<verb>_cmd.py`` modules (mirroring kicad-tools' CLI
package). The argument parser is built in :mod:`parser`; this module dispatches
to the selected subcommand. Every subcommand supports ``--format json`` — JSON
is the API.
"""

import sys

from .. import _warn
from ..build_identity import build_version
from .parser import create_parser


def main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    # No subcommand: preserve the scaffold status blurb (and exit 0).
    if getattr(args, "func", None) is None:
        print(
            f"klt {build_version()} — scaffold; run `klt --help` for available commands"
        )
        return 0

    # Usage checks a required mutually exclusive group cannot express (see
    # `parser._require_one_input`): exit 2 via the subparser, before dispatch.
    post_parse = getattr(args, "post_parse", None)
    if post_parse is not None:
        post_parse(args)

    # Issue #6935: under --format json, stderr is reserved for the error
    # envelope, so klt's own human-readable warnings are deferred for the
    # invocation and dropped/resumed at the shared output boundary
    # (`cli.output.emit_error`/`emit_success`) once the outcome is known.
    # Text invocations (and verbs without --format) never arm the deferral:
    # their warnings print as they happen, byte-identical to before.
    if getattr(args, "format", None) != "json":
        return args.func(args)
    _warn.arm()
    try:
        return args.func(args)
    finally:
        # A command that emitted through neither boundary still shows its
        # warnings -- no envelope was written for them to pollute.
        _warn.flush()


if __name__ == "__main__":
    sys.exit(main())
