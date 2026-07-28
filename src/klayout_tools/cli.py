"""klt — CLI entry point.

Scaffold: subcommands land per ROADMAP.md phases (layers, cells, stats,
drc, ...). Every subcommand must support --format json; JSON is the API.
"""

import argparse
import json
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="klt",
        description="Tools for AI agents to work with IC layout.",
    )
    parser.add_argument("--version", action="version", version=f"klt {__version__}")
    parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="output format"
    )
    args = parser.parse_args(argv)

    status = {
        "name": "klayout-tools",
        "version": __version__,
        "status": "scaffold",
        "roadmap": "https://github.com/2AMLogic/klayout-tools/blob/main/ROADMAP.md",
    }
    if args.format == "json":
        json.dump(status, sys.stdout, indent=2)
        print()
    else:
        print(f"klt {__version__} — scaffold; see ROADMAP.md for what lands next")
    return 0


if __name__ == "__main__":
    sys.exit(main())
