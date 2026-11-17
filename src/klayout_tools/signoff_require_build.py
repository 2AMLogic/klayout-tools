"""``klt signoff --manifest/--fleet --require-grading-ruleset sha256:<hex>``
(issue #5847).

A verdict-of-record render could not be told which grading build it must
be: the report's ``build.grading_ruleset_id`` is an output only, and exit
3 from the wrong build is indistinguishable from exit 3 from the intended
one -- while ``is_release``/``grading_ruleset_id`` and even per-item
reasons move between builds. This flag lets the caller assert the grading
identity up front: on a mismatch (or a build that cannot compute an id at
all) the command refuses with exit ``1``, an error envelope on stderr,
nothing on stdout, and no manifest read or evidence command run.

The id, not a version string, is the pin: ``grading_ruleset_id`` is a
content hash of the shipped ``signoff.py`` (issue #2216), identical
across provisioning routes of one grading build, where ``klt_version``
differs. Both ``klt signoff --describe-grader`` and
``klt version --format json`` print it.

Split out of :mod:`.cli.signoff_cmd` and ``.cli.parser`` for the
file-size ratchet, like :mod:`.signoff_attest_build` before it.
"""

from __future__ import annotations

import re
from typing import Any

from .build_identity import grading_ruleset_id

#: The only accepted shape: ``sha256:`` + 64 hex digits. Case-insensitive
#: throughout -- prefix and hex are lowercased before comparing against
#: :func:`grading_ruleset_id`'s lowercase form, so a caller copying the id
#: through anything that changes case is not punished for it.
_REQUIRE_RULESET_RE = re.compile(r"^sha256:([0-9a-f]{64})$", re.IGNORECASE)


def add_require_grading_ruleset_argument(parser: Any) -> None:
    """Register ``--require-grading-ruleset`` on the ``klt signoff`` parser."""
    parser.add_argument(
        "--require-grading-ruleset",
        metavar="sha256:HEX",
        help=(
            "with --manifest/--fleet (render or --check; issue #5847): "
            "refuse (exit 1, error envelope on stderr, nothing on stdout, "
            "no evidence command run) unless this build's "
            "grading_ruleset_id equals the given id -- checked before the "
            "manifest is read. Print the id with --describe-grader or "
            "`klt version --format json`. Refused with --attest, "
            "--describe-grader and plain envelope aggregation. See "
            "docs/cli/signoff.md's 'Pinning the grading build' section."
        ),
    )


def require_grading_ruleset_problem(
    required: str, manifest_source: str | None, fleet_source: str | None
) -> str | None:
    """The refusal message for ``--require-grading-ruleset``, or ``None``
    to proceed exactly as if the flag had not been given.

    Mode is judged first: the flag only applies to a ``--manifest``/
    ``--fleet`` render or ``--check`` (envelope aggregation, ``--attest``
    and ``--describe-grader`` produce no grade to pin), and that refusal
    is the answer whatever the value's shape. Only then the value: a
    malformed id is refused naming the expected shape; a build that
    cannot compute its own id refuses rather than fabricate one
    (:func:`grading_ruleset_id` returns ``None`` only then); and a
    differing id is refused with both ids in the message.
    """
    if not (manifest_source or fleet_source):
        return (
            "--require-grading-ruleset only applies to --manifest/--fleet "
            "(render or --check); envelope aggregation, --attest and "
            "--describe-grader produce no grade to pin"
        )
    match = _REQUIRE_RULESET_RE.match(required)
    if not match:
        return (
            "--require-grading-ruleset expects 'sha256:' followed by 64 "
            "hex digits (the build's grading_ruleset_id, as printed by "
            "--describe-grader or 'klt version --format json'), got "
            f"{required!r}"
        )
    running = grading_ruleset_id()
    if running is None:
        return (
            "--require-grading-ruleset cannot be satisfied: this build "
            "cannot compute its own grading_ruleset_id (the shipped "
            "grading module is missing or unreadable)"
        )
    required_id = "sha256:" + match.group(1).lower()
    if required_id != running:
        return (
            f"--require-grading-ruleset: this build grades by {running}, "
            f"not the required {required_id} -- refusing before reading "
            "the manifest or running any evidence command"
        )
    return None
