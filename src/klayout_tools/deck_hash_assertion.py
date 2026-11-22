"""Shared fresh-run deck-hash assertion for ``klt drc``/``klt lvs`` (issue
#5806).

Every ``klt drc``/``klt lvs`` report already records which deck bytes it ran
under (``provenance.deck.content_hash``, built by
:mod:`klayout_tools._provenance`'s deck-block helper), but nothing let a
caller *assert* that hash on a fresh run -- so a toolchain pin bump that
changed deck bytes re-scored every locked expectation silently. This module
is the one compare both verbs share: ``cli/_deck_hash_parser_args.py`` adds
the flag (argparse ``type=`` validation), ``cli/drc_cmd.py``/
``cli/lvs_cmd.py`` call :func:`assert_deck_hash` on the produced report just
before emitting it.

Semantics, mirroring the shipped ``expect_rule_categories`` precedent
(issue #2697): fail closed. An exact match is silent and leaves the report
byte-identical (the assertion is deliberately *not* recorded in the report --
a match is not an event). A mismatch -- or an assertion against a run that
recorded no deck hash at all (``provenance.deck`` null, e.g. ``klt lvs`` on a
pre-extracted netlist) -- raises the verb's own error type, which the CLI
turns into the documented error envelope on exit 1.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

#: The one form ``provenance.deck.content_hash`` emits
#: (:func:`klayout_tools._digest.content_hash`): ``sha256:`` plus 64 lowercase
#: hex digits. Anything else is rejected rather than normalized, so the
#: expected value stays byte-comparable with the observed one.
EXPECTED_FORM = re.compile(r"sha256:[0-9a-f]{64}")

#: The error factory both CLI adapters pass: the verb's own error type
#: (``DrcError``/``LvsError``) on the run path, ``argparse.ArgumentTypeError``
#: for the flag's ``type=`` validation -- any callable taking one message.
ErrorFactory = Callable[[str], Exception]


def parse_expected(value: Any, error_cls: ErrorFactory) -> str | None:
    """Validate one expected deck-hash value, or ``None`` (no assertion).

    ``sha256:<64 lowercase hex>`` -- exactly what
    ``provenance.deck.content_hash`` emits -- passes through unchanged;
    anything else is rejected with a message naming the form, so a caller who
    pasted a bare digest or a hash from another algorithm is told what to
    supply instead of getting a guaranteed mismatch at compare time.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not EXPECTED_FORM.fullmatch(value):
        raise error_cls(
            "expected deck hash must be the 'sha256:<64 hex>' form "
            f"provenance.deck.content_hash emits (got {value!r}); take the "
            "value from an earlier report's provenance.deck.content_hash"
        )
    return value


def assert_deck_hash(
    report: dict[str, Any], expected: str | None, error_cls: ErrorFactory
) -> None:
    """Compare ``report``'s recorded deck hash against ``expected`` (issue
    #5806).

    ``expected is None`` (flag/field absent) is a no-op: without an
    assertion the report is untouched, so runs that do not opt in are
    byte-identical to before this module existed. An assertion against a run
    that recorded no deck hash never passes -- an expectation that cannot be
    checked must fail loudly, not silently, or a deck-less input shape would
    quietly satisfy every pin. A mismatch names the deck, both hashes, and
    where to identify the unexpected revision.
    """
    if expected is None:
        return
    deck = (report.get("provenance") or {}).get("deck") or {}
    observed = deck.get("content_hash")
    if not observed:
        raise error_cls(
            "--expect-deck-hash was given, but this run recorded no deck "
            "content hash to assert against (provenance.deck is absent or "
            "hashless -- e.g. `klt lvs` on a pre-extracted layout.netlist "
            "involves no deck); an expectation that cannot be checked "
            "never passes silently"
        )
    if observed != expected:
        raise error_cls(
            "deck-hash assertion failed: expected "
            f"{expected} but the run's provenance.deck.content_hash is "
            f"{observed} for deck '{deck.get('name', '<unknown>')}' -- the "
            "deck bytes changed under a locked expectation. Identify the "
            "revision with `klt deck resolve --content-hash <hash>` and "
            "re-review before updating the expected value"
        )
