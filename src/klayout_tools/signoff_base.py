"""``klt signoff``'s shared envelope/grading vocabulary, as a leaf module.

Holds what every ``signoff_*`` sibling needs from the orchestrator before
the orchestrator itself can be imported: the command's schema-version
constants, the error contract (:class:`SignoffError`,
:class:`EnvelopeVersionSkewError`), the JSON envelope reader
(:func:`_read_json_source`/:func:`_read_envelope`) and its strict-number
guards, the regeneration-hash vocabulary, the command-evidence timeout, and
the pure grading helpers (:func:`input_set_digest`,
:func:`_citation_label`).

This follows the ``<verb>_base`` leaf pattern (``lvs_base.py``,
``functional_verification_base.py``): importing only stdlib plus the
shared core (``._jsongz``), never anything ``signoff*``, so sibling
modules import these names at module scope while :mod:`.signoff` --
which imports *them* at its own top level -- re-exports every name to
keep ``klayout_tools.signoff.<name>`` resolving exactly as it did before
the split (issue #7371). The shared ``_REASON_*`` unmet-reason strings
moved to :mod:`.signoff_unmet_reasons` in the same split, keeping this
module to vocabulary that is not a reason string.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from collections.abc import Iterable
from typing import Any

from ._jsongz import READ_ERRORS, open_text

#: T1 item ids whose **only** accepted evidence is an artifact-anchored
#: ``"generic"`` envelope (issue #2844) -- items 1 (Design sources), 9
#: (Testbenches shipped) and 10 (Repo hygiene). Before that issue a passing
#: native envelope of any recognised kind satisfied them, topical relevance
#: notwithstanding (a clean `klt drc` report "proved" repo hygiene). Now that
#: ``klt signoff --attest`` (:mod:`.signoff_attest`) produces a mechanically
#: derived envelope for exactly these three, :func:`_allowed_kinds_for`
#: restricts them to ``{"generic"}``, so a native citation renders
#: ``"unmet"``/:data:`_REASON_WRONG_KIND`.
#:
#: Item 2 (Layout) is deliberately absent: it is outside issue #2844's scope
#: and keeps accepting any passing native envelope as before.
#:
#: Kept separate from :data:`_ITEM_ALLOWED_KINDS` on purpose: that table also
#: decides :func:`_is_structurally_ungradeable_item` and
#: :func:`_is_graded_by_build`, and moving these ids into it would silently
#: re-rank the fleet roll-up's ``blocking_item`` (issue #2178) -- a separate
#: behaviour change this tightening does not make.
_ITEMS_REQUIRING_ATTESTED_EVIDENCE: frozenset[int] = frozenset({1, 9, 10})

#: T1 item 8 (Characterization report, issue #4311): attestable from an
#: inventory but deliberately *not* in ``_ITEMS_REQUIRING_ATTESTED_EVIDENCE``,
#: so a bare hand-written generic envelope still grades.
CHARACTERIZATION_ITEM = 8

#: The T1 items this producer attests from an inventory -- the items whose
#: evidence :data:`~klayout_tools.signoff._ITEMS_REQUIRING_ATTESTED_EVIDENCE`
#: restricts to a generic envelope, plus item 8. Item 2 (Layout) stays out of
#: scope: its binding is the committed stream itself (issue #2718), unchanged
#: by issue #2844.
ATTESTABLE_ITEMS: tuple[int, ...] = tuple(
    sorted(_ITEMS_REQUIRING_ATTESTED_EVIDENCE | {CHARACTERIZATION_ITEM})
)

#: The one T1 item attested from a committed layout stream rather than an
#: inventory (issue #4188): ``--attest 2 --input STREAM``. Item 2's binding is
#: the stream itself, so there is no inventory to hand-write.
STREAM_ATTESTABLE_ITEM = 2

#: The primary input expected by each generic attestation producer.
ATTESTATION_INPUT_FLAGS = {
    **dict.fromkeys(ATTESTABLE_ITEMS, "--inventory"),
    STREAM_ATTESTABLE_ITEM: "--input",
}

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md. This is *this* command's own
#: envelope version, independent of the schema_version of any envelope it
#: aggregates.
SCHEMA_VERSION = 1

#: Schema version for :func:`build_tier_report`'s own JSON shape -- distinct
#: from :data:`SCHEMA_VERSION` (the envelope-aggregation mode's shape)
#: because the two modes' top-level fields are unrelated; bumping one must
#: never imply the other changed.
#:
#: Still ``1`` after issue #2176 (``build``, ``items[].graded_by_build``, the
#: ``ungradeable_by_build`` reason), issue #2202
#: (``build_t1_item_count``), and issue #2216 (``build.grading_ruleset_id``),
#: deliberately: every one of those fields is a *new* key, and a new
#: ``reason`` value is a value set growing within an unchanged shape -- both
#: explicitly additive under ``docs/json-contract.md`` ("adding new fields
#: does not" bump; "an additive change can introduce a new enum-like
#: value"). Issue #2202's field has no behaviour change behind it at all: it
#: reports *beside* ``t1_item_count``, and never reinterprets it. Issue
#: #2216's field is the same shape: a new key inside the already-additive
#: ``build`` block, reporting *beside* its existing fields rather than
#: redefining any of them. The grading change that rode with #2176 refuses a
#: citation this build has no rules for, which is a *correction* of a
#: verdict that was wrong, not a redefinition of what ``status``/``reason``
#: mean -- the same shape as issue #1987's (item 3 stopped accepting `klt
#: extract` citations) and issue #2044's (items 5/6/8 gained kind
#: restrictions) grading corrections, neither of which bumped this constant.
#: Contrast :data:`FLEET_REPORT_SCHEMA_VERSION` below, which bumped for issue #2178
#: because an already-shipped field's *meaning* changed there.
TIER_REPORT_SCHEMA_VERSION = 1

#: Schema version for :func:`build_fleet_report`'s own JSON shape -- distinct
#: from both :data:`SCHEMA_VERSION` and :data:`TIER_REPORT_SCHEMA_VERSION`
#: for the same reason: the three modes' top-level fields are unrelated, so
#: bumping one must never imply either of the others changed.
#:
#: ``2`` (issue #2178): ``blocks[].blocking_item`` no longer means "the first
#: unmet T1 item in render order". It now skips the structurally-ungradeable
#: items 1, 2, 9 and 10 whenever a gradeable T1 item is also unmet (see
#: :func:`_blocking_t1_item`), so the same fleet manifest that reported
#: ``blocking_item.id == 1`` under version ``1`` reports a different item
#: under ``2``. Adding the new ``blocks[].ungraded_items`` field alongside it
#: would have been purely additive on its own, but redefining what an
#: already-shipped field's unchanged type *means* is a breaking change under
#: ``docs/json-contract.md``'s "value sets within an unchanged shape" rule
#: (the `klt precheck` ``layer_whitelist[].shapes`` precedent, issue #452),
#: so this bumps rather than landing silently.
#:
#: ``3`` (issue #2203): ``blocks[].blocking_item`` moves again, for exactly
#: the same reason and so under exactly the same rule. An unmet item this
#: build has no grading rules for (``graded_by_build: False``, issue #2176)
#: is now *preferred* as the blocker over every other unmet item, where
#: version ``2`` demoted it below any gradeable one -- it satisfied
#: :func:`_is_structurally_ungradeable_item` incidentally, so #2178's skip
#: swept it up. A fleet manifest graded against a ``--tiers-doc`` newer than
#: the running build therefore reports a different ``blocking_item.id``
#: under ``3`` than under ``2`` (see :func:`_blocking_t1_item` for the rule
#: and the rationale). That case is reachable in practice -- it is the case
#: issue #2176 exists for -- so this is a real observable move, not a
#: speculative bump. Nothing else changes shape: ``ungraded_items`` lists
#: exactly the same rows it did under ``2``, and no field is added, removed
#: or retyped.
FLEET_REPORT_SCHEMA_VERSION = 3

#: Schema version for :func:`describe_grader`'s own JSON shape (issue
#: #2216) -- distinct from the other three for the same reason they are
#: distinct from each other: this mode's top-level fields are unrelated to
#: envelope-aggregation, tier-report, and fleet-roll-up mode, so bumping one
#: must never imply another changed.
DESCRIBE_GRADER_SCHEMA_VERSION = 1


#: The only ``regenerated_hash`` shape accepted: the ``sha256:<64 lowercase
#: hex>`` form every `klt` re-hash produces (``_provenance.sha256_file``),
#: so a placeholder or differently-encoded digest is refused as malformed
#: instead of reading as a stale artifact.
_REGENERATED_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: ``provenance.regeneration.hash_kind`` values: raw bytes (default) or the
#: decoded-geometry digest, which ignores GDS/OASIS write timestamps.
_HASH_KIND_BYTES = "bytes"
_HASH_KIND_GEOMETRY = "geometry"


#: Wall-clock cap on a command-backed evidence entry's subprocess (issue
#: #825) -- a hung gate must not hang `klt signoff`. Generous (corner-matrix
#: sims are slow) but finite; a timeout renders the item
#: "unmet"/:data:`_REASON_COMMAND_TIMEOUT` (#5763), never an aborting
#: exception.
_COMMAND_EVIDENCE_TIMEOUT_S = 1800


class SignoffError(Exception):
    """Raised when an envelope cannot even be read/parsed, or does not match
    any recognized ``klt`` envelope shape.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other ``klt`` verb's error contract.
    """


class AttestationError(SignoffError):
    """A malformed inventory or committed envelope -- nothing was attested.

    A :class:`~klayout_tools.signoff.SignoffError` subclass so the CLI's
    existing ``except SignoffError`` reports it through the ordinary error
    envelope (exit ``1``)."""

    # Mixed-version workers must still resolve this exception at its public path.
    __module__ = "klayout_tools.signoff_attest"


class EnvelopeVersionSkewError(SignoffError):
    """Raised by :func:`_classify` for an envelope that carries a kind's
    **primary** marker but none of the shape discriminators that marker is
    paired with (issue #2198) -- the shape a `klt` response written before
    the discriminator field existed has.

    A :class:`SignoffError` subclass on purpose: every existing caller keeps
    treating it exactly as it treated the unrecognised-shape raise (the CLI
    still exits 1 with a clean message), while a caller that *can* act on
    the distinction -- :func:`_resolve_evidence`, which renders
    :data:`_REASON_ENVELOPE_VERSION_SKEW` rather than
    :data:`_REASON_UNRECOGNIZED_ENVELOPE` -- catches it first.

    ``kind``/``primary_field``/``missing_fields`` name the near-miss, so the
    message can say which verb's response this almost is and which field it
    lacks -- something the unrecognised-shape raise structurally cannot say
    about a genuinely foreign document.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        primary_field: str,
        missing_fields: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.primary_field = primary_field
        self.missing_fields = missing_fields


# --------------------------------------------------------------------------- #
# Reading + classifying input envelopes
# --------------------------------------------------------------------------- #


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"nonstandard numeric constant {token!r} is not permitted")


