"""``options.complex_vectors`` for ``klt sim`` (issue #3337).

An AC request can declare single-ended node vectors whose complex samples it
wants back::

    {"analysis": {"kind": "ac", "args": "dec 10 1 1meg"},
     "options": {"complex_vectors": ["v(out)", "v(in)"]}}

Each corner dumps the declared vectors of its finished ``ac`` analysis to an
ASCII rawfile (``let`` + ``write``, like :mod:`klayout_tools.sim_transfer_meas`),
decodes it with :mod:`klayout_tools.sim_raw` -- phase kept, no log scraping --
and writes the decoded frequency axis and samples to a sidecar JSON next to the
corner's other artifacts. The corner reports the sidecar's path under
``artifacts.complex_vectors`` and a small ``complex_vectors`` summary (names
and point count); the arrays are never inlined in the report.

The sidecar rides the ordinary per-corner artifact directory, so the ``batch``
and ``remote`` backends return it through the same path rewriting as every
other artifact. ``keep_artifacts`` is forced on whenever vectors are declared,
because the sidecar *is* the result.

Every failure (rawfile absent or unreadable, a declared vector missing from it,
no samples, a non-finite sample) is an ``error`` diagnostic whose message names
the vector -- never a quiet ``pass``.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any

from . import sim_raw, sim_transfer_meas
from .sim_errors import SimError

OPTION = "complex_vectors"

#: Reason tokens a failed vector's diagnostic message leads with.
ERROR_CODES = (
    "complex_vector_missing",
    "complex_vector_raw_invalid",
    "complex_vector_non_finite",
)

SIDECAR_FILENAME = "complex_vectors.json"
RAW_FILENAME = "complex_vectors.raw"

_VECTOR_PREFIX = "__klt_cv_"


def validate(
    options: dict[str, Any], analysis: dict[str, Any], save_mode: str
) -> tuple[str, ...]:
    """The normalized ``options.complex_vectors`` selectors (empty when the
    option is absent), refused before any corner launches when malformed, not
    an AC analysis, or saved away by ``options.save_mode: "netlist"``."""
    if OPTION not in options or options[OPTION] is None:
        return ()
    raw = options[OPTION]
    if not isinstance(raw, list) or not raw:
        raise SimError(
            "options.complex_vectors must be a non-empty array of single-ended "
            '"v(node)" selectors, e.g. ["v(out)"]'
        )
    vectors: list[str] = []
    for entry in raw:
        selector = sim_transfer_meas.normalize_selector(entry)
        if selector is None:
            raise SimError(
                'options.complex_vectors entries must be single-ended "v(node)" '
                "selectors with one node token (no whitespace, comma or "
                f"parenthesis); got {entry!r}"
            )
        if selector not in vectors:
            vectors.append(selector)
    kind = str(analysis.get("kind", "")).lower()
    if kind != "ac":
        raise SimError(
            "options.complex_vectors returns AC sweep samples and so requires "
            f'analysis.kind "ac" (got {analysis.get("kind")!r})'
        )
    if save_mode != "all":
        raise SimError(
            "options.complex_vectors is incompatible with options.save_mode "
            f'"{save_mode}": klt cannot guarantee the declared vectors are '
            "saved; omit options.save_mode"
        )
    return tuple(vectors)


def refuse_for_engine(engine: str) -> None:
    if engine != "ngspice":
        raise SimError(
            f"options.complex_vectors is not supported for engine '{engine}': "
            "the vectors are dumped by ngspice `let`/`write` commands inside a "
            "`.control` block; use engine 'ngspice'"
        )


def sidecar_path(corner_dir: str) -> str:
    return os.path.join(corner_dir, SIDECAR_FILENAME)


def rawfile_path(corner_dir: str) -> str:
    return os.path.join(corner_dir, RAW_FILENAME)


def control_lines(vectors: tuple[str, ...], corner_dir: str) -> list[str]:
    """One ``let`` per vector and one ``write`` of all of them; empty without
    declared vectors (deck byte-identical to before)."""
    if not vectors:
        return []
    names = [f"{_VECTOR_PREFIX}{i}" for i in range(len(vectors))]
    lines = [
        f"let {name} = {vector}" for name, vector in zip(names, vectors, strict=True)
    ]
    lines.append(f"write {rawfile_path(corner_dir)} {' '.join(names)}")
    return lines


@dataclass
class VectorOutcome:
    """One corner's result: the sidecar path and point count, or errors."""

    path: str | None = None
    points: int = 0
    diagnostics: list[dict[str, str]] = field(default_factory=list)


