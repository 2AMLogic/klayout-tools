"""``analysis.passes`` -- bounded multi-pass AC acquisition (issue #3159).

A dual-injection (Tian) bench needs two AC executions at the *same* corner,
changing which injection source is active between them, and both complex
result sets retained. ``klt sim`` historically ran one analysis per corner.
This is increment 1 of that bench (#2847): **acquisition only** -- no Tian
math and no scalar metrics (#3160), and no off-host transport (#3161).

An AC ``analysis`` may carry ``passes``::

    {"kind": "ac", "args": "dec 20 1 1e8",
     "passes": [
       {"name": "inj_v", "alter": [{"target": "VINJ", "value": 1},
                                   {"target": "IINJ", "value": 0}]},
       {"name": "inj_i", "alter": [{"target": "VINJ", "value": 0},
                                   {"target": "IINJ", "value": 1}]}]}

The base ``kind``/``args`` are shared. Each ``alter`` entry sets a top-level
independent source's **AC magnitude** (``field: "ac"``, the default --
``alter VINJ ac = 1``) or, with ``field: "dc"``, a source's or passive
element's value (``alter R1 = 2k``); the target and value are validated by the
same rules as ``analysis_steps[].alter``.

Generated ``.control`` block, per pass in declaration order::

    alter VINJ ac = 1          <- the pass's COMPLETE alter set
    setplot new                <- a fresh plot: no vector can be inherited
    ac dec 20 1 1e8
    write <corner_dir>/ac_pass_<k>.raw

Why every pass must alter the same targets: ``alter`` is sticky, so a pass
that omitted a target would silently inherit the previous pass's value --
making its result depend on pass order. The request is refused instead, so
reversing the pass order provably changes nothing but the order.

Results are persisted privately, keyed by pass name, as complex ASCII
rawfiles decoded by :mod:`klayout_tools.sim_raw` (the public waveform JSON
stays magnitude-only and is untouched). A pass whose rawfile is missing or
empty is reported ``pass_failed``. The additive per-corner ``passes`` list
carries each pass's status and sample count (and ``raw`` path when
``options.keep_artifacts`` is set).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from . import sim_raw, sim_steps
from .sim_errors import SimError

#: What an ``alter`` entry changes.
FIELDS = ("ac", "dc")

_ENTRY_KEYS = frozenset({"target", "value", "field"})
_PASS_KEYS = frozenset({"name", "alter"})


def is_passes_analysis(analysis: dict[str, Any]) -> bool:
    return analysis.get("passes") is not None


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def resolve_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """``analysis`` itself when it declares no ``passes``; otherwise a copy
    whose ``passes`` are validated and normalised (``alter`` becomes ordered
    ``[target, value, field]`` triples). Raises before any engine launch."""
    if analysis.get("passes") is None:
        return analysis
    kind = str(analysis.get("kind"))
    if kind.lower() != "ac":
        raise SimError(
            f'analysis.passes requires analysis.kind "ac" (got {kind!r}): '
            "multi-pass acquisition retains complex AC results"
        )
    raw = analysis["passes"]
    if not isinstance(raw, list) or not raw:
        raise SimError(
            "analysis.passes must be a non-empty array of "
            '{"name", "alter": [{"target", "value"}, ...]} objects'
        )
    passes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        resolved = _resolve_pass(entry, index)
        key = resolved["name"].lower()
        if key in seen:
            raise SimError(
                f"analysis.passes[{index}] reuses pass name "
                f"{resolved['name']!r} (compared case-insensitively): pass "
                "names must be unique"
            )
        seen.add(key)
        passes.append(resolved)
    _require_complete_alter_sets(passes)
    return {**analysis, "passes": passes}


def _resolve_pass(entry: Any, index: int) -> dict[str, Any]:
    where = f"analysis.passes[{index}]"
    if not isinstance(entry, dict):
        raise SimError(f"{where} must be an object")
    unknown = sorted(set(entry) - _PASS_KEYS)
    if unknown:
        raise SimError(
            f"{where}: unknown key(s) {unknown}; allowed: {sorted(_PASS_KEYS)}"
        )
    name = entry.get("name")
    if not isinstance(name, str) or not sim_steps._NAME_RE.match(name):
        raise SimError(f"{where}.name must match [A-Za-z_][A-Za-z0-9_]* (got {name!r})")
    alter = entry.get("alter")
    if not isinstance(alter, list) or not alter:
        raise SimError(
            f"pass {name!r}: alter must be a non-empty array of "
            '{"target": "<source>", "value": <number>} objects -- every pass '
            "states its complete alter set so no state leaks from the "
            "previous pass"
        )
    triples: list[list[Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in alter:
        target, value, field = _resolve_alter_entry(item, name)
        key = (target.lower(), field)
        if key in seen:
            raise SimError(
                f"pass {name!r}: alter target {target!r} ({field}) is repeated"
            )
        seen.add(key)
        triples.append([target, value, field])
    return {"name": name, "alter": triples}


def _resolve_alter_entry(item: Any, name: str) -> tuple[str, Any, str]:
    if not isinstance(item, dict):
        raise SimError(
            f"pass {name!r}: each alter entry must be an object "
            '{"target", "value", "field"?}'
        )
    unknown = sorted(set(item) - _ENTRY_KEYS)
    if unknown:
        raise SimError(
            f"pass {name!r}: unknown alter key(s) {unknown}; "
            f"allowed: {sorted(_ENTRY_KEYS)}"
        )
    target = item.get("target")
    if not isinstance(target, str) or not sim_steps._ALTER_TARGET_RE.match(target):
        raise SimError(
            f"pass {name!r}: alter target {target!r} is not a top-level "
            "independent source or passive element name (V/I/R/C/L followed "
            "by [A-Za-z0-9_]); hierarchical and device-parameter targets are "
            "not supported"
        )
    value = item.get("value")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise SimError(
            f"pass {name!r}: alter value for {target!r} must be a finite "
            f"number (got {value!r})"
        )
    field = item.get("field", "ac")
    if field not in FIELDS:
        raise SimError(
            f"pass {name!r}: alter field for {target!r} must be one of "
            f"{list(FIELDS)} (got {field!r})"
        )
    if field == "ac" and target[0].upper() not in "VI":
        raise SimError(
            f"pass {name!r}: alter target {target!r} has no AC magnitude -- "
            'only an independent source (V/I) does; use "field": "dc" to '
            "change a passive element's value"
        )
    return target, value, field


def _require_complete_alter_sets(passes: list[dict[str, Any]]) -> None:
    """Every pass must alter exactly the union of all passes' targets."""
    keysets = [{(t.lower(), f) for t, _v, f in p["alter"]} for p in passes]
    union = set().union(*keysets)
    spelled = {(t.lower(), f): t for p in passes for t, _v, f in p["alter"]}
    for passed, keys in zip(passes, keysets, strict=True):
        missing = sorted(union - keys)
        if missing:
            listed = ", ".join(f"{spelled[key]} ({key[1]})" for key in missing)
            raise SimError(
                f"pass {passed['name']!r} does not alter {listed}, which "
                "another pass alters: `alter` persists, so it would inherit "
                "the previous pass's value and its result would depend on "
                "pass order -- state every pass's complete alter set"
            )


