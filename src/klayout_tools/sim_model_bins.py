"""Bin-card parser and per-instance bin-range lookup (issue #5138).

Binned SPICE model libraries (gf180mcu, sky130, ...) ship one ``.model
<name>.<n>`` card per geometry bin, each carrying ``lmin``/``lmax``/``wmin``/
``wmax`` in metres. ngspice aborts with ``could not find a valid modelname``
when an instance's ``w``/``l`` falls inside no bin. This module reads those
bins from the resolved model library so the preflight can name the instance
and the nearest bin instead of relying on a fixed width threshold.

Everything here is best-effort and advisory: unreadable files, unparseable
cards and unbinned models yield ``None``/empty results, never an exception.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any

from . import _spice_number as spice_number

#: Cap on library files followed through ``.include``/``.lib`` per run.
_MAX_FILES = 64

#: ``(lmin, lmax, wmin, wmax)`` in metres; ``None`` bounds are unconstrained.
Bin = tuple[float, float, float, float]

_MODEL_RE = re.compile(r"^\s*\.model\s+(\S+)\s+(.*)$", re.IGNORECASE)
_BIN_NAME_RE = re.compile(r"^(?P<base>.+)\.(?P<idx>\d+)$")
_KEY_RE = re.compile(r"\b(lmin|lmax|wmin|wmax)\s*=\s*([^\s)]+)", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"^\s*\.(?:include|inc|lib)\s+(.+)$", re.IGNORECASE)
_INST_RE = re.compile(r"^\s*([MX]\S+)\s+(?:\S+\s+){4}(\S+)(.*)$", re.IGNORECASE)
_PARAM_RE = re.compile(r"(\w+)\s*=\s*(\S+)")


def _num(raw: str) -> float | None:
    match = spice_number.NUMBER_RE.match(raw.strip().strip("'\"{}"))
    if match is None:
        return None
    return float(match.group(1)) * spice_number.suffix_scale(match.group(2))


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("+") and lines:
            lines[-1] += " " + raw.lstrip()[1:]
        else:
            lines.append(raw)
    return lines


def _include_targets(line: str, base_dir: str) -> list[str]:
    match = _INCLUDE_RE.match(line)
    if match is None:
        return []
    first = match.group(1).split()[0].strip("'\"")
    if not first or "$" in first:
        return []
    return [first if os.path.isabs(first) else os.path.join(base_dir, first)]


def parse_bins(library_path: str | None) -> dict[str, list[Bin]]:
    """``{lowercased model base name: [bin, ...]}`` for ``library_path`` and
    the files it ``.include``s/``.lib``s (bounded; never raises)."""
    bins: dict[str, list[Bin]] = {}
    if not library_path:
        return bins
    seen: set[str] = set()
    queue = [library_path]
    while queue and len(seen) < _MAX_FILES:
        path = os.path.normpath(queue.pop(0))
        if path in seen:
            continue
        seen.add(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        base_dir = os.path.dirname(path)
        for line in _logical_lines(text):
            queue.extend(_include_targets(line, base_dir))
            model = _MODEL_RE.match(line)
            if model is None:
                continue
            name = _BIN_NAME_RE.match(model.group(1).strip("`'\""))
            if name is None:
                continue
            keys = {k.lower(): _num(v) for k, v in _KEY_RE.findall(model.group(2))}
            if not keys or any(v is None for v in keys.values()):
                continue
            inf = math.inf
            bins.setdefault(name.group("base").lower(), []).append(
                (
                    keys.get("lmin") if keys.get("lmin") is not None else 0.0,  # type: ignore[arg-type]
                    keys.get("lmax") if keys.get("lmax") is not None else inf,  # type: ignore[arg-type]
                    keys.get("wmin") if keys.get("wmin") is not None else 0.0,  # type: ignore[arg-type]
                    keys.get("wmax") if keys.get("wmax") is not None else inf,  # type: ignore[arg-type]
                )
            )
    return bins


def _distance(bin_: Bin, w: float, length: float) -> float:
    lmin, lmax, wmin, wmax = bin_
    dl = max(lmin - length, 0.0, length - lmax)
    dw = max(wmin - w, 0.0, w - wmax)
    return math.hypot(dl, dw)


def find_miss(netlist_text: str, bins: dict[str, list[Bin]]) -> dict[str, Any] | None:
    """The first MOS instance whose ``w``/``l`` lies inside no bin of its
    binned model, as ``{instance, model, w_um, l_um, nearest_bin}``; else
    ``None``. Expression-valued or missing ``w``/``l`` are skipped; ``nf`` is
    ignored (keyed on ``w`` as written, #4976); ``m=`` does not exempt."""
    if not bins:
        return None
    for line in _logical_lines(netlist_text):
        match = _INST_RE.match(line)
        if match is None:
            continue
        model = match.group(2)
        cards = bins.get(model.strip("`'\"").lower())
        if not cards:
            continue
        params = {k.lower(): v for k, v in _PARAM_RE.findall(match.group(3))}
        w = _num(params["w"]) if "w" in params else None
        length = _num(params["l"]) if "l" in params else None
        if w is None or length is None:
            continue
        if any(b[0] <= length <= b[1] and b[2] <= w <= b[3] for b in cards):
            continue
        nearest = min(cards, key=lambda b: _distance(b, w, length))
        return {
            "instance": match.group(1),
            "model": model,
            "w_um": w * 1e6,
            "l_um": length * 1e6,
            "nearest_bin": {
                "lmin_um": nearest[0] * 1e6,
                "lmax_um": None if math.isinf(nearest[1]) else nearest[1] * 1e6,
                "wmin_um": nearest[2] * 1e6,
                "wmax_um": None if math.isinf(nearest[3]) else nearest[3] * 1e6,
            },
        }
    return None
