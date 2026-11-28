"""Shared per-kind measurement primitives for the ``klt sim`` sidecar forms.

Each ``measurements[]`` form module (:mod:`klayout_tools.sim_crossings`,
:mod:`klayout_tools.sim_energy`, ...) declares one form and carries the same
small primitives around it: a one-key "is this entry declared with my form"
test, the is-it-an-object / unknown-key prelude its body validation opens
with, the single-ended ``"v(node)"`` selector check the rawfile-indexed forms
share, the rawfile path its ``.control`` block writes, the first-declared-order
key dedup its deck lines and rawfiles are numbered by, the filter-and-index
loop that reads those rawfiles back per corner, the ``analysis.kind`` gate
its context validation opens with, and (for the forms whose responses index
by name) the duplicate-measurement-name guard. Those are identical modulo
one string -- the form key, the rawfile prefix, the diagnostic noun -- so
they live here instead of once per form module. The forms differ in what
they *compute* from a measurement, not in how entries are declared, numbered,
or read back.

Every parameter (``key=``, ``prefix=``, ``form=``) names the **caller's own
form**, so a path or diagnostic reads exactly as the per-kind module wrote
it -- the same convention :mod:`klayout_tools.sim_window` established.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, TypeVar

from . import sim_raw
from .sim_errors import SimError
from .sim_forms import normalize_selector

OutcomeT = TypeVar("OutcomeT")
KeyT = TypeVar("KeyT")


def declares(spec: dict[str, Any], key: str) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``key``."""
    return spec.get(key) is not None


def validate_form_object(
    name: Any,
    body: Any,
    *,
    form: str,
    allowed_keys: frozenset[str],
    example: str,
) -> None:
    """Refuse a ``spec[form]`` body that is not an object, or that carries
    keys outside ``allowed_keys`` -- the prelude every per-kind body
    validation opens with.

    ``form`` is the caller's own form key (``"energy"``, ``"crossings"``,
    ...), quoted by the first diagnostic and named bare by the second;
    ``example`` is the form's own minimal body literal, shown verbatim, and
    the allowed keys are listed sorted, so both messages read exactly as the
    per-kind module wrote them. Which keys are *required* stays the form's
    own check.
    """
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': '{form}' must be an object, e.g. {example}"
        )
    unknown = sorted(set(body) - allowed_keys)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown {form} key(s) {unknown}; "
            f"allowed: {sorted(allowed_keys)}"
        )


def require_voltage_selectors(
    name: Any, body: dict[str, Any], keys: tuple[str, ...], *, form: str
) -> None:
    """Refuse any ``body[key]`` of ``keys`` that is not a single-ended
    ``"v(node)"`` selector -- one node token, no whitespace, comma or
    parenthesis -- as :func:`klayout_tools.sim_forms.normalize_selector`
    accepts it. ``keys`` is checked in order, so the first offending key is
    the one the diagnostic names; ``form`` is the caller's own form key."""
    for key in keys:
        if normalize_selector(body.get(key)) is None:
            raise SimError(
                f"measurement '{name}': {form}.{key} must be a single-ended "
                '"v(node)" selector with one node token (no whitespace, '
                f"comma or parenthesis); got {body.get(key)!r}"
            )


def rawfile_path(corner_dir: str, prefix: str, index: int) -> str:
    """Where measurement number ``index`` of the ``prefix`` kind writes its
    ASCII rawfile -- the on-disk name its ``.control`` ``write`` line and
    its ``extract_corner_*`` read-back must agree on."""
    return os.path.join(corner_dir, f"{prefix}_{index}.raw")


def extract_indexed(
    measurements_spec: list[dict[str, Any]],
    corner_dir: str,
    *,
    prefix: str,
    predicate: Callable[[dict[str, Any]], bool],
    extractor: Callable[[dict[str, Any], str], OutcomeT],
) -> dict[int, OutcomeT]:
    """``{position in measurements_spec: outcome}`` for every entry matching
    ``predicate`` of one finished corner. Matching entries are numbered in
    declaration order -- the order the kind's ``.control`` block wrote its
    rawfiles -- and ``extractor(spec, raw_path)`` reduces each."""
    outcomes: dict[int, OutcomeT] = {}
    index = 0
    for position, spec in enumerate(measurements_spec):
        if not predicate(spec):
            continue
        outcomes[position] = extractor(spec, rawfile_path(corner_dir, prefix, index))
        index += 1
    return outcomes


