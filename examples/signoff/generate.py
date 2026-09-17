#!/usr/bin/env python3
"""Generate the `klt signoff --manifest` worked-example fixtures (see
`docs/cli/signoff.md`).

The gap this closes (issue #1954): `klt signoff --manifest`/`--fleet` ship
and work, but every block manifest in the docs is inline prose -- there was
no committed, runnable manifest a block author could copy. This directory is
that copy-paste starting point.

What gets written:

- `block.gds` -- the block's layout: a deliberately schematic-looking sketch
  (one `diff.drawing` region crossed by two `poly.drawing` gates, plus
  `li1`/`met1` routing bars). Enough real geometry for a real, DRC-clean
  `klt drc` run against the built-in `sky130` deck; **not** a manufacturable
  current mirror, and not a substitute for one.
- `schematic.spice` / `layout.spice` -- the two sides of the LVS compare, in
  the schematic-equivalent plain-element form `klt lvs` documents. The
  layout side is hand-written here, standing in for `klt extract` output --
  see this directory's README.md ("Why synthetic").
- `lvs.request.json` -- the `klt lvs` request binding that pair.
- `drc.json` / `lvs.json` -- the **real** `klt drc`/`klt lvs --format json`
  envelopes for the above, captured verbatim. These are the evidence
  `manifest.json` cites; they are not hand-edited.
- `manifest.json` -- the block manifest itself, citing T1 items 3 (DRC) and
  4 (LVS) and nothing else, so the report comes back with a visible mix of
  `MET` and `UNMET`/`no_evidence` items.
- `fleet.json` -- a one-block fleet manifest, so `klt signoff --fleet` has a
  committed example too.

Item 3's evidence entry pins `content_hash` to whatever
`drc.json`'s own `provenance.input.content_hash` says, so the manifest
demonstrates the staleness gate and stays self-consistent across
regeneration. Item 4's does not: `klt lvs` populates no
`provenance.input` block at all (its two netlist inputs are hashed into
`environment.layout_sha256`/`reference_sha256` instead), so a pinned hash
there could only ever render `unmet`.

**No PDK required.** `klt drc --deck sky130` uses the built-in rule deck and
`klt lvs` compares two netlists in-process, so this whole example
regenerates from a bare `pip install klayout` checkout -- unlike
`examples/design-pipeline/`, which needs a resolvable sky130A install.

Run from the repo root:

    uv run python3 examples/signoff/generate.py

Regeneration is byte-identical for a fixed `klt`/KLayout build. The captured
envelopes embed `provenance.klt_version` and `provenance.klayout_version`,
so a version bump legitimately changes them -- the graded verdicts do not.
"""

import json
import os
import subprocess
import sys

import klayout.db as kdb

_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_DIR))

#: Paths as they are written into `manifest.json` and passed to `klt`.
#: Relative to `_REPO_ROOT`, because a manifest's file-backed evidence paths
#: resolve against the *invoking process's* working directory (there is no
#: manifest-relative anchoring) -- so these commands are all meant to be run
#: from the repo root, the same convention `examples/yield/` uses.
_REL = "examples/signoff"

#: The block's declared top cell, shared by the layout and both netlists.
_TOP = "current_mirror"

_SCHEMATIC_SPICE = f"""\
* Reference (schematic) netlist for `examples/signoff/`.
*
* A two-device NMOS current mirror, in the schematic-equivalent
* plain-element device form `klt lvs` requires (see docs/cli/lvs.md's
* "Netlist form") -- a leading device letter and geometric literals, not a
* PDK subcircuit call.
.subckt {_TOP} vin vout vss
M1 vin  vin vss vss nfet L=1U W=4U
M2 vout vin vss vss nfet L=1U W=4U
.ends
"""

_LAYOUT_SPICE = f"""\
* Layout-side netlist for `examples/signoff/`.
*
* Hand-written, standing in for `klt extract` output (see this directory's
* README.md, "Why synthetic"). Deliberately names its devices differently
* from schematic.spice and lists them in the other order, so `klt lvs`'s
* NetlistComparer has to match the two by graph topology rather than by
* line-for-line coincidence.
.subckt {_TOP} vin vout vss
MB vout vin vss vss nfet L=1U W=4U
MA vin  vin vss vss nfet L=1U W=4U
.ends
"""

_LVS_REQUEST = {
    "schema": "klt.lvs.request/1",
    "layout": {"netlist": "layout.spice", "top": _TOP},
    "reference": {"netlist": "schematic.spice", "top": _TOP},
}


