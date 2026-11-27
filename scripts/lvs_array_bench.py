#!/usr/bin/env python3
"""Time KLayout's NetlistComparer on a generated regular array (issue #5310).

Builds an R x C array of a 6-device bitcell as SPICE, in two shapes:

* ``flat``  -- every device in the top circuit (what flat extraction yields);
* ``hier``  -- one ``BITCELL`` subcircuit instantiated R*C times.

and compares layout-side against reference-side, optionally breaking one
instance. Same ``NetlistComparer`` that ``klt lvs --engine klayout`` calls.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path

import klayout.db as db

CELL_DEVICES = (  # (name, d, g, s, b, model)
    ("M1", "BL", "WL", "Q", "VSS", "nfet"),
    ("M2", "BLB", "WL", "QB", "VSS", "nfet"),
    ("M3", "Q", "QB", "VSS", "VSS", "nfet"),
    ("M4", "QB", "Q", "VSS", "VSS", "nfet"),
    ("M5", "Q", "QB", "VDD", "VDD", "pfet"),
    ("M6", "QB", "Q", "VDD", "VDD", "pfet"),
)


def _dev(name: str, nets: tuple[str, ...], model: str) -> str:
    d, g, s, b = nets
    return f"{name} {d} {g} {s} {b} {model} W=1U L=0.15U"


def build(rows: int, cols: int, *, hier: bool, broken: str | None) -> str:
    """SPICE text. ``broken``: None | "wiring" (top-level) | "internal" (cell)."""
    out: list[str] = []
    pins = [f"WL{r}" for r in range(rows)] + [f"BL{c} BLB{c}" for c in range(cols)]
    out.append(".subckt ARRAY " + " ".join(pins) + " VDD VSS")
    for r in range(rows):
        for c in range(cols):
            wl, bl, blb = f"WL{r}", f"BL{c}", f"BLB{c}"
            if broken == "wiring" and (r, c) == (rows // 2, cols // 2):
                wl = f"WL{(r + 1) % rows}"  # one cell on the wrong wordline
            if hier:
                out.append(f"X{r}_{c} {bl} {blb} {wl} VDD VSS BITCELL")
                continue
            sub = {"BL": bl, "BLB": blb, "WL": wl, "Q": f"Q{r}_{c}", "QB": f"QB{r}_{c}"}
            for name, d, g, s, b, model in CELL_DEVICES:
                nets = tuple(sub.get(n, n) for n in (d, g, s, b))
                out.append(_dev(f"{name}_{r}_{c}", nets, model))
    out.append(".ends")
    if hier:
        out.append(".subckt BITCELL BL BLB WL VDD VSS")
        for name, d, g, s, b, model in CELL_DEVICES:
            if broken == "internal" and name == "M4":
                g = "QB"  # M4 gate tied to its own drain; in every instance
            out.append(_dev(name, (d, g, s, b), model))
        out.append(".ends")
    return "\n".join(out) + "\n"


def anonymize(text: str, seed: int = 1) -> str:
    """Rename internal nets on the layout side, as extraction does
    (internal nets carry no names), so the comparer cannot pair by name."""
    import random

    rng = random.Random(seed)
    names = sorted(set(re.findall(r"\bQB?\d+_\d+\b", text)))
    new = [f"$n{i}" for i in range(len(names))]
    rng.shuffle(new)
    m = dict(zip(names, new, strict=True))
    text = re.sub(r"\bQB?\d+_\d+\b", lambda x: m[x.group(0)], text)
    return text


def _read(path: Path) -> db.Netlist:
    nl = db.Netlist()
    nl.read(str(path), db.NetlistSpiceReader())
    return nl


def run(rows: int, cols: int, *, hier: bool, broken: str | None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp, "layout.sp"), Path(tmp, "ref.sp")
        a.write_text(anonymize(build(rows, cols, hier=hier, broken=broken)))
        b.write_text(build(rows, cols, hier=hier, broken=None))
        la, rb = _read(a), _read(b)
        for nl in (la, rb):
            nl.combine_devices()
            nl.make_top_level_pins()
            nl.purge()
        t0 = time.monotonic()
        cmp = db.NetlistComparer()
        match = cmp.compare(la, rb)
        dt = time.monotonic() - t0
    return {
        "rows": rows,
        "cols": cols,
        "devices": rows * cols * 6,
        "form": "hier" if hier else "flat",
        "broken": broken,
        "match": bool(match),
        "compare_s": round(dt, 3),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rows", type=int, default=32)
    p.add_argument("--cols", type=int, default=32)
    p.add_argument("--form", choices=("flat", "hier"), default="hier")
    p.add_argument("--broken", choices=("wiring", "internal"))
    a = p.parse_args()
    print(json.dumps(run(a.rows, a.cols, hier=a.form == "hier", broken=a.broken)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
