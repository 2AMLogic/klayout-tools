"""ASAP7 post-route power-stack enclosure repair for the DEF -> GDS merge
(issue #6909).

OpenROAD ``pdngen``'s ``M2 -> M5`` power-grid via stack leaves three rule
families dirty against the packaged ASAP7 deck, none of them reachable from
any ``power.*`` request field (measured in #4494/#4499, #6030/#6180 and
again on explicit on-grid straps in #6909's tracker):

* ``V4.M4.EN.1`` (Table 3.15.1, 11 nm on two opposite sides). The only wide
  ``V4`` the tech LEF offers for a 0.216 um M5 strap is
  ``VIARULE M5_M4widePWR0p864`` -- a single 216 x 24 nm cut with
  ``ENCLOSURE 0 0 0 0``, and pdngen draws its M4 landing exactly that size,
  so the cut has zero M4 enclosure in *every* direction under every strap
  crossing.
* ``V4.M5.EN.2`` (11 nm by M5 on two opposite sides). The cut row is exactly
  as wide as the strap (``V4.M5.AUX.2`` demands that), so the left/right
  enclosure is 0 and the rule rests on the top/bottom pair -- which is also
  short at the endmost crossing of each strap, where pdngen stops the strap
  flush (or nearly so) with the last cut.
* ``M3.S.2`` (Table 3.12.1, 25 nm tip-to-side). The stack's on-grid M3 stub
  is 34 nm tall, which the deck classifies as a *short* edge (<= 36 nm), so
  a signal M3 wire on the adjacent 36 nm track -- 18 nm away, a gap that is
  legal side-to-side -- is measured tip-to-side and fails.

This pass repairs all three on the merged layout, after pdngen, the same way
:func:`~klayout_tools.place_and_route_min_area.repair_min_area` and
:func:`~klayout_tools.place_and_route_via_pad.trim_via_pads` repair what
they own:

* **M4 enclosure**: every ``V4`` cut whose enclosure by M4 fails both
  opposite-side pairs gets its M4 landing extended horizontally by 12 nm on
  the left and the right. Horizontal extension keeps every M4 rule the
  landing already satisfied: the across-track geometry (width, ``AUX.1``
  grid, ``AUX.2`` track) is untouched, and M4's along edges are grid-free
  (0.25 nm), so only the 40 nm along spacing (``M4.S.2``) can object -- and
  the pass checks that before drawing.
* **M5 enclosure**: the endmost cut of each strap gets the strap extended
  12 nm past it on the short side. The strap's across-track geometry (and
  ``V4.M5.AUX.2``, cut exactly as wide as M5) is untouched; M5's along
  direction is grid-free, so only ``M5.S.2`` (40 nm along spacing) can
  object -- checked before drawing.
* **M3 stub spacing**: every stack stub whose side edges the deck would
  classify as short (<= 36.25 nm) is extended vertically to 44 nm. This
  moves its side edges into the *long* class, which strictly *lowers* the
  spacing every horizontal neighbour can demand (25 -> 18 nm against a long
  neighbour, 27/31 -> 25 nm against a short one) without moving any edge in
  x, so no horizontal pair can start failing because of it; the pass also
  verifies the extension strip keeps its 32 nm clearance (the family's
  worst threshold is 31 nm) to every other M3 polygon below.

Every patch is drawn only when the region it adds, plus the rule-family
spacing ring around it, contains no other polygon of the same layer -- so a
patch can only ever merge with the conductor it repairs (the via stack's
own landing, strap or stub): it cannot bridge two nets. A target it cannot
repair safely is counted in ``left`` and reported, never forced.

It is a documented no-op for every other PDK family -- the GDS is not
touched at all.
"""

from __future__ import annotations

from typing import Any

from .pdk_families import pdk_variant_family

_ASAP7_FAMILY = "asap7"