def _finite_json_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"numeric literal {token!r} is not finite")
    return value


def _read_json_source(source: str, description: str) -> Any:
    """Read and JSON-decode one JSON source: ``source == "-"`` reads stdin, otherwise
    ``source`` is a file path (gzip-compressed or not -- see ``_jsongz.open_text``,
    issue #5452). Raises :class:`SignoffError` on any read/parse failure, including
    nonstandard numeric constants and literals that overflow to infinity -- never
    lets invalid JSON evidence become a passing verdict or leak nonfinite values into
    output. ``description`` (e.g. ``"envelope"``, ``"manifest"``) only affects
    error-message wording, so each caller's failures still read naturally.

    Deliberately mirrors ``report.py``'s ``_read_envelope`` (same
    read/parse/error-message contract) rather than importing it: the two commands'
    input-reading needs are identical but incidental, not a shared abstraction worth
    coupling two independently-versioned CLI verbs over. Shared *within* this module
    by :func:`_read_envelope` and :func:`_read_fleet_block_manifest` (issue #827),
    which belong to the same command and would otherwise triplicate this logic.
    """
    if source == "-":
        try:
            return json.load(
                sys.stdin,
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
            )
        except ValueError as exc:
            raise SignoffError(f"stdin {description} is not valid JSON: {exc}") from exc

    if not os.path.exists(source):
        raise SignoffError(f"file not found: {source}")
    if os.path.isdir(source):
        raise SignoffError(f"not a file: {source}")

    try:
        with open_text(source) as handle:
            return json.load(
                handle,
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
            )
    except READ_ERRORS as exc:
        raise SignoffError(
            f"could not read {description} file '{source}': {exc}"
        ) from exc
    except ValueError as exc:
        raise SignoffError(
            f"{description} file '{source}' is not valid JSON: {exc}"
        ) from exc


def _read_envelope(source: str) -> Any:
    """Read and JSON-decode one ``klt`` envelope -- see
    :func:`_read_json_source`."""
    return _read_json_source(source, "envelope")


def input_set_digest(members: Iterable[tuple[str, str]]) -> str:
    """The combined ``provenance.input.content_hash`` for a set binding (issue
    #3044): ``sha256:`` over the lines ``"<hex>  <path>\\n"`` -- ``<hex>`` the
    member's hash without its ``sha256:`` prefix, ``<path>`` the member path
    as the envelope names it -- sorted bytewise by path, so member order does
    not matter. Equivalent to ``sha256sum`` over the members in ``LC_ALL=C``
    sorted order, piped into ``sha256sum``."""
    ordered = sorted(members, key=lambda pair: pair[0].encode("utf-8"))
    body = "".join(
        f"{content_hash.removeprefix('sha256:')}  {path}\n"
        for path, content_hash in ordered
    )
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def _citation_label(spec: dict[str, Any]) -> str:
    if spec["kind"] == "command":
        return " ".join(spec["command"])
    return str(spec["file"])