def build_layout() -> kdb.Layout:
    """The block's layout: DRC-clean sky130 geometry, drawn by hand.

    Two `poly.drawing` gates crossing one `diff.drawing` region, plus an
    `li1` and a `met1` routing bar -- the shape of a current mirror, sized
    well clear of every `sky130` width/space/enclosure threshold in the
    built-in deck so `klt drc` reports `clean`.
    """
    layout = kdb.Layout()
    top = layout.create_cell(_TOP)

    def layer(number: int, datatype: int, name: str) -> int:
        index = layout.layer(number, datatype)
        layout.set_info(index, kdb.LayerInfo(number, datatype, name))
        return index

    # diff.drawing (65/20): the shared active region, 4.0 x 1.0 um.
    diff = layer(65, 20, "diff.drawing")
    top.shapes(diff).insert(kdb.Box(0, 0, 4000, 1000))

    # poly.drawing (66/20): two 0.2 um gates (comfortably over the 0.15 um
    # poly.width.1 floor) crossing the diff region with 0.4 um of overhang
    # on each end.
    poly = layer(66, 20, "poly.drawing")
    top.shapes(poly).insert(kdb.Box(800, -400, 1000, 1400))
    top.shapes(poly).insert(kdb.Box(2800, -400, 3000, 1400))

    # li1.drawing (67/20) and met1.drawing (68/20): wide routing bars.
    li1 = layer(67, 20, "li1.drawing")
    top.shapes(li1).insert(kdb.Box(0, 2000, 3000, 2400))
    met1 = layer(68, 20, "met1.drawing")
    top.shapes(met1).insert(kdb.Box(0, 3000, 4000, 3400))

    return layout


def _write_text(name: str, text: str) -> None:
    path = os.path.join(_DIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {path}")


def _write_json(name: str, doc: object) -> None:
    path = os.path.join(_DIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


def _capture_envelope(name: str, argv: list[str], *, ok_returncode: int = 0) -> dict:
    """Run `klt <argv>` from the repo root and commit its stdout verbatim.

    The captured envelope is the *evidence*, so it is never reshaped here --
    a hand-edited envelope would defeat the entire point of `klt signoff`.
    """
    result = subprocess.run(
        [sys.executable, "-m", "klayout_tools.cli", *argv],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != ok_returncode:
        raise SystemExit(
            f"klt {' '.join(argv)} exited {result.returncode} "
            f"(expected {ok_returncode}): {result.stderr.strip()}"
        )
    text = result.stdout
    if not text.endswith("\n"):
        text += "\n"
    _write_text(name, text)
    return json.loads(text)


def _write_gds(name: str) -> None:
    """Write `block.gds` with GDS timestamps suppressed.

    A GDS `BGNLIB`/`BGNSTR` record carries modification/access times, so a
    default write is never byte-identical twice -- which would also move
    `drc.json`'s `provenance.input.content_hash` (and therefore the
    `content_hash` `manifest.json` pins) on every regeneration. Zeroing the
    timestamps makes the whole fixture set reproducible.
    """
    options = kdb.SaveLayoutOptions()
    options.gds2_write_timestamps = False
    path = os.path.join(_DIR, name)
    build_layout().write(path, options)
    print(f"wrote {path}")


def main() -> None:
    _write_gds("block.gds")

    _write_text("schematic.spice", _SCHEMATIC_SPICE)
    _write_text("layout.spice", _LAYOUT_SPICE)
    _write_json("lvs.request.json", _LVS_REQUEST)

    drc = _capture_envelope(
        "drc.json",
        [
            "drc",
            f"{_REL}/block.gds",
            "--deck",
            "sky130",
            "--format",
            "json",
        ],
    )
    if drc["status"] != "clean":
        raise SystemExit(f"expected a clean DRC run, got status={drc['status']!r}")

    lvs = _capture_envelope(
        "lvs.json",
        [
            "lvs",
            f"{_REL}/lvs.request.json",
            "--format",
            "json",
        ],
    )
    if lvs["status"] != "match":
        raise SystemExit(f"expected an LVS match, got status={lvs['status']!r}")

    layout_hash = drc["provenance"]["input"]["content_hash"]
    _write_json(
        "manifest.json",
        {
            "block": "example-current-mirror",
            "kind": "analog",
            "evidence": {
                "3": {
                    "file": f"{_REL}/drc.json",
                    "content_hash": layout_hash,
                },
                "4": f"{_REL}/lvs.json",
            },
        },
    )
    _write_json("fleet.json", {"blocks": [f"{_REL}/manifest.json"]})


if __name__ == "__main__":
    main()
