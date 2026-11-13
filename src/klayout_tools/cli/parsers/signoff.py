"""``klt signoff`` argument parser (split out of ``parser.py``, issue #5527).

Parser registration lives here; ``run()`` stays in
:mod:`klayout_tools.cli.signoff_cmd`. Shared helpers are passed in as
callables, following :mod:`klayout_tools.cli.parsers.extract` and
``parser_netlist``.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from ...signoff import describe_opt_in_kinds_text
from ...signoff_attest_carry import add_skip_local_only_argument
from .. import signoff_cmd
from ..color import add_color_args


def add_signoff_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``signoff`` verb for aggregating check results.

    Aggregate klt drc/lvs/extract/sim results into a pass/fail verdict.
    See docs/cli/signoff.md for full details.
    """
    signoff_parser = subparsers.add_parser(
        "signoff",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Opt-in evidence kinds (non-native envelopes) and the T1 "
            "checklist items each may satisfy; any other item rejects them "
            "as wrong_kind. Also available as JSON via --describe-grader:\n"
            + describe_opt_in_kinds_text()
        ),
        help=(
            "aggregate klt drc/lvs/extract/sim JSON envelopes into one "
            "pass/fail verdict, render a T1-T4 tier-verdict report with "
            "--manifest, or a fleet-wide tier roll-up with --fleet"
        ),
        description=(
            "Read one or more `klt` JSON envelope files (or '-' for stdin) "
            "produced by `klt drc`/`klt lvs`/`klt extract`/`klt sim` and "
            "combine them into a single signoff verdict: pass only if every "
            "check's own status passed AND every input's `provenance` block "
            "(issue #251) agrees on PDK/deck/input-layout identity -- a "
            "mismatched-provenance input set is refused (status: "
            "'refused'), never silently aggregated into a wrong verdict. "
            "With --manifest instead, renders the T1-T4 evidence-tier item "
            "skeleton mechanically parsed from docs/design-evidence-tiers.md, "
            "graded against a block manifest's declared kind and per-item "
            "evidence locations -- an item is 'met' only when it cites a "
            "passing klt JSON envelope with fresh provenance; a missing or "
            "stale check renders 'unmet', never assumed met. With --fleet "
            "instead, grades every block named in a fleet manifest and "
            "reports each block's current tier and, for any block not yet "
            "T1, the single item still blocking it -- one query across the "
            "whole fleet instead of opening each block's own report. "
            "See docs/cli/signoff.md."
        ),
    )
    signoff_parser.add_argument(
        "files",
        nargs="*",
        default=[],
        help=(
            "path(s) to klt drc/lvs/extract/sim JSON envelope files, or "
            "'-' to read one from stdin (mutually exclusive with "
            "--manifest/--fleet)"
        ),
    )
    signoff_parser.add_argument(
        "--manifest",
        metavar="FILE",
        help=(
            "path to a block manifest JSON file (or '-' for stdin) -- "
            "renders the T1-T4 tier-verdict report instead of aggregating "
            "envelope <file> arguments (mutually exclusive with them and "
            "with --fleet). See docs/cli/signoff.md's 'Tier-verdict report' "
            "section for the manifest shape."
        ),
    )
    signoff_parser.add_argument(
        "--fleet",
        metavar="FILE",
        help=(
            "path to a fleet manifest JSON file (or '-' for stdin) -- "
            "grades every block it names (each a block manifest, inline or "
            "by path) and renders a fleet-wide tier roll-up instead of a "
            "single block's report (mutually exclusive with the envelope "
            "<file> arguments and with --manifest). See "
            "docs/cli/signoff.md's 'Fleet roll-up' section for the fleet "
            "manifest shape."
        ),
    )
    signoff_parser.add_argument(
        "--repo-root",
        metavar="DIR",
        help=(
            'directory that `scope: "repo"` input paths resolve against, '
            "instead of the nearest `.git` ancestor of the evidence file -- "
            "for grading evidence copied outside any git checkout (a source "
            "archive, a vendored copy, an unpacked CI artifact). Must be an "
            "existing directory. Only meaningful with --manifest/--fleet. "
            "See docs/cli/signoff.md's 'Grading outside a git checkout' "
            "section."
        ),
    )
    signoff_parser.add_argument(
        "--allow-generator-exec",
        action="store_true",
        help=(
            "opt in (default off) to running an operator-approved T1 item "
            "1/2 regeneration generator on the batch runner and "
            "byte-comparing its output with the committed artifact. Needs "
            "--generator-approvals (or $KLT_SIGNOFF_GENERATOR_APPROVALS): "
            "a manifest can never approve its own generator. Trusted-code "
            "policy, not isolation. Only meaningful with --manifest/--fleet. "
            "See docs/cli/signoff.md's 'Executing approved generators' "
            "section."
        ),
    )
    signoff_parser.add_argument(
        "--generator-approvals",
        metavar="FILE",
        help=(
            "trusted approvals JSON binding each runnable generator's "
            "interpreter, script content hash, allowed arguments and "
            "artifact -- operator configuration outside the manifest. Only "
            "valid with --allow-generator-exec."
        ),
    )
    signoff_parser.add_argument(
        "--repin",
        action="store_true",
        help=(
            "rewrite --manifest's stale evidence content_hash pins in place "
            "from the evidence they name, instead of rendering a "
            "tier-verdict report (issue #5527) -- the mechanical half of "
            "the regenerate-then-re-pin step after an attestation inventory "
            "changes. Only a pin whose live hash this grader re-derived "
            "from the artifact itself (citation input_verified: true) is "
            "rewritten; a stale pin the grader could not re-verify is left "
            "untouched and reported as skipped, and an entry that pins "
            "nothing is never given a pin. Requires --manifest (not '-'); "
            "mutually exclusive with <file>/--fleet/--attest/--check/"
            "--describe-grader. See docs/cli/signoff.md's "
            "'Re-pinning a stale hash' section."
        ),
    )
    signoff_parser.add_argument(
        "--tiers-doc",
        metavar="PATH",
        help=(
            "path to the design-evidence-tiers Markdown doc to parse the "
            "T1-T4 item skeleton from, instead of the copy this install "
            "ships (or $KLT_TIERS_DOC, which this overrides) -- only "
            "meaningful with --manifest/--fleet. See docs/cli/signoff.md's "
            "'Where the tier doc comes from' section."
        ),
    )
    signoff_parser.add_argument(
        "--check",
        metavar="REPORT",
        help=(
            "path to a previously committed --manifest/--fleet report JSON "
            "file -- re-grades the manifest and reports whether that report "
            "still reproduces (status: 'match'/'drifted') instead of "
            "rendering a fresh one. Exits 0 if it still holds, 3 if drifted, "
            "1 if the committed report is missing/unparseable or was "
            "produced by the other mode. The `build` block is excluded from "
            "the comparison: it names how this install was provisioned, not "
            "only which commit it came from, so two byte-legitimate installs "
            "of the same pinned commit report different ones (issue #2249). "
            "Use this rather than byte-comparing the committed file against "
            "a fresh render. Only meaningful with --manifest/--fleet; see "
            "docs/cli/signoff.md's '--check' section. With --attest, REPORT "
            "is a committed attestation envelope instead: re-attests and "
            "exits 0 only if it still reproduces and still passes, 3 "
            "otherwise -- never rewriting it."
        ),
    )
    signoff_parser.add_argument(
        "--attest",
        metavar="ITEM",
        type=int,
        choices=[1, 2, 8, 9, 10],
        help=(
            "produce the artifact-anchored generic envelope for T1 item 1 "
            "(Design sources), 8 (Characterization report), 9 (Testbenches "
            "shipped) or 10 (Repo hygiene) from --inventory, by running the "
            "item-specific checks the inventory declares (issue #2844); or "
            "for item 2 (Layout) from --input STREAM, by checking the "
            "committed layout stream (readable, non-empty top cell, dbu on "
            "grid; issue #4188). Status is derived from those checks: exit "
            "0 pass, 3 fail (nothing written), 1 malformed inventory. "
            "Mutually exclusive with <file>/--manifest/--fleet/"
            "--describe-grader/--tiers-doc. See docs/cli/signoff.md."
        ),
    )
    signoff_parser.add_argument(
        "--inventory",
        metavar="FILE",
        help=(
            "with --attest: the versioned attestation inventory (kind "
            "'t1-attestation-inventory') in a git repo; the envelope pins its "
            "sha256. Optional for --attest 2: declared layout 'checks'."
        ),
    )
    add_skip_local_only_argument(signoff_parser)
    signoff_parser.add_argument(
        "--input",
        dest="attest_input",
        metavar="STREAM",
        action="append",
        help=(
            "with --attest 2: committed GDS/OASIS stream in a git repo (issue "
            "#4188); repeat for a per-cell library, bound as a set (issue #4935). "
            "Refused with items 1, 8, 9 and 10, which take --inventory."
        ),
    )
    signoff_parser.add_argument(
        "--generator",
        metavar="CMD",
        help=(
            "with --attest 2: the command that regenerates the stream, "
            "recorded as provenance.regeneration.generator. Echoed, never "
            "run; requires --regenerated-hash."
        ),
    )
    signoff_parser.add_argument(
        "--regenerated-hash",
        metavar="sha256:HEX",
        help=(
            "with --attest 2: the hash your own CI got after re-running the "
            "generator, recorded as provenance.regeneration.regenerated_hash; "
            "the grader flags the item stale_artifact if it differs from the "
            "committed stream. Requires --generator."
        ),
    )
    signoff_parser.add_argument(
        "--hash-kind",
        choices=["bytes", "geometry"],
        help=(
            "with --attest 2 --regenerated-hash: what the regenerated hash "
            "digests -- raw bytes (default) or the decoded geometry, which "
            "ignores GDS/OASIS write timestamps."
        ),
    )
    signoff_parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help=(
            "with --attest: also write the envelope to FILE when it passes, "
            "only if its bytes changed (an unchanged attestation leaves the "
            "committed file untouched)."
        ),
    )
    signoff_parser.add_argument(
        "--describe-grader",
        action="store_true",
        help=(
            "print which T1 checklist item ids this build has grading "
            "rules for, plus the content hash identifying the grading code "
            "itself, without reading source or running any check -- "
            "mutually exclusive with <file>/--manifest/--fleet/--tiers-doc/"
            "--attest "
            "(this always reports the build's own shipped grading rules, "
            "never an overridden doc's item list). See docs/cli/signoff.md's "
            "'Identifying the grading build' section."
        ),
    )
    _add_format_arg(signoff_parser)
    # Issue #2227: `signoff` is the one verb that colours its `--format text`
    # rendering, and the rendering it colours is also a *committed* artifact.
    # `--color`/`--no-color` (plus $NO_COLOR and the isatty default) live in
    # `.color` so a second colouring verb registers the same policy with one
    # call rather than re-deriving it.
    add_color_args(signoff_parser)
    signoff_parser.set_defaults(func=signoff_cmd.run)