#: Table 3.15.1: min enclosure of V4 by M4 / by M5 on two opposite sides.
_ENCLOSURE_NM = 11.0
#: Patches extend one nanometre past the rule they satisfy, so the repaired
#: geometry is not sitting exactly on the deck's threshold.
_PATCH_MARGIN_NM = 1.0
#: Table 3.14.1: min M4 along spacing / min M5 along spacing (M4 and M5
#: both belong to the M4-M7 width/space family with the shared S.2 = 40 nm).
_ALONG_SPACE_NM = 40.0
#: Table 3.14.1: min M4 across spacing / min M5 across spacing, the M4-M7
#: family's shared S.1 = the layer grid (24 nm for M4 and M5).
_ACROSS_SPACE_NM = 24.0
#: Table 3.12.1: the M1-M3 edge-length classes are stated with a 36 nm
#: bound; the deck encodes "short" as ``with_length(nil, 36.25 nm)`` so a
#: 0.25 nm drawing grid cannot straddle it.
_SHORT_EDGE_MAX_NM = 36.25
#: Extended stub height. Any height above ``_SHORT_EDGE_MAX_NM`` moves the
#: stub's side edges into the long class; 44 nm (the M4-M7 along minimum,
#: and within a nanometre of the DRCFILL-patched stubs' own measured
#: height) keeps a margin.
_STUB_TARGET_NM = 44.0
#: Clearance required around an M3 extension strip: the worst M1-M3 spacing
#: threshold the moved edge can face is 31 nm (S.4/S.5), so 32 nm of empty
#: M3 around the strip keeps every one of S.1-S.6 satisfied.
_M3_CLEAR_NM = 32.0

_LEF_LAYERS = ("M3", "M4", "M5", "V3", "V4")


def _report(status: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "patches": 0,
        "v4_m4_enclosure": {"targets": 0, "repaired": 0, "left": 0},
        "v4_m5_enclosure": {"targets": 0, "repaired": 0, "left": 0},
        "m3_stub_spacing": {"targets": 0, "repaired": 0, "left": 0},
    }


def _nm_to_dbu(nm: float, dbu_um: float) -> int:
    return int(round(nm / 1000.0 / dbu_um))


def _region(kdb: Any, box: Any) -> Any:
    return kdb.Region(box)


def _others_clear(zone: Any, others: Any) -> bool:
    """True when ``zone`` (a Region ring or strip) contains no polygon of
    ``others`` -- the "no other conductor nearby" safety check every patch
    must pass before it is drawn."""
    return (zone & others).is_empty()


def _repair_m4_enclosure(
    kdb: Any,
    *,
    top_cell: Any,
    m4_index: int,
    m4: Any,
    v4: Any,
    dbu_um: float,
    report: dict[str, Any],
) -> Any:
    """Extend the M4 landing of every V4 cut whose M4 enclosure fails both
    opposite-side pairs. Returns the updated M4 region."""
    enc = _nm_to_dbu(_ENCLOSURE_NM, dbu_um)
    ext = _nm_to_dbu(_ENCLOSURE_NM + _PATCH_MARGIN_NM, dbu_um)
    along = _nm_to_dbu(_ALONG_SPACE_NM, dbu_um)
    across = _nm_to_dbu(_ACROSS_SPACE_NM, dbu_um)
    # A cut has >= 11 nm of M4 on top AND bottom iff the cut grown
    # vertically by 11 nm stays inside the M4 (the three-argument sized
    # with mode 2 sizes the axes independently, which is exactly the "both
    # opposite sides" question); likewise left/right. A cut is a target
    # when BOTH pairs are short -- the deck flags a via only then, and one
    # repaired pair clears it. The intersection of the two short sets
    # reproduces the original cut rectangles (each grown cut intersects
    # only its own other-grown self).
    bad_h = v4.sized(0, enc, 2).not_inside(m4)
    bad_v = v4.sized(enc, 0, 2).not_inside(m4)
    targets = bad_h & bad_v
    counts = report["v4_m4_enclosure"]
    counts["targets"] = targets.count()
    for via in targets.each():
        b = via.bbox()
        patch = kdb.Box(b.left - ext, b.bottom, b.right + ext, b.top)
        patch_region = _region(kdb, patch)
        landing = m4.interacting(_region(kdb, b))
        # The strip of new metal, plus the spacing ring the M4-M7 family can
        # ask for around it (40 nm along, 24 nm across), must be free of
        # every other M4 polygon: no merge with a foreign conductor, no new
        # M4.S.1/M4.S.2 pair. Margins equal to the thresholds admit a
        # neighbour sitting exactly at them, which the deck accepts.
        ring = (
            _region(
                kdb,
                kdb.Box(
                    patch.left - along,
                    patch.bottom - across,
                    patch.right + along,
                    patch.top + across,
                ),
            )
            - patch_region
        )
        if not _others_clear(patch_region | ring, m4 - landing):
            counts["left"] += 1
            continue
        top_cell.shapes(m4_index).insert(patch)
        m4 += patch_region
        counts["repaired"] += 1
        report["patches"] += 1
    return m4