def validate_alter_targets(
    analysis: dict[str, Any], netlist_path: str, corner_points: list[Any]
) -> None:
    """The same pre-dispatch target checks ``analysis_steps[].alter`` gets
    (``corners.supply_v`` collisions, waveform-declared sources, ``.param``
    names), applied to every pass."""
    if not is_passes_analysis(analysis):
        return
    from .sim import _param_only_targets, _top_level_waveform_sources

    supply_targets = {
        key.strip().lower() for point in corner_points for key in point.supply_v
    }
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            waveforms = _top_level_waveform_sources(handle.read())
    except (OSError, UnicodeDecodeError):
        waveforms = {}
    for passed in analysis["passes"]:
        for target, _value, field in passed["alter"]:
            # A waveform source ignores `alter ... = v` (DC) but its AC
            # magnitude is independent of the transient waveform.
            sim_steps._check_alter_target(
                f"pass {passed['name']}",
                target,
                supply_targets,
                waveforms if field == "dc" else {},
            )
    dc_targets = sorted(
        {t for p in analysis["passes"] for t, _v, f in p["alter"] if f == "dc"}
    )
    params = _param_only_targets(netlist_path, dc_targets) if dc_targets else {}
    for passed in analysis["passes"]:
        for target, _value, field in passed["alter"]:
            if field == "dc" and target in params:
                raise SimError(
                    f"pass {passed['name']!r}: alter target {target!r} names "
                    "a .param, not an element: ngspice's alter cannot change "
                    "a parameter -- alter the source or element itself"
                )


def refuse_unsupported(
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    *,
    want_waveforms: bool,
    plot_requested: bool,
) -> None:
    """Refuse, by name, what multi-pass acquisition does not implement."""
    if not is_passes_analysis(analysis):
        return
    if measurements_spec:
        raise SimError(
            "analysis.passes only acquires complex AC results; "
            "request.measurements[] is not supported with it yet (the "
            "scalar-metric increment is tracked in #3160)"
        )
    if want_waveforms or plot_requested:
        raise SimError(
            "options.waveforms / --plot are not supported with "
            "analysis.passes: the waveform artifact captures one analysis "
            "per corner, and a multi-pass corner runs several"
        )


