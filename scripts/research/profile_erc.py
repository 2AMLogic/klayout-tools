#!/usr/bin/env python3
"""Wall-clock phase profile of one ``klt erc`` run (issue #2229).

Measures where :func:`klayout_tools.erc.run_erc` spends its time on a given
layout + spec, as **non-overlapping** top-level wall-clock totals that sum to
the end-to-end ``run_erc`` time:

- ``primary_extraction`` -- the first ``_extract_connectivity`` call (the
  stackup/vias graph every ``gates[]`` entry and ``nets[]`` finding reads);
- ``candidate_walk`` -- everything from the moment that call returns until
  ``_floating_gate_findings`` is entered: the ``active_layer`` region build,
  the candidate sort, and the complete ``for net in candidates`` loop,
  including every ``_accumulated_levels`` (full mode) or
  ``_gate_is_floating`` (``--findings-only``) call;
- ``tie_extraction`` -- the second ``_extract_connectivity`` call, made only
  when the spec declares ``ties[]``;
- ``remainder`` -- end-to-end minus the three above (spec validation,
  ``load_layout``, device cuts, the finding rules, coverage, provenance).
  The largest named pieces of it are reported separately under
  ``remainder_breakdown``.

Nested sub-costs (``_accumulated_levels``, ``_gate_is_floating``, the
``active_layer`` region) are reported under ``candidate_walk_breakdown`` and
are *contained in* ``candidate_walk``; never add them to the top-level
totals.

**No behaviour change.** Instrumentation is done here, out of
``erc.py``: each timed function is swapped for a pass-through wrapper on the
``klayout_tools.erc`` module for the duration of one call and restored
afterwards. ``--check-identical`` additionally re-runs ``run_erc`` with no
wrappers installed and asserts that ``gates``, ``erc_findings``, ``status``
and ``erc_status`` are equal between the two runs.

``--cprofile PATH`` writes a separate ``cProfile`` run's stats to ``PATH``
(``pstats`` format) and prints the top functions by internal time. Profiler
overhead inflates that run, so its times are for attribution only; the
phase table above comes from an un-profiled run.

Deliberate, manual, **never a CI step** -- the measurement depends on the
host. See ``docs/design/erc-runtime-profile.md`` for the recorded result.

Usage::

    python scripts/research/profile_erc.py LAYOUT SPEC [--top CELL] [--pdk sky130]
        [--findings-only] [--deck NAME] [--repeat N] [--check-identical]
        [--cprofile OUT.pstats] [--attribute-walk [--attribute-walk-limit N]]
"""

from __future__ import annotations

import argparse
import contextlib
import cProfile
import io
import json
import pstats
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from klayout_tools import erc  # noqa: E402

# Top-level helpers run_erc calls directly, outside the candidate walk.
_REMAINDER_FUNCS = (
    "load_layout",
    "_device_body_cuts",
    "_floating_gate_findings",
    "_net_connectivity_findings",
    "_tie_findings",
    "_degenerate_tie_reasons",
    "_undeclared_stream_layers",
    "build_provenance",
)


def _now() -> tuple[float, float]:
    """``(wall, cpu)`` -- process CPU time is reported beside wall clock so a
    measurement taken on a shared host can be read through its load."""
    return time.perf_counter(), time.process_time()


