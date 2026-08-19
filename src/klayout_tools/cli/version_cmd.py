"""``klt version``: the running build's identity (issue #1202).

``klt --version`` prints the same one-line string this command's ``--format
text`` prints; ``--format json`` adds the machine-readable payload a consumer
gating on "am I on a release?" needs (``{version, package_version, git_commit,
git_tag, dirty, is_release}``). The payload itself is built by
:func:`klayout_tools.build_identity.version_report` so it travels with the
library, not the CLI layer (see ``docs/json-contract.md``, "Adding a new
command").

Always exits 0: identifying the running build cannot fail -- an unrecoverable
identity is reported as ``+unknown`` with ``is_release: null``, never as an
error.
"""

from __future__ import annotations

import argparse

from ..build_identity import version_report
from .output import emit_success


def run(args: argparse.Namespace) -> int:
    emit_success(version_report(), args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"klt {report['version']}")