def refuse_offhost_backend(analysis: dict[str, Any], backend: str) -> None:
    if is_passes_analysis(analysis) and backend in ("remote", "batch"):
        raise SimError(
            f"analysis.passes is not supported for backend {backend!r}: the "
            "off-host worker does not return the per-pass complex rawfiles "
            "(transport is tracked in #3161). Run it with backend 'local' or "
            "'local-parallel' (a single corner may use `--backend local`)"
        )


def refuse_xyce(analysis: dict[str, Any], engine: str) -> None:
    if is_passes_analysis(analysis) and engine == "xyce":
        raise SimError(
            "analysis.passes is not supported for engine 'xyce': the passes "
            "are ngspice `alter`/`ac`/`write` commands in a `.control` "
            "block, which the Xyce deck does not have; use engine 'ngspice'"
        )


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    return os.path.join(corner_dir, f"ac_pass_{index}.raw")


def _format_value(value: float) -> str:
    return str(value) if isinstance(value, int) else repr(float(value))


def control_lines(analysis: dict[str, Any], corner_dir: str) -> list[str]:
    """The pass sequence for the generated ``.control`` block (after the
    corner prefix, before ``quit``): each pass's complete alter set, a fresh
    plot, the shared AC analysis, and a ``write`` of that plot."""
    lines = ["set filetype=ascii"]
    for index, passed in enumerate(analysis["passes"]):
        for target, value, field in passed["alter"]:
            suffix = " ac" if field == "ac" else ""
            lines.append(f"alter {target}{suffix} = {_format_value(value)}")
        lines.append("setplot new")
        lines.append(f"{analysis['kind']} {analysis['args']}".rstrip())
        lines.append(f"write {rawfile_path(corner_dir, index)}")
    return lines


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


@dataclass
class PassResult:
    """One pass's decoded complex result, or why there is none."""

    name: str
    index: int
    raw_path: str
    data: sim_raw.ComplexRawfile | None = None
    error: str | None = None


def extract_corner_passes(
    analysis: dict[str, Any], corner_dir: str
) -> list[PassResult]:
    """Decode every pass's rawfile of one finished corner, in pass order.
    Never raises: a problem becomes that pass's ``error``."""
    results: list[PassResult] = []
    for index, passed in enumerate(analysis["passes"]):
        path = rawfile_path(corner_dir, index)
        result = PassResult(name=passed["name"], index=index, raw_path=path)
        if not os.path.isfile(path):
            result.error = (
                "its AC analysis wrote no rawfile (the solve failed or never ran)"
            )
        else:
            try:
                result.data = sim_raw.read_complex_ascii_rawfile(path)
            except sim_raw.RawFileError as exc:
                result.error = f"could not read its rawfile: {exc}"
            else:
                if not result.data.plotname.lower().startswith("ac analysis"):
                    # A failed `ac` still `write`s the then-current plot
                    # (ngspice's `constants`), which must never pass as a
                    # result.
                    plot = result.data.plotname
                    result.data = None
                    result.error = (
                        f"its rawfile holds the {plot!r} plot, not an AC "
                        "analysis (the solve failed -- see this corner's log)"
                    )
                elif not result.data.points:
                    result.data = None
                    result.error = "its rawfile holds no samples"
        results.append(result)
    return results


def corner_report(
    results: list[PassResult], *, keep_artifacts: bool
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """``(passes field, diagnostics)`` for one corner. The field carries no
    complex data -- only status, sample count and (kept) rawfile path."""
    field: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    for result in results:
        ok = result.data is not None
        field.append(
            {
                "name": result.name,
                "status": "ok" if ok else "error",
                "points": len(result.data.points) if result.data else 0,
                "raw": result.raw_path if ok and keep_artifacts else None,
            }
        )
        if not ok:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "pass_failed",
                    "message": f"analysis pass {result.name!r}: {result.error}",
                }
            )
    return field, diagnostics


def corner_fields(
    analysis: dict[str, Any], corner_dir: str, *, keep_artifacts: bool
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """``(extra corner fields, diagnostics)`` for one finished corner: the
    additive ``passes`` field for an ``analysis.passes`` request, ``({}, [])``
    for every other one."""
    if not is_passes_analysis(analysis):
        return {}, []
    field, diagnostics = corner_report(
        extract_corner_passes(analysis, corner_dir), keep_artifacts=keep_artifacts
    )
    return {"passes": field}, diagnostics
