"""klt — CLI entry point.

Subcommands live in ``<verb>_cmd.py`` modules (mirroring kicad-tools' CLI
package). The argument parser is built in :mod:`parser`; this module dispatches
to the selected subcommand. Every subcommand supports ``--format json`` — JSON
is the API.
"""

import sys

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

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