def _repair_m5_enclosure(
    kdb: Any,
    *,
    top_cell: Any,
    m5_index: int,
    m5: Any,
    v4: Any,
    dbu_um: float,
    report: dict[str, Any],
) -> Any:
    """Extend the M5 strap 12 nm past every endmost V4 cut whose top/bottom
    M5 enclosure is short. Returns the updated M5 region."""
    enc = _nm_to_dbu(_ENCLOSURE_NM, dbu_um)
    ext = _nm_to_dbu(_ENCLOSURE_NM + _PATCH_MARGIN_NM, dbu_um)
    along = _nm_to_dbu(_ALONG_SPACE_NM, dbu_um)
    across = _nm_to_dbu(_ACROSS_SPACE_NM, dbu_um)
    # not_inside hands back the *grown* cut rectangles; intersecting with
    # the un-grown via region recovers the original cut rect each target
    # is, so the patch below sits against the cut's own edge and merges
    # with the strap.
    targets = v4.sized(0, enc, 2).not_inside(m5) & v4
    counts = report["v4_m5_enclosure"]
    counts["targets"] = targets.count()
    for via in targets.each():
        b = via.bbox()
        strap = m5.interacting(_region(kdb, b))
        repaired = False
        for side in ("bottom", "top"):
            # The 11 nm strip past this end of the cut, inside the cut's
            # x range (V4.M5.AUX.2 keeps the cut exactly as wide as the
            # strap, so a patch adds metal only along the strap). A strip
            # the strap does not fully cover is a short end: extend past
            # it by the enclosure plus the margin.
            y0, y1 = (
                (b.bottom - enc, b.bottom) if side == "bottom" else (b.top, b.top + enc)
            )
            strip = _region(kdb, kdb.Box(b.left, y0, b.right, y1))
            if (strip - m5).is_empty():
                continue
            edge = b.bottom if side == "bottom" else b.top
            patch = (
                kdb.Box(b.left, edge - ext, b.right, edge)
                if side == "bottom"
                else kdb.Box(b.left, edge, b.right, edge + ext)
            )
            patch_region = _region(kdb, patch)
            ring = (
                _region(
                    kdb,
                    kdb.Box(
                        patch.left - across,
                        patch.bottom - along,
                        patch.right + across,
                        patch.top + along,
                    ),
                )
                - patch_region
            )
            if not _others_clear(patch_region | ring, m5 - strap):
                continue
            top_cell.shapes(m5_index).insert(patch)
            m5 += patch_region
            report["patches"] += 1
            repaired = True
        if repaired:
            counts["repaired"] += 1
        else:
            counts["left"] += 1
    return m5