def unique_keys(
    specs: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], KeyT]
) -> list[KeyT]:
    """``key_fn(spec)`` for every distinct value over ``specs``, in
    first-declared order -- the order a form's deck lines and rawfiles are
    numbered in, so two entries that differ only in what they *compute* share
    one rawfile."""
    keys: list[KeyT] = []
    for spec in specs:
        key = key_fn(spec)
        if key not in keys:
            keys.append(key)
    return keys


def require_analysis_kind(
    declared: list[dict[str, Any]],
    analysis: dict[str, Any],
    *,
    form: str,
    required: str,
    explanation: str,
) -> None:
    """Refuse the ``form`` entries in ``declared`` under any other
    ``analysis.kind``. ``explanation`` is the form's own reason clause, so
    the raised diagnostic reads exactly as the per-kind module wrote it.
    Caller keeps the empty-``declared`` early return; call only when at
    least one entry declares ``form``."""
    kind = str(analysis.get("kind", "")).lower()
    if kind != required:
        raise SimError(
            f"measurement '{declared[0]['name']}' declares '{form}', which "
            f"{explanation} and so requires "
            f'analysis.kind "{required}" (got {analysis.get("kind")!r})'
        )


def require_unique_measurement_names(
    measurements_spec: list[dict[str, Any]], *, form: str
) -> None:
    """Refuse a duplicate (case-insensitive) measurement name while
    ``measurements_spec`` declares ``form`` entries. ``form`` is the
    caller's own entry noun phrase exactly as the diagnostic states it
    (``"a search entry"``, ``"an snm entry"``), so the message reads as the
    per-kind module wrote it."""
    seen: set[str] = set()
    for spec in measurements_spec:
        key = str(spec.get("name")).lower()
        if key in seen:
            raise SimError(
                f"duplicate measurement name '{spec.get('name')}': names must "
                f"be unique when measurements[] declares {form}"
            )
        seen.add(key)


def read_real_columns(
    raw_path: str,
    *,
    name: str,
    columns: int,
    missing_error: str,
    empty_error: str,
    rawfile_label: str = "rawfile",
) -> tuple[list[list[float]] | None, str | None]:
    """Read the leading real columns with per-measurement failure messages."""
    if not os.path.isfile(raw_path):
        return None, missing_error
    try:
        waveform = sim_raw.parse_ascii_rawfile(raw_path)
    except (SimError, ValueError) as exc:
        return None, f"measurement '{name}': could not read its {rawfile_label}: {exc}"
    points = waveform["points"]
    if len(waveform["variables"]) < columns or not points:
        return None, empty_error
    return [[point[index] for point in points] for index in range(columns)], None


def read_complex_rawfile(
    raw_path: str,
    *,
    form: str,
    columns: int,
    vector_desc: str,
    analysis_label: str,
    expected_columns: str,
) -> tuple[sim_raw.ComplexRawfile | None, tuple[str, str] | None]:
    """Read one complex sweep with the exact set of columns the form requested."""
    if not os.path.isfile(raw_path):
        return None, (
            f"{form}_vector_missing",
            f"its {vector_desc} vector did not resolve against this {analysis_label} "
            "analysis's saved vectors (no rawfile was written) -- check the node "
            "names, and that options.save_mode does not omit them",
        )
    try:
        raw = sim_raw.read_complex_ascii_rawfile(raw_path)
    except sim_raw.RawFileError as exc:
        return None, (f"{form}_raw_invalid", f"could not read its rawfile: {exc}")
    if len(raw.names) != columns or not raw.points:
        return None, (
            f"{form}_raw_invalid",
            f"its rawfile must hold {expected_columns} with samples",
        )
    return raw, None