def _sub(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    return a[0] - b[0], a[1] - b[1]


class _Recorder:
    def __init__(self) -> None:
        self.totals: dict[str, list[float]] = {}
        self.counts: dict[str, int] = {}
        self.walk_start: tuple[float, float] | None = None
        self.walk_end: tuple[float, float] | None = None
        self.in_walk = False
        self.depth = 0
        self.counts_snapshot: dict[str, int] = {}
        self.overhead = (0.0, 0.0)

    def add(self, key: str, delta: tuple[float, float]) -> None:
        acc = self.totals.setdefault(key, [0.0, 0.0])
        acc[0] += delta[0]
        acc[1] += delta[1]
        self.counts[key] = self.counts.get(key, 0) + 1


def _start_walk(rec: _Recorder, result: Any, t1: tuple[float, float]) -> None:
    """Snapshot the primary circuit's counts and open the candidate walk.

    The circuit does not outlive run_erc's own l2n, so count it here; the
    counting time is excluded from every phase and subtracted from
    end-to-end.
    """
    nets = list(result[1].each_net())
    rec.counts_snapshot = {
        "primary_circuit_nets": len(nets),
        "candidates": sum(1 for n in nets if n.cluster_id != 0),
        "primary_circuit_subcircuits": sum(1 for _ in result[1].each_subcircuit()),
    }
    rec.walk_start = _now()
    spent = _sub(rec.walk_start, t1)
    rec.overhead = (rec.overhead[0] + spent[0], rec.overhead[1] + spent[1])
    rec.in_walk = True


def _extract_wrapper(rec: _Recorder, orig: Any) -> Any:
    """Time ``_extract_connectivity`` as primary or tie extraction."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ties = args[4] if len(args) > 4 else kwargs.get("ties")
        key = "tie_extraction" if ties else "primary_extraction"
        rec.depth += 1
        t0 = _now()
        try:
            result = orig(*args, **kwargs)
        finally:
            t1 = _now()
            rec.depth -= 1
            rec.add(key, _sub(t1, t0))
        if key == "primary_extraction":
            _start_walk(rec, result, t1)
        return result

    return wrapper


def _nested_wrapper(rec: _Recorder, name: str, orig: Any) -> Any:
    """Time a sub-cost of the candidate walk (reported, never summed)."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not rec.in_walk or rec.depth:
            return orig(*args, **kwargs)
        t0 = _now()
        try:
            return orig(*args, **kwargs)
        finally:
            rec.add(f"walk.{name}", _sub(_now(), t0))

    return wrapper


def _top_level_wrapper(rec: _Recorder, name: str, orig: Any) -> Any:
    """Time a top-level remainder helper; closes the walk on
    ``_floating_gate_findings`` entry."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if name == "_floating_gate_findings" and rec.in_walk:
            rec.walk_end = _now()
            rec.in_walk = False
        if rec.depth:
            return orig(*args, **kwargs)
        rec.depth += 1
        t0 = _now()
        try:
            return orig(*args, **kwargs)
        finally:
            rec.depth -= 1
            rec.add(f"remainder.{name}", _sub(_now(), t0))

    return wrapper


@contextlib.contextmanager
def _instrumented(rec: _Recorder) -> Iterator[None]:
    originals: dict[str, Any] = {}

    def install(name: str, factory: Any, *extra: Any) -> None:
        orig = getattr(erc, name)
        originals[name] = orig
        setattr(erc, name, factory(rec, *extra, orig))

    install("_extract_connectivity", _extract_wrapper)
    for name in ("_accumulated_levels", "_gate_is_floating", "_region"):
        install(name, _nested_wrapper, name)
    for name in _REMAINDER_FUNCS:
        install(name, _top_level_wrapper, name)

    try:
        yield
    finally:
        for name, orig in originals.items():
            setattr(erc, name, orig)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    return erc.run_erc(
        args.layout,
        args.spec,
        top=args.top,
        pdk=args.pdk,
        deck=args.deck,
        findings_only=args.findings_only,
    )


def _one_profile(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    rec = _Recorder()
    with _instrumented(rec):
        t0 = _now()
        report = _run(args)
        total = _sub(_sub(_now(), t0), rec.overhead)
    assert rec.walk_start is not None and rec.walk_end is not None

    walk = _sub(rec.walk_end, rec.walk_start)
    primary = tuple(rec.totals.get("primary_extraction", [0.0, 0.0]))
    tie = tuple(rec.totals.get("tie_extraction", [0.0, 0.0]))
    remainder = (
        total[0] - primary[0] - walk[0] - tie[0],
        total[1] - primary[1] - walk[1] - tie[1],
    )

    def entry(v: Any) -> dict[str, float]:
        return {
            "seconds": round(v[0], 3),
            "percent": round(100.0 * v[0] / total[0], 1),
            "cpu_seconds": round(v[1], 3),
            "cpu_percent": round(100.0 * v[1] / total[1], 1),
        }

    top = {
        "primary_extraction": primary,
        "candidate_walk": walk,
        "tie_extraction": tie,
        "remainder": remainder,
    }
    phase = {
        "end_to_end_s": round(total[0], 3),
        "end_to_end_cpu_s": round(total[1], 3),
        "top_level": {k: entry(v) for k, v in top.items()},
        "candidate_walk_breakdown": {
            k.removeprefix("walk."): {**entry(v), "calls": rec.counts[k]}
            for k, v in sorted(rec.totals.items())
            if k.startswith("walk.")
        },
        "remainder_breakdown": {
            k.removeprefix("remainder."): {**entry(v), "calls": rec.counts[k]}
            for k, v in sorted(rec.totals.items())
            if k.startswith("remainder.")
        },
        "counts": {
            **rec.counts_snapshot,
            "gates": len(report["gates"]),
            "erc_finding_count": report["erc_finding_count"],
        },
    }
    return report, phase


def _attribute_walk(args: argparse.Namespace) -> dict[str, Any]:
    """Statement-level attribution of the candidate walk's per-net prologue.

    ``cProfile`` cannot see the walk's ``Region`` boolean operators (``&`` is
    a C-level number slot, not a method call, so it raises no profiler
    event), and those land in ``run_erc``'s own ``tottime``. This re-runs
    the primary extraction through ``erc``'s own helpers and then times each
    statement of the walk's per-net prologue separately -- a *measurement
    replica* of those lines, not a second implementation of ``run_erc``:
    nothing it computes is compared against or fed back into a report.
    Specs declaring ``devices[]`` (or a ``--deck``) are rejected, since the
    replica does not rebuild device cuts.
    """
    spec = erc._load_spec_json(args.spec, erc.ErcError)
    if spec.get("devices") or args.deck:
        raise SystemExit("--attribute-walk does not support devices[]/--deck")
    stackup = erc._validate_stackup(spec, args.spec)
    vias = erc._validate_vias(spec, args.spec, [e["name"] for e in stackup])
    layout = erc.load_layout(args.layout, erc.ErcError)
    top_cell = erc.select_top_cells(layout, args.top, erc.ErcError)[0]
    l2n, circuit, layer_index, _ = erc._extract_connectivity(
        layout, top_cell, stackup, vias, [], {}, verb=None
    )
    gate_index = layer_index[stackup[0]["name"]]
    active_layer = stackup[0]["active_layer"]
    active_region = (
        erc._region(layout, top_cell, active_layer)
        if active_layer is not None
        else None
    )
    candidates = sorted(
        (n for n in circuit.each_net() if n.cluster_id != 0), key=lambda n: n.cluster_id
    )
    if args.attribute_walk_limit:
        candidates = candidates[: args.attribute_walk_limit]
    t = {"gate_poly_merged": 0.0, "and_active_merged": 0.0, "area": 0.0}
    gate_nets = 0
    for net in candidates:
        t0 = time.perf_counter()
        gate_poly = l2n.polygons_of_net(net, gate_index).merged()
        t1 = time.perf_counter()
        gate_area_region = (
            (gate_poly & active_region).merged()
            if active_region is not None
            else gate_poly
        )
        t2 = time.perf_counter()
        area = gate_area_region.area()
        t3 = time.perf_counter()
        t["gate_poly_merged"] += t1 - t0
        t["and_active_merged"] += t2 - t1
        t["area"] += t3 - t2
        gate_nets += area > 0
    return {
        "candidates_timed": len(candidates),
        "gate_nets": gate_nets,
        "active_layer": active_layer,
        "active_region_polygons": None
        if active_region is None
        else active_region.count(),
        "seconds": {k: round(v, 3) for k, v in t.items()},
        "ms_per_candidate": {
            k: round(1000.0 * v / max(len(candidates), 1), 3) for k, v in t.items()
        },
    }


def _layout_stats(path: str, top: str | None) -> dict[str, Any]:
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(path)
    top_cell = layout.cell(top) if top else layout.top_cell()
    return {
        "dbu": layout.dbu,
        "cells": layout.cells(),
        "top_cell": top_cell.name,
        "top_cell_instances": top_cell.child_instances(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("layout")
    parser.add_argument("spec")
    parser.add_argument("--top")
    parser.add_argument("--pdk")
    parser.add_argument("--deck")
    parser.add_argument("--findings-only", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--check-identical", action="store_true")
    parser.add_argument("--cprofile")
    parser.add_argument("--attribute-walk", action="store_true")
    parser.add_argument(
        "--attribute-walk-limit",
        type=int,
        default=0,
        help="time only the first N candidates (cluster_id order); 0 = all",
    )
    args = parser.parse_args(argv)

    out: dict[str, Any] = {
        "layout": args.layout,
        "spec": args.spec,
        "mode": "findings_only" if args.findings_only else "full",
        "pdk": args.pdk,
        "layout_stats": _layout_stats(args.layout, args.top),
        "runs": [],
    }
    report: dict[str, Any] = {}
    for _ in range(args.repeat):
        report, phase = _one_profile(args)
        out["runs"].append(phase)

    if args.check_identical:
        plain = _run(args)
        keys = ("gates", "erc_findings", "status", "erc_status")
        out["identical_to_uninstrumented"] = all(plain[k] == report[k] for k in keys)

    if args.attribute_walk:
        out["walk_attribution"] = _attribute_walk(args)

    if args.cprofile:
        prof = cProfile.Profile()
        prof.runcall(_run, args)
        prof.dump_stats(args.cprofile)
        buf = io.StringIO()
        pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(15)
        out["cprofile_top_tottime"] = buf.getvalue().splitlines()

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
