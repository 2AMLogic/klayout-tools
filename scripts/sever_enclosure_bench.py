#!/usr/bin/env python3
"""Time ``_sever_terminal_enclosure_bands`` on synthetic SB.10 arrays (issue #6493).

Builds N separated halo-plus-lead resistor structures (the gf180mcu SB.10
convention from ``tests/test_extract_gf180mcu_sab_resistor.py``: a
220 nm Poly2 halo block around each 6um x 1um body plus two leads crossing
the band) and calls the helper directly against a synthetic terminal-layer
conductor -- a micro-benchmark of this function alone, not a full
``klt extract`` run. The pre-#6493 full-layer loop is kept verbatim as
``reference_sever`` so one invocation reports before/after on the same
fixtures and host.

Usage::

    uv run python scripts/sever_enclosure_bench.py [--counts 1 10 50 200 500]
        [--repeats 5] [--json]

Timings use ``time.monotonic`` around the helper call only; fixture
construction and the per-run region reset are excluded. Median over
``--repeats`` runs after one warm-up run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass

import klayout.db as kdb

from klayout_tools.extract_passive_regions import _sever_terminal_enclosure_bands

_DBU_UM = 0.001
_HALO_UM = 0.22  # SB.10 / poly2.enclosing.sab.1, 220 nm
_HALO_DBU = round(_HALO_UM / _DBU_UM)
_LAYER = (30, 0)  # gf180mcu Poly2

# Per-structure geometry, matching the test module's fixture convention:
# body 6um x 1um between x=3 and x=9, halo block grown by 220 nm, two
# 0.6um-tall leads crossing the band and continuing outward to x=0/12.
_BODY_UM = (3.0, 0.0, 9.0, 1.0)
_PITCH_X_UM = 15.0
_PITCH_Y_UM = 5.0
_COLS = 25


@dataclass
class _SpecStub:
    """The two helper attributes the function reads off a ResistorDevice."""

    name: str = "ppolyf_u"
    terminal_enclosure_um: float = _HALO_UM


@dataclass
class _LayoutStub:
    dbu: float = _DBU_UM


def _um(v: float) -> int:
    return round(v / _DBU_UM)


def build_fixture(n: int) -> tuple[kdb.Region, kdb.Region]:
    """Return ``(conductor, body)``: N separated halo+lead structures.

    The conductor is the merged union of every halo block and lead; the
    body region holds the N marked bodies (one connected component each).
    """
    conductor = kdb.Region()
    body = kdb.Region()
    for i in range(n):
        col, row = i % _COLS, i // _COLS
        ox, oy = col * _PITCH_X_UM, row * _PITCH_Y_UM
        x0, y0, x1, y1 = (
            v + (ox if j % 2 == 0 else oy) for j, v in enumerate(_BODY_UM)
        )
        left, bottom, right, top = _um(x0), _um(y0), _um(x1), _um(y1)
        halo = _HALO_DBU
        mid, half = (bottom + top) // 2, _um(0.3)
        conductor.insert(kdb.Box(left - halo, bottom - halo, right + halo, top + halo))
        conductor.insert(kdb.Box(_um(ox), mid - half, left - halo, mid + half))
        conductor.insert(kdb.Box(right + halo, mid - half, _um(ox + 12.0), mid + half))
        body.insert(kdb.Box(left, bottom, right, top))
    return conductor.merged(), body.merged()


def reference_sever(
    layout: _LayoutStub,
    recognised: list[tuple[_SpecStub, kdb.Region, tuple[int, int]]],
    bases: dict[tuple[int, int], kdb.Region],
) -> None:
    """The pre-#6493 full-layer loop, verbatim semantics (issue #6493's
    original report): per component, ``conductor - ring`` and
    ``outer.sized()`` run against the whole terminal-layer conductor."""
    severed_by_layer: dict[tuple[int, int], kdb.Region] = {}
    for spec, body, terminal_layer in recognised:
        if spec.terminal_enclosure_um <= 0.0:
            continue
        conductor = bases[terminal_layer]
        enc_dbu = max(1, int(round(spec.terminal_enclosure_um / layout.dbu)))
        layer_severed = severed_by_layer.setdefault(terminal_layer, kdb.Region())
        for component in body.merged().each():
            ring = kdb.Region(component).sized(enc_dbu)
            band = conductor & ring
            if band.is_empty():
                continue
            outer = conductor - ring
            layer_severed += band - outer.sized(enc_dbu)
    for layer, severed in severed_by_layer.items():
        if not severed.is_empty():
            bases[layer] = bases[layer] - severed


def time_variant(fn, conductor: kdb.Region, body: kdb.Region, repeats: int) -> float:
    """Median seconds per helper call, fixture construction excluded."""
    timings: list[float] = []
    for _ in range(repeats + 1):  # first run is warm-up
        fresh = conductor.dup()  # helper mutates bases: reset per run
        bases = {_LAYER: fresh}
        t0 = time.monotonic()
        fn(_LayoutStub(), [(_SpecStub(), body, _LAYER)], bases)
        timings.append(time.monotonic() - t0)
    return statistics.median(timings[1:])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--counts", type=int, nargs="+", default=[1, 10, 50, 200, 500])
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    rows = []
    for n in a.counts:
        conductor, body = build_fixture(n)
        ref_s = time_variant(reference_sever, conductor, body, a.repeats)
        new_s = time_variant(
            _sever_terminal_enclosure_bands, conductor, body, a.repeats
        )
        rows.append(
            {
                "n_bodies": n,
                "reference_total_s": round(ref_s, 6),
                "reference_per_body_ms": round(ref_s / n * 1e3, 4),
                "optimized_total_s": round(new_s, 6),
                "optimized_per_body_ms": round(new_s / n * 1e3, 4),
            }
        )
    result = {
        "python": ".".join(map(str, sys.version_info[:3])),
        "klayout": kdb.__version__,
        "dbu_um": _DBU_UM,
        "halo_um": _HALO_UM,
        "repeats": a.repeats,
        "fixture": (
            f"separated halo+lead structures, body 6x1um at 15um/5um pitch, "
            f"leads to +/-3um outside the halo, {_COLS} per row"
        ),
        "timings": rows,
    }
    if a.json:
        print(json.dumps(result))
    else:
        print(f"python {result['python']}  klayout {result['klayout']}")
        print(f"repeats={a.repeats} (median, 1 warm-up excluded)  {result['fixture']}")
        header = (
            f"{'N':>5} {'reference s':>12} {'ref ms/body':>11}"
            f"{'optimized s':>12} {'opt ms/body':>11} {'speedup':>8}"
        )
        print(header)
        for r in rows:
            speedup = (
                r["reference_total_s"] / r["optimized_total_s"]
                if r["optimized_total_s"]
                else float("inf")
            )
            print(
                f"{r['n_bodies']:>5} {r['reference_total_s']:>12.6f} "
                f"{r['reference_per_body_ms']:>11.4f} {r['optimized_total_s']:>12.6f} "
                f"{r['optimized_per_body_ms']:>11.4f} {speedup:>7.1f}x"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