def _bare_name(name: str) -> str:
    """A rawfile variable name without a ``v(...)`` wrapper."""
    if name.startswith("v(") and name.endswith(")"):
        return name[2:-1]
    return name


def _error(code: str, message: str) -> dict[str, str]:
    return {
        "severity": "error",
        "code": "measurement",
        "message": f"[{code}] {message}",
    }


def _fail(code: str, message: str) -> VectorOutcome:
    return VectorOutcome(diagnostics=[_error(code, message)])


def extract_corner(vectors: tuple[str, ...], corner_dir: str) -> VectorOutcome:
    """Decode one finished corner's rawfile and write its sidecar. Never
    raises: any problem becomes an ``error`` diagnostic naming the vector."""
    joined = ", ".join(vectors)
    raw_path = rawfile_path(corner_dir)
    if not os.path.isfile(raw_path):
        return _fail(
            "complex_vector_missing",
            f"complex vector(s) {joined} were not written (no rawfile): check "
            "the node names and that the AC analysis completed",
        )
    try:
        raw = sim_raw.read_complex_ascii_rawfile(raw_path)
    except sim_raw.RawFileError as exc:
        return _fail(
            "complex_vector_raw_invalid",
            f"complex vector(s) {joined}: could not read the rawfile: {exc}",
        )
    columns = {_bare_name(name): index for index, name in enumerate(raw.names)}
    diagnostics: list[dict[str, str]] = []
    for i, vector in enumerate(vectors):
        if f"{_VECTOR_PREFIX}{i}" not in columns:
            diagnostics.append(
                _error(
                    "complex_vector_missing",
                    f"complex vector {vector} did not resolve against this AC "
                    "analysis's saved vectors",
                )
            )
    if diagnostics:
        return VectorOutcome(diagnostics=diagnostics)
    if not raw.points:
        return _fail(
            "complex_vector_raw_invalid",
            f"complex vector(s) {joined}: the rawfile holds no samples",
        )
    freqs = [z.real for z in raw.column(0)]
    data: dict[str, dict[str, list[float]]] = {}
    for i, vector in enumerate(vectors):
        column = raw.column(columns[f"{_VECTOR_PREFIX}{i}"])
        if not all(math.isfinite(z.real) and math.isfinite(z.imag) for z in column):
            diagnostics.append(
                _error(
                    "complex_vector_non_finite",
                    f"complex vector {vector} has a non-finite sample",
                )
            )
            continue
        data[vector] = {
            "real": [z.real for z in column],
            "imag": [z.imag for z in column],
        }
    if not all(math.isfinite(f) for f in freqs):
        diagnostics.append(
            _error(
                "complex_vector_non_finite",
                f"complex vector(s) {joined}: the frequency axis has a "
                "non-finite sample",
            )
        )
    if diagnostics:
        return VectorOutcome(diagnostics=diagnostics)
    path = sidecar_path(corner_dir)
    sidecar = {
        "vectors": list(vectors),
        "points": len(freqs),
        "frequency_hz": freqs,
        "data": data,
    }
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(sidecar, handle, sort_keys=True)
    except OSError as exc:
        return _fail(
            "complex_vector_raw_invalid",
            f"complex vector(s) {joined}: could not write the sidecar: {exc}",
        )
    return VectorOutcome(path=path, points=len(freqs))


def corner_fields(vectors: tuple[str, ...], outcome: VectorOutcome) -> dict[str, Any]:
    """The additive ``complex_vectors`` corner field; empty when undeclared."""
    if not vectors:
        return {}
    return {"complex_vectors": {"names": list(vectors), "points": outcome.points}}
