"""The cross-release ``provenance.input`` shape check behind
``klt signoff --attest --check``'s additive ``shape_mismatch`` field
(issue #5829).

Item 1 moved from a single ``provenance.input.path`` binding (0.8.0) to the
``paths`` input-set binding (0.9.x, #4764) under an unchanged envelope
``schema_version``, so ``--attest N --check`` across the two releases reports
a ``drifted`` a consumer cannot tell apart from real evidence drift: the
field pointers differ (``provenance.input.path`` vs ``provenance.input.paths``)
but nothing says "your tool version changed the envelope shape -- regenerate".

This module supplies the additive top-level ``shape_mismatch`` object that
says exactly that: it compares the **key presence** of ``provenance.input``
on both sides and, when the key sets differ, names them and points at
regeneration. It never changes ``status`` (a shape mismatch is still drift --
the committed envelope does not reproduce), never touches the ``drift`` list,
and is **absent** when the shapes agree, so a content-only drift reports no
field.

Split out of ``signoff_attest`` for the per-file size ratchet
(``scripts/check_file_size_baseline.py``), the same reason
``signoff_attest_drift`` is a separate module. It deliberately imports
nothing from ``signoff_attest`` -- it reads only the envelopes it is handed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The remediation the field states: a shape-mismatched committed envelope
#: cannot be converted (recomputing the combined set digest needs the input
#: files), it has to be regenerated and re-committed.
_REGENERATION_NOTE = (
    "the committed and freshly re-attested envelopes bind provenance.input "
    "with different key shapes -- a cross-release envelope-shape change "
    "(item 1 moved from the single 'path' binding to the 'paths' input-set "
    "binding in 0.9.0, #4764), not evidence content drift. Regenerate the "
    "envelope: re-run 'klt signoff --attest ITEM --inventory INVENTORY -o "
    "PATH' under the current klt and re-commit it."
)


def input_keys(envelope: Mapping[str, Any]) -> list[str] | None:
    """The sorted key names of ``envelope``'s ``provenance.input``, or
    ``None`` when the envelope carries no mapping there (a malformed or
    absent ``provenance`` block is diffed by the ordinary drift machinery,
    not named as a shape mismatch)."""
    provenance = envelope.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    binding = provenance.get("input")
    if not isinstance(binding, Mapping):
        return None
    return sorted(binding)


def shape_mismatch(
    committed: Mapping[str, Any], fresh: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The additive ``shape_mismatch`` response field, or ``None``.

    Present only when the committed and fresh envelopes bind
    ``provenance.input`` with **different key sets** -- the 0.8.0
    ``{path, content_hash}`` binding against the 0.9.x ``{paths,
    content_hash}`` one, in either direction. The value names both key sets
    (``keys_committed``/``keys_fresh``, the same sorted order
    :func:`input_keys` returns) and states the remediation, and carries the
    ``field`` it is about -- the same ``"provenance.input"`` spelling a
    ``drift[]`` entry's ``field`` uses for the block."""
    committed_keys = input_keys(committed)
    fresh_keys = input_keys(fresh)
    if committed_keys is None or fresh_keys is None or committed_keys == fresh_keys:
        return None
    return {
        "field": "provenance.input",
        "keys_committed": committed_keys,
        "keys_fresh": fresh_keys,
        "note": _REGENERATION_NOTE,
    }
