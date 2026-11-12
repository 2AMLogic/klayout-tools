"""Micrometre-box parsers for ``ties[]`` / ``nets[]`` spec entries (split out
of ``erc_spec.py`` to keep that file under its size ceiling, issue #5343).
"""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_number


def _parse_um_boxes(
    entry: dict[str, Any],
    key: str,
    spec_path: str,
    index: int,
    section: str = "ties",
) -> list[tuple[float, float, float, float]]:
    """One spec entry's ``[left, bottom, right, top]`` micrometre box list
    under ``key`` -- the shared parser behind ``ties[].tap_boxes`` (issue
    #2234, :func:`_parse_tap_boxes`), ``ties[].well_boxes`` (issue #2255,
    :func:`_parse_well_boxes`), ``ties[].well_requires_boxes`` /
    ``ties[].well_excludes_boxes`` (issue #2540, :func:`_parse_well_selection`)
    and ``nets[].unlabelled_allowed_boxes`` (issue #2524,
    :func:`_parse_unlabelled_allowed_boxes`), which take the identical
    literal-geometry shape and differ only in what the boxes *mean*.

    ``[left, bottom, right, top]`` is the same tuple
    :func:`~klayout_tools._layout.clip_box` already converts for ``klt
    clip``/``components.py``. Omitted/``null`` -> ``[]``. Every box is
    validated rather than coerced: a 3-element array, a non-number, a bool
    (JSON ``true`` is not a coordinate), or an inverted/degenerate extent is
    a spec error, since a silently-dropped box would weaken an assertion the
    caller believes they made.

    ``section`` names the spec array the entry came from, so the error
    message points at the caller's own key path (``ties[2].tap_boxes[0]`` vs.
    ``nets[1].unlabelled_allowed_boxes[0]``) rather than at whichever array
    happened to be the first user of this parser.
    """
    from .erc_spec import ErcError  # deferred: erc_spec imports this module

    raw = entry.get(key, [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(
            f"spec '{spec_path}': {section}[{index}].{key} must be an array of "
            "[left, bottom, right, top] micrometre boxes"
        )
    boxes: list[tuple[float, float, float, float]] = []
    for j, value in enumerate(raw):
        field = f"{section}[{index}].{key}[{j}]"
        if (
            not isinstance(value, list)
            or len(value) != 4
            or not all(is_number(v) for v in value)
        ):
            raise ErcError(
                f"spec '{spec_path}': {field} must be a 4-number "
                "[left, bottom, right, top] array"
            )
        left, bottom, right, top = (float(v) for v in value)
        if left >= right or bottom >= top:
            raise ErcError(
                f"spec '{spec_path}': {field} must have left < right and bottom < top"
            )
        boxes.append((left, bottom, right, top))
    return boxes


def _parse_tap_boxes(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[float, float, float, float]]:
    """``ties[].tap_boxes`` (optional, issue #2234): a caller *assertion* of
    where the tap geometry is, as a list of ``[left, bottom, right, top]``
    micrometre boxes intersected into ``tap_layer``. Composes with
    ``tap_requires`` (both narrow the same region) but, unlike it, needs no
    PDK marker layer to exist at all: a stream whose taps are genuinely
    drawn but carry no distinguishing implant/marker layer can point
    directly at them instead. Omitted/``null`` -> ``[]`` (no assertion,
    pre-#2234 behaviour, byte-identical). Split out of :func:`_validate_ties`
    to keep that function under the repo's C901 complexity ratchet, as
    :func:`_parse_tap_requires` is.
    """
    return _parse_um_boxes(entry, "tap_boxes", spec_path, index)


def _parse_well_boxes(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[float, float, float, float]]:
    """``ties[].well_boxes`` (optional, issue #2255): a caller *assertion*
    of the substrate/well region this tie is about, for a block that draws
    no well/tub layer for it to name -- the same ``[left, bottom, right,
    top]`` micrometre box list ``tap_boxes`` takes, and deliberately so, but
    a fundamentally different kind of claim.

    ``tap_boxes`` **narrows** a required, drawn ``tap_layer``: it composes
    with the geometry, and the geometry is still what is measured. This
    **substitutes** for a required drawn layer, so the region it names is
    not corroborated by anything in the stream at all. That is why it is
    valid only with ``well_layer: null`` (:func:`_parse_well_declaration`),
    why it is graded under its own coverage classification rather than
    folded into ``checked_by_assertion``, and why it carries its own
    degeneracy test (:func:`_degenerate_tie_reasons`).
    """
    return _parse_um_boxes(entry, "well_boxes", spec_path, index)


def _parse_isolation_boxes(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[float, float, float, float]]:
    """``ties[].isolation_boxes`` (optional, issue #5343): a caller *assertion*
    that a substrate tap site inside a box sits in that box's own isolated
    body (deep n-well / isolated p-well) rather than the shared substrate.
    Same ``[left, bottom, right, top]`` micrometre shape and validation as
    ``tap_boxes``/``well_boxes``; the claim is graded ``checked_by_assertion``
    because nothing in the stream corroborates it. Omitted/``null`` -> ``[]``.
    """
    return _parse_um_boxes(entry, "isolation_boxes", spec_path, index)
