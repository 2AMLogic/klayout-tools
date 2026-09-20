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
  envelopes for the above, captured as emitted except for the two
  host-identity provenance fields listed under "Normalized fields" below.
  These are the evidence `manifest.json` cites; no verdict-bearing field,
  content hash, or coverage entry in them is ever hand-written.
- `manifest.json` -- the block manifest itself, citing T1 items 3 (DRC) and
  4 (LVS) and nothing else, so the report comes back with a visible mix of
  `MET` and `UNMET`/`no_evidence` items.
- `fleet.json` -- a one-block fleet manifest, so `klt signoff --fleet` has a
  committed example too.

Item 3's evidence entry pins `content_hash` to whatever
`drc.json`'s own `provenance.input.content_hash` says, so the manifest
demonstrates the staleness gate and stays self-consistent across
regeneration. Item 4's deliberately does not -- it stays a bare path string,
which is the *other* evidence-entry form a block author needs to see. It is
no longer that item 4 *cannot* pin one: `klt lvs` populates
`provenance.input` since issue #1969, carrying issue #2027's `role`
discriminator (`"netlist"` here). What that hash pins, in this example's
pre-extracted `request.layout.netlist` shape, is `layout.spice` -- the
layout-side *netlist*, not `block.gds` -- so pinning it would assert a
different staleness claim from item 3's, against a different artifact. See
this directory's README.md ("Reading the manifest").

**Normalized fields.** Two `provenance` fields describe the machine and
checkout that *regenerated* this fixture rather than the block being signed
off, so :func:`_normalize_volatile_provenance` sets both to `null` before
the envelope is written (see :data:`_NORMALIZED_PROVENANCE_PATHS` for the
full rationale). Nothing else is touched: the assertion in
:func:`_capture_envelope` re-serializes the captured stdout and fails the
run unless it reproduces the CLI's bytes exactly, so "everything but these
two fields is what `klt` actually emitted" is checked, not merely asserted
here.

**No PDK required.** `klt drc --deck sky130` uses the built-in rule deck and
`klt lvs` compares two netlists in-process, so this whole example
regenerates from a bare `pip install klayout` checkout -- unlike
`examples/design-pipeline/`, which needs a resolvable sky130A install.

Run from the repo root:

    uv run python3 examples/signoff/generate.py

Regeneration is byte-identical from any checkout of a given `klt`/KLayout
build -- `scripts/check-signoff-example.sh` (wired into CI) regenerates and
fails on any diff, so these fixtures cannot silently drift away from the
verbs that produce them again. The captured envelopes still embed
`provenance.klayout_version`, which `uv.lock` pins, so a KLayout bump
legitimately changes them and is regenerated in the same PR -- the graded
verdicts do not change.
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

#: `provenance` paths set to `null` in the committed envelopes (issue #2028).
#:
#: Every other field in these fixtures is a property of the *block* -- the
#: deck and input content hashes, the coverage rollup, the verdicts. These two
#: are properties of whichever checkout happened to run `generate.py`, so
#: committing whatever the regenerating machine produced would bake a
#: checkout-local artifact into a worked example and make a round-trip check
#: impossible to keep green:
#:
#: - `provenance.klt_version` carries the running build's identity suffix
#:   (issue #2090): `0.5.0` on a clean tagged-release checkout, but
#:   `0.5.0+g<sha>` or `0.5.0+g<sha>.dirty` otherwise. It is not even stable
#:   *within one regeneration run* -- `drc.json` is captured first, and
#:   rewriting it dirties the working tree, so `lvs.json` (captured second)
#:   would record a `.dirty` suffix its sibling does not.
#: - `provenance.deck.released` is a lookup of the deck's content hash in
#:   this build's generated release-history table
#:   (`klayout_tools.decks.history.is_deck_hash_released`). It answers
#:   "does a *released* klayout-tools ship this exact deck", which flips as
#:   decks are edited between releases and flips back when the next release
#:   regenerates the table -- for the same fixture bytes.
#:
#: `null` rather than a substituted value, because `null` is what the JSON
#: contract already reserves for "no answer recorded here" -- see
#: `docs/json-contract.md` and `is_deck_hash_released`'s tri-state, whose
#: `None` leg consumers are already required to treat as "cannot confirm",
#: never as a claim. Writing `released: true` from a checkout where it is
#: false would be a fabricated release claim; writing the dev checkout's
#: `false` (or its `.dirty` build string) would publish a fact about this
#: machine rather than about the block.
_NORMALIZED_PROVENANCE_PATHS: tuple[tuple[str, ...], ...] = (
    ("provenance", "klt_version"),
    ("provenance", "deck", "released"),
)


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