def _repair_m3_stubs(
    kdb: Any,
    *,
    top_cell: Any,
    m3_index: int,
    m3: Any,
    v4: Any,
    m4: Any,
    v3: Any,
    dbu_um: float,
    report: dict[str, Any],
) -> None:
    """Lengthen every M2 -> M5 stack stub whose side edges the deck
    classifies as short, moving them into the long class where the 18 nm
    side-to-side spacing a neighbour track is entitled to applies."""
    short_max = _nm_to_dbu(_SHORT_EDGE_MAX_NM, dbu_um)
    target = _nm_to_dbu(_STUB_TARGET_NM, dbu_um)
    clear = _nm_to_dbu(_M3_CLEAR_NM, dbu_um)
    # Stack stubs: the M3 polygons carrying the V3 cuts of the wide-V4
    # stack -- V3 cuts that land on an M4 polygon carrying a V4 cut.
    landings = m4.interacting(v4)
    stack_v3 = v3.interacting(landings)
    if stack_v3.is_empty():
        return
    stubs = m3.interacting(stack_v3)
    counts = report["m3_stub_spacing"]
    for stub in stubs.each():
        b = stub.bbox()
        height = b.height()
        if height > short_max:
            # Side edges are already long: no tip-to-side classification to
            # escape, and the 18 nm adjacent-track gap already passes.
            continue
        counts["targets"] += 1
        stub_region = _region(kdb, stub)
        # Extend downward (the direction pdngen's own DRCFILL patches take)
        # past the current height to the target height; upward only when
        # below is not clear. Safety is about the edges that MOVE: the side
        # edges stay at the same x and every horizontal pair's threshold
        # only drops when they grow long, so no lateral clearance is needed
        # (demanding one would block on the very adjacent-track neighbour
        # the repair exists for). The strip itself must overlap no foreign
        # M3, and the band past its new end edge -- the family's worst
        # threshold, 31 nm, plus a nanometre -- must be empty.
        for y0, y1, band in (
            (
                b.top - target,
                b.bottom,
                kdb.Box(b.left, b.top - target - clear, b.right, b.top - target),
            ),
            (
                b.top,
                b.bottom + target,
                kdb.Box(b.left, b.bottom + target, b.right, b.bottom + target + clear),
            ),
        ):
            if y1 <= y0:
                continue
            patch = kdb.Box(b.left, y0, b.right, y1)
            patch_region = _region(kdb, patch)
            if not _others_clear(patch_region | _region(kdb, band), m3 - stub_region):
                continue
            top_cell.shapes(m3_index).insert(patch)
            counts["repaired"] += 1
            report["patches"] += 1
            break
        else:
            counts["left"] += 1


def repair_power_stack(
    kdb: Any,
    *,
    layout: Any,
    top_cell: Any,
    variant: str,
    lef_to_gds: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """Repair the pdngen M2 -> M5 stack's deck-measured enclosure and stub
    spacing violations on the merged ASAP7 layout (issue #6909), and report
    what was drawn.

    Returns ``status`` (``"repaired"``: at least one patch drawn;
    ``"clean"``: ran, nothing to repair; ``"skipped"``: did not run,
    ``reason`` says why -- every non-ASAP7 PDK reports ``"skipped"`` and its
    GDS is untouched), ``patches`` (total rectangles drawn), and one count
    triple per family: ``targets`` (geometry the deck's rule would flag),
    ``repaired`` (patched), ``left`` (not safely repairable, reported rather
    than forced). Never raises for absent input.
    """
    if pdk_variant_family(variant) != _ASAP7_FAMILY:
        return _report("skipped", f"PDK family of '{variant}' is not ASAP7; no-op")
    missing = [name for name in _LEF_LAYERS if lef_to_gds.get(name) is None]
    if missing:
        return _report(
            "skipped",
            f"no {'/'.join(missing)} layer in the KLayout layer map",
        )
    indices: dict[str, int | None] = {
        name: layout.find_layer(*lef_to_gds[name]) for name in _LEF_LAYERS
    }

    def merged(name: str) -> Any:
        index = indices[name]
        if index is None:
            return kdb.Region()
        return kdb.Region(top_cell.begin_shapes_rec(index)).merged()

    v4 = merged("V4")
    if v4.is_empty():
        return _report("clean")
    m3 = merged("M3")
    m4 = merged("M4")
    m5 = merged("M5")
    v3 = merged("V3")

    report = _report("clean")
    if indices["M4"] is not None:
        m4 = _repair_m4_enclosure(
            kdb,
            top_cell=top_cell,
            m4_index=indices["M4"],
            m4=m4,
            v4=v4,
            dbu_um=layout.dbu,
            report=report,
        )
    if indices["M5"] is not None:
        _repair_m5_enclosure(
            kdb,
            top_cell=top_cell,
            m5_index=indices["M5"],
            m5=m5,
            v4=v4,
            dbu_um=layout.dbu,
            report=report,
        )
    if indices["M3"] is not None and not v3.is_empty():
        _repair_m3_stubs(
            kdb,
            top_cell=top_cell,
            m3_index=indices["M3"],
            m3=m3,
            v4=v4,
            m4=m4,
            v3=v3,
            dbu_um=layout.dbu,
            report=report,
        )
    if report["patches"]:
        report["status"] = "repaired"
    return report
