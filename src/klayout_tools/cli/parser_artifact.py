"""``klt artifact`` argument parser, split out of :mod:`parser` (per-file size
ratchet, issue #5464) so the ``verify --local`` wiring (issue #5591) could land
without growing :mod:`parser` past its ceiling -- the same move
:mod:`parser_pex` made for ``klt pex``. Parser-wide helpers are callables the
caller passes in, so this module never imports :mod:`parser` back.

The verb's logic lives in :mod:`klayout_tools.artifact_store`,
:mod:`klayout_tools.artifact_verify_local` and :mod:`.artifact_cmd`; see
docs/cli/artifact.md.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from .. import artifact_store
from . import artifact_cmd


def add_artifact_parser(
    subparsers: argparse._SubParsersAction,
    _no_subcommand_handler: Callable[..., Any],
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``artifact`` verb with nested ``put``/``get``/``verify``/``delete``
    subcommands (issues #4063, #4101): the client for the fleet artifact store's
    broker. See docs/cli/artifact.md.
    """
    artifact_parser = subparsers.add_parser(
        "artifact",
        help="put/get/verify/delete evidence bytes in the fleet artifact store by "
        "sha256, with a committed manifest",
        description=(
            "Evidence bytes go to the artifact store through its broker; git "
            "keeps a manifest naming each object by sha256. The broker URL "
            f"comes from --broker or ${artifact_store.ENV_BROKER}; there is "
            "no default. No credential is held: the broker identifies the "
            "caller by its network identity. Presigned URLs stay in-process "
            "and are never printed."
        ),
    )
    artifact_sub = artifact_parser.add_subparsers(
        dest="artifact_command", metavar="<subcommand>"
    )
    artifact_parser.set_defaults(func=_no_subcommand_handler(artifact_parser))

    put_parser = artifact_sub.add_parser(
        "put",
        help="store files and emit (or merge) manifest entries",
        description=(
            "Hash each file, ask the broker for a presigned PUT, upload with "
            "exactly the headers it returned, and emit one manifest entry per "
            "file. An object already stored is not re-sent and still succeeds."
        ),
    )
    put_parser.add_argument("files", nargs="+", metavar="FILE", help="file(s) to store")
    put_parser.add_argument(
        "--manifest",
        metavar="PATH",
        default=None,
        help="merge the entries into this manifest JSON (created if absent)",
    )
    put_parser.add_argument(
        "--kind", choices=list(artifact_store.CLIENT_KINDS), default="evidence"
    )
    put_parser.add_argument(
        "--name",
        default=None,
        help="entry name (single file only; default: the path relative to "
        "its git checkout)",
    )
    put_parser.add_argument("--media-type", default=None, help="entry media_type")
    put_parser.add_argument(
        "--producer-tool", default=None, help="producer.tool (default: klt)"
    )
    put_parser.add_argument(
        "--producer-version",
        default=None,
        help="producer.version (default: this klt's version)",
    )
    put_parser.add_argument(
        "--commit",
        default=None,
        help="producer.commit (default: git HEAD of the current directory)",
    )
    _add_artifact_common_args(put_parser, repo=True, _add_format_arg=_add_format_arg)
    put_parser.set_defaults(func=artifact_cmd.run_put)

    get_parser = artifact_sub.add_parser(
        "get",
        help="fetch one object, writing it only if its sha256 verifies",
        description=(
            "REF is a sha256 (stored under --repo) or MANIFEST[#SHA256|NAME]. "
            "The bytes are written only when their recomputed sha256 matches "
            "both the manifest's digest and the key's. `-o -` writes the "
            "verified bytes to stdout instead of a JSON envelope."
        ),
    )
    get_parser.add_argument("ref", metavar="REF", help="sha256 or MANIFEST[#SELECTOR]")
    get_parser.add_argument(
        "-o", "--output", required=True, help="destination file, or - for stdout"
    )
    get_parser.add_argument(
        "--force", action="store_true", help="replace an existing output file"
    )
    _add_artifact_common_args(get_parser, repo=True, _add_format_arg=_add_format_arg)
    get_parser.set_defaults(func=artifact_cmd.run_get)

    verify_parser = artifact_sub.add_parser(
        "verify",
        help="check every manifest entry's size and sha256 against the "
        "store, or re-hash local files in place with --local",
        description=(
            "Against the broker: stat each entry, then fetch and re-hash "
            "it. With --local no broker is contacted at all: each entry "
            "whose name resolves to a file under --root is re-hashed in "
            "place and its sha256 and size compared to the manifest, so a "
            "committed summary can be checked in a clean checkout with no "
            "credential. Exits 3 if any checked entry is missing or "
            "mismatched, 1 on a broker refusal, a bad manifest, a local "
            "run in which no entry resolved, or a local entry name that "
            "escapes the verify root."
        ),
    )
    verify_parser.add_argument(
        "manifest",
        metavar="MANIFEST",
        help="manifest to check; with --local, MANIFEST[#SELECTOR] checks "
        "one entry (SELECTOR is an entry's sha256 or name)",
    )
    verify_parser.add_argument(
        "--local",
        action="store_true",
        help="verify against files under --root instead of the store: no "
        "broker, no network, no credential",
    )
    verify_parser.add_argument(
        "--root",
        metavar="DIR",
        default=None,
        help="directory entry names resolve against (with --local; "
        "default: the manifest's git checkout root, else the current "
        "directory)",
    )
    _add_artifact_common_args(
        verify_parser, repo=False, _add_format_arg=_add_format_arg
    )
    verify_parser.set_defaults(func=artifact_cmd.run_verify)

    delete_parser = artifact_sub.add_parser(
        "delete",
        help="delete stored objects through the broker (dry run unless --yes)",
        description=(
            "REF is a sha256 (under --repo) or MANIFEST[#SELECTOR], as for "
            "`get`. Without --yes only `stat` is called and nothing is "
            "deleted. Each --keep MANIFEST protects the objects it names: a "
            "referenced target refuses the whole batch unless "
            "--force-referenced. An absent object is success. The broker "
            "decides whether the caller may delete."
        ),
    )
    delete_parser.add_argument(
        "refs", nargs="+", metavar="REF", help="sha256 or MANIFEST[#SELECTOR]"
    )
    delete_parser.add_argument(
        "--keep",
        metavar="MANIFEST",
        action="append",
        default=[],
        help="manifest whose objects must not be deleted (repeatable)",
    )
    delete_parser.add_argument(
        "--yes", action="store_true", help="actually delete (default: dry run)"
    )
    delete_parser.add_argument(
        "--force-referenced",
        action="store_true",
        help="delete even objects a --keep manifest names",
    )
    _add_artifact_common_args(delete_parser, repo=True, _add_format_arg=_add_format_arg)
    delete_parser.set_defaults(func=artifact_cmd.run_delete)


def _add_artifact_common_args(
    parser: argparse.ArgumentParser,
    *,
    repo: bool,
    _add_format_arg: Callable[..., Any],
) -> None:
    parser.add_argument(
        "--broker",
        metavar="URL",
        default=None,
        help=f"artifact broker base URL (default: ${artifact_store.ENV_BROKER})",
    )
    if repo:
        parser.add_argument(
            "--repo",
            metavar="OWNER/NAME",
            default=None,
            help=f"repo the objects are stored under (default: "
            f"${artifact_store.ENV_REPO}, else the git origin remote)",
        )
    _add_format_arg(parser)