class NormalizationError(Exception):
    """A committed envelope could not be normalized as
    :data:`_NORMALIZED_PROVENANCE_PATHS` describes -- raised instead of
    silently writing an un-normalized (or differently-shaped) fixture."""


def _normalize_volatile_provenance(doc: dict) -> list[str]:
    """Set every :data:`_NORMALIZED_PROVENANCE_PATHS` entry in ``doc`` to
    ``None`` in place, returning the dotted names actually normalized.

    A path whose *parent block* is ``None`` is skipped, not an error: `klt
    lvs` compares two netlists against no rule deck at all, so its
    `provenance.deck` is legitimately ``null`` and there is no `released`
    flag under it to normalize.

    A path whose parent block *is* present but does not carry the field
    raises :class:`NormalizationError`. That is the case worth failing on:
    it means the envelope contract moved (a renamed or dropped field) and
    this normalization silently stopped covering the value it exists to
    cover -- the exact way a fixture starts encoding the regenerating
    machine again without anyone noticing.
    """
    normalized: list[str] = []
    for path in _NORMALIZED_PROVENANCE_PATHS:
        dotted = ".".join(path)
        node: object = doc
        for key in path[:-1]:
            if node is None:
                break
            if not isinstance(node, dict) or key not in node:
                raise NormalizationError(
                    f"cannot normalize {dotted}: no '{key}' block in the "
                    f"captured envelope -- has the provenance contract changed?"
                )
            node = node[key]
        if node is None:
            continue
        if not isinstance(node, dict) or path[-1] not in node:
            raise NormalizationError(
                f"cannot normalize {dotted}: the block is present but carries "
                f"no '{path[-1]}' field -- has the provenance contract changed?"
            )
        node[path[-1]] = None
        normalized.append(dotted)
    return normalized


def _capture_envelope(name: str, argv: list[str], *, ok_returncode: int = 0) -> dict:
    """Run `klt <argv>` from the repo root and commit its stdout, normalized
    only in :data:`_NORMALIZED_PROVENANCE_PATHS`.

    The captured envelope is the *evidence*, so nothing else is reshaped
    here -- a hand-edited envelope would defeat the entire point of `klt
    signoff`. That is enforced rather than promised: the parsed document is
    re-serialized with the CLI's own `json.dump(..., indent=2)` settings
    (`cli/output.py`) and compared against the captured bytes *before*
    normalization, so the only difference between this file and the verb's
    real stdout is the handful of `null`s put there deliberately.
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
    doc = json.loads(text)
    if _render(doc) != text:
        raise SystemExit(
            f"klt {' '.join(argv)} stdout does not round-trip through "
            "json.dumps(..., indent=2) -- the CLI's serialization changed, so "
            "writing the re-serialized document would reshape the captured "
            "evidence beyond the normalized provenance fields"
        )
    try:
        normalized = _normalize_volatile_provenance(doc)
    except NormalizationError as exc:
        raise SystemExit(f"{name}: {exc}") from exc
    _write_text(name, _render(doc))
    if normalized:
        print(f"  normalized to null: {', '.join(normalized)}")
    return doc


def _render(doc: object) -> str:
    """``doc`` serialized exactly the way `klt --format json` writes it."""
    return json.dumps(doc, indent=2) + "\n"


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
