"""``klt characterize --split-grid N`` (issue #5410): run one cell's grid as
several sim legs, split out of :mod:`characterize` (size ratchet).

A big cell's whole slew x load grid is one deck and one fleet job; past the
fleet's job ceiling it can never finish. The input-slew rows are independent
instances (tagged ``a<arc>s<slew>l<load>``), so they can live in separate
decks. ``N`` omitted or 1 is today's single leg, byte for byte.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import sim
from .characterize_leg import run_sim_leg


@dataclass(frozen=True)
class CellLegs:
    """The merged outcome of a cell's leg(s): the flat measurement values, a
    report shaped like one leg's, and the (first) leg's artifact paths."""

    values: dict[str, Any]
    sim_report: dict[str, Any]
    testbench_path: str
    sim_request_path: str
    sim_report_path: str


def resolve_split_grid(
    spec: dict[str, Any], cli_value: Any, error_cls: type[Exception]
) -> int:
    """``--split-grid`` (wins) else ``options.split_grid``; an int >= 1,
    default 1. Refused here, before any I/O."""
    if cli_value is None:
        value, where = spec.get("split_grid", 1), "request.options.split_grid"
    else:
        value, where = cli_value, "--split-grid"
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise error_cls(f"{where} must be an integer >= 1, got {value!r}")
    return value


def split_rows(slew_count: int, split_grid: int) -> list[tuple[int, ...]]:
    """Contiguous slew-row chunks, at most ``split_grid`` of them, sizes
    differing by at most one (earlier chunks take the remainder)."""
    chunks = max(1, min(split_grid, slew_count))
    base, extra = divmod(slew_count, chunks)
    rows: list[tuple[int, ...]] = []
    start = 0
    for k in range(chunks):
        size = base + (1 if k < extra else 0)
        rows.append(tuple(range(start, start + size)))
        start += size
    return rows


def run_cell_legs(
    *,
    work_dir: str,
    plan: dict[str, Any],
    split_grid: int,
    slew_count: int,
    build_plan: Callable[[frozenset[int] | None], dict[str, Any]],
    build_request: Callable[..., dict[str, Any]],
    extract: Callable[[dict[str, Any], str], dict[str, Any]],
    error_cls: type[Exception],
    backend: str | None,
    resume: bool,
    batch_shard: dict[str, Any] | None,
) -> CellLegs:
    """Run the cell's leg(s). One leg (the default) lives in ``work_dir``;
    with a split, chunk ``k`` (1-based) lives in ``work_dir/grid-<k>/`` so
    ``--resume`` and batch re-attach work per chunk. ``plan`` is the
    full-grid plan, used as-is for the single leg."""
    chunks = split_rows(slew_count, split_grid)
    split = len(chunks) > 1
    merged: dict[str, Any] = {}
    first: tuple[dict[str, Any], str, str, str] | None = None
    runtime_s = 0.0
    for k, rows in enumerate(chunks, start=1):
        leg_dir = os.path.join(work_dir, f"grid-{k}") if split else work_dir
        leg_plan = build_plan(frozenset(rows)) if split else plan
        label = f"grid chunk {k}/{len(chunks)} (slew rows {rows[0]}-{rows[-1]}): "
        paths = (
            os.path.join(leg_dir, "testbench.spice"),
            os.path.join(leg_dir, "sim-request.json"),
            os.path.join(leg_dir, "sim-report.json"),
        )
        try:
            report = run_sim_leg(
                testbench_path=paths[0],
                testbench_text=leg_plan["netlist_text"],
                sim_request=build_request(plan=leg_plan, testbench_path=paths[0]),
                sim_request_path=paths[1],
                sim_report_path=paths[2],
                sim_dir=os.path.join(leg_dir, "sim"),
                backend=backend,
                resume=resume,
                **(batch_shard or {}),
            )
        except sim.SimError as exc:
            raise error_cls(
                f"{label if split else ''}simulation failed: {exc}"
            ) from exc
        try:
            values = extract(report, paths[2])
        except error_cls as exc:
            if not split:
                raise
            raise error_cls(f"{label}{exc}") from exc
        # Leakage (lk*) and pin capacitance (cp*) ride every chunk's deck;
        # chunk 1's are the ones kept.
        merged.update(
            values
            if first is None
            else {n: v for n, v in values.items() if n[0] == "a"}
        )
        entries = report.get("corners") or [{}]
        runtime_s += float(entries[0].get("runtime_s") or 0.0)
        if first is None:
            first = (report, *paths)
    assert first is not None
    report = first[0]
    if split:
        report = copy.deepcopy(report)
        report["corners"][0]["runtime_s"] = runtime_s
    return CellLegs(merged, report, first[1], first[2], first[3])
