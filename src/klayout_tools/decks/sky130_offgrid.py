"""sky130 curated-deck off-grid / corner-angle rules (issue #2642).

Pure move, no behaviour change: split out of :mod:`.sky130` to keep that
module under its file-size ceiling. ``sky130`` re-exports the public names and
calls :func:`offgrid_rules` with its own provenance factory (passed in, so
this module never imports :mod:`.sky130` back).
"""

from __future__ import annotations

from collections.abc import Callable

from . import DrcRule, RuleProvenance
from .sky130_unmodeled import native_output_id

# ---------------------------------------------------------------------------
# Manufacturing-grid / corner-angle rules -- the source deck's own `OFFGRID`
# rule group (issue #2642)
# ---------------------------------------------------------------------------
#
# `sky130.lydrc` declares three rule groups it can run, as plain Ruby locals
# near the top of the script:
#
#     FEOL    = false # front-end-of-line checks
#     BEOL    = true # back-end-of-line checks
#     OFFGRID = true # manufacturing grid/angle checks
#
# -- i.e. the grid/angle group is **on by default** in the deck a foundry
# signs off against, and (unlike `FEOL`) is not one of the groups that ships
# switched off. Until this issue the curated deck transcribed nothing from it
# at all, on any layer: `klt drc`'s "clean" verdict was silent about a defect
# class the real deck actively flags, and the only way to notice was to
# cross-check a `klt precheck --grid-um 0.005` census (a separate,
# deck-independent, caller-supplied check -- see `docs/cli/precheck.md`)
# against a clean `klt drc` run on the same layout.
#
# The group's body is one or two calls per drawn layer:
#
#     dnwell.ongrid(0.005).output("dnwell_OFFGRID", "x.1b : OFFGRID vertex on dnwell")
#     dnwell.with_angle(0 .. 45).output("dnwell_angle",
#                                    "x.3a : non 45 degree angle dnwell")
#
# so the transcription below is a table with one row per source-deck layer,
# in the source block's own line order, rather than ~75 near-identical
# `DrcRule(...)` literals: a row-per-source-line table can be diffed directly
# against that contiguous section of the upstream file, which is the property
# that makes a transcription of this size reviewable. The rules it builds are
# ordinary `DrcRule` entries appended to `DECK` (`DECK += ...` below), and
# they live in *this* module on purpose -- `decks.deck_source_path()` hashes
# this file as the deck's identity for `provenance.deck.content_hash`, so a
# rule table moved to a sibling module would be invisible to that hash.
#
# Grid: **0.005um for every checked layer**, verbatim -- the source block
# passes the same literal `0.005` to all 38 `ongrid` calls, with no per-layer
# variation to get wrong. Expressed as `grid_um` (a real physical distance
# rescaled against the layout's own dbu), not `threshold_dbu`; see that
# field's docstring in `decks/rules.py`.
#
# Angle: two published limits, per layer, under two official rule ids --
# `x.3a` ("non 45 degree angle <layer>", `with_angle(0 .. 45)`) on the
# implant/well/metal layers, and `x.2` ("non 90 degree angle <layer>",
# `with_angle(0 .. 90)`) on poly, the cut/via layers, and `mf`. What the
# upstream primitive actually enforces is a *minimum interior corner angle*,
# not "every edge lies on a multiple of 45 degrees" -- see
# `DrcRule.angle_limit_deg` and `drc.py`'s `_run_angle_check` for the
# verified semantics. This deck transcribes the primitive and the value the
# source deck uses, and documents the gap, rather than "correcting" the rule
# into something whose verdict is no longer comparable to the PDK deck's own.
#
# Two deliberate deviations, both recorded rather than silently defaulted:
#
# 1. `diff`/`tap` angle: the source deck does not apply one limit to these
#    two layers. It splits each by a *three-input derived* layer --
#
#        diff.not(areaid_en.and(uhvi)).with_angle(0 .. 90) -> "x.2"
#        diff.and(areaid_en.and(uhvi)).with_angle(0 .. 45) -> "x.2c"
#
#    -- i.e. 90 degrees outside the extended-drain/ultra-high-voltage marker
#    intersection and 45 degrees inside it. `DerivedLayer` combines exactly
#    two drawn layers, so neither half is expressible here. Both rows below
#    therefore transcribe the **45-degree floor** (`x.2c`), the weaker of the
#    two limits: every corner it flags is also flagged by the official deck
#    (a corner under 45 degrees is under 90 too), so this can only
#    under-report, never produce a finding the real deck would not. That is
#    the same "transcribe the unconditional floor rather than the published
#    conditional value" pattern this module's own threshold-fidelity note
#    (see the module docstring, `li1.enclosing.licon1.1`/
#    `tap.enclosing.licon.1`) already established. These two rules are also
#    the only ones in this group left `voltage_independent=False`: their
#    source rule genuinely publishes two columns, so the
#    `coverage.voltage_domain_warnings` gate's "may have applied the wrong
#    column" premise does hold for them (issue #552/#2369). Every other row
#    here publishes a single value for every layer and voltage domain, which
#    is what `voltage_independent=True` asserts.
#
# 2. `areaid_re` (areaid.rfdiode, 81/125) is checked `ongrid` but has **no
#    `with_angle` call in the OFFGRID block at all** -- the one layer in the
#    block with a grid check and no angle check. Its angle constraint lives
#    in the `FEOL` block instead, as a differently-identified rule
#    (`areaid_re.with_angle(0 .. 90).output("rfdiode.1", ...)`), which this
#    deck does not transcribe. So its row below carries `None` for the angle
#    limit and this deck emits no `areaid_re.angle.*` rule: an absence copied
#    from the source, not an omission by oversight.
#
# Layers the OFFGRID block checks *neither* grid nor angle on are recorded in
# `OFFGRID_UNCHECKED_LAYERS` below rather than left to inference.

#: The manufacturing grid every `ongrid` call in `sky130.lydrc`'s OFFGRID
#: block passes, in micrometres (`layer.ongrid(0.005)`, 38 call sites, one
#: literal). Also the grid a caller would pass to `klt precheck --grid-um`
#: for this PDK.
OFFGRID_GRID_UM = 0.005

#: One row per layer `sky130.lydrc`'s `if OFFGRID ... end` block checks, in
#: that block's own line order:
#: ``(rule-id stem, (layer, datatype), angle limit in degrees or None,
#: official angle rule id)``. The stem is this deck's own layer naming (so
#: `m1` -> `met1`, matching `met1.width.1` et al.); the trailing comment on
#: each row is the source deck's own variable name where it differs. An
#: angle limit of `None` means the source block emits no `with_angle` call
#: for that layer (see note 2 above).
_OFFGRID_ROWS: tuple[tuple[str, tuple[int, int], float | None, str], ...] = (
    ("dnwell", (64, 18), 45.0, "x.3a"),
    ("nwell", (64, 20), 45.0, "x.3a"),
    ("pwbm", (19, 44), 45.0, "x.3a"),
    ("pwde", (124, 20), 45.0, "x.3a"),
    ("hvtp", (78, 44), 45.0, "x.3a"),
    ("hvtr", (18, 20), 45.0, "x.3a"),
    ("lvtn", (125, 44), 45.0, "x.3a"),
    ("ncm", (92, 44), 45.0, "x.3a"),
    # `x.2`/`x.2c` pair, transcribed at the 45-degree floor -- see note 1.
    ("diff", (65, 20), 45.0, "x.2c"),
    ("tap", (65, 44), 45.0, "x.2c"),
    ("tunm", (80, 20), 45.0, "x.3a"),
    ("poly", (66, 20), 90.0, "x.2"),
    ("rpm", (86, 20), 45.0, "x.3a"),
    ("npc", (95, 20), 45.0, "x.3a"),
    ("nsdm", (93, 44), 45.0, "x.3a"),
    ("psdm", (94, 20), 45.0, "x.3a"),
    ("licon1", (66, 44), 90.0, "x.2"),  # source deck: `licon`
    ("li1", (67, 20), 45.0, "x.3a"),  # source deck: `li`
    ("mcon", (67, 44), 90.0, "x.2"),
    ("vpp", (82, 64), 45.0, "x.3a"),  # lyt layer-map: `capacitor.drawing`
    ("met1", (68, 20), 45.0, "x.3a"),  # source deck: `m1`
    ("via", (68, 44), 90.0, "x.2"),
    ("met2", (69, 20), 45.0, "x.3a"),  # source deck: `m2`
    ("via2", (69, 44), 90.0, "x.2"),
    ("met3", (70, 20), 45.0, "x.3a"),  # source deck: `m3`
    ("via3", (70, 44), 90.0, "x.2"),
    ("nsm", (61, 20), 45.0, "x.3a"),
    ("met4", (71, 20), 45.0, "x.3a"),  # source deck: `m4`
    ("via4", (71, 44), 90.0, "x.2"),
    ("met5", (72, 20), 45.0, "x.3a"),  # source deck: `m5`
    ("pad", (76, 20), 45.0, "x.3a"),
    ("mf", (76, 44), 90.0, "x.2"),  # lyt layer-map: `target.drawing`
    ("hvi", (75, 20), 45.0, "x.3a"),
    ("hvntm", (125, 20), 45.0, "x.3a"),
    ("vhvi", (74, 21), 45.0, "x.3a"),
    ("uhvi", (74, 22), 45.0, "x.3a"),
    ("pwell_rs", (64, 13), 45.0, "x.3a"),  # lyt layer-map: `pwell.res`
    # Grid-checked, angle-unchecked in the OFFGRID block -- see note 2.
    ("areaid_re", (81, 125), None, ""),
)

#: Layers the source deck defines and this curated deck otherwise models, but
#: which `sky130.lydrc`'s OFFGRID block checks **neither** grid nor angle on
#: -- recorded so a reader can tell a transcribed absence from an untranscribed
#: one (issue #2642's own "never silently default" requirement). Verified by
#: reading every call in that block: the names below appear nowhere in it.
#:
#: The two MiM-capacitor top-plate marks are the interesting case, and exactly
#: the "a layer is exempted while everything that contacts it is checked"
#: shape the issue describes: `capm`/`capm2` carry width/space/enclosure rules
#: in this deck (`capm.width.1`, ...) and sit directly on top of `via3`/`via4`
#: and `met3`/`met4`, every one of which *is* grid- and angle-checked. So a
#: layout whose MiM top plate is off-grid stays clean here, not because this
#: deck declined to transcribe the rule but because the source deck declines
#: to make it.
OFFGRID_UNCHECKED_LAYERS: dict[tuple[int, int], str] = {
    (89, 44): "capm.drawing -- no ongrid/with_angle call in the OFFGRID block",
    (97, 44): "capm2.drawing (source deck `cap2m`) -- likewise unchecked",
    (82, 44): "pnp.drawing -- device-recognition mark, not a patterned layer",
    (79, 20): "urpm.drawing -- no ongrid/with_angle call in the OFFGRID block",
}


def offgrid_rules(
    provenance: Callable[[str, str], RuleProvenance],
) -> list[DrcRule]:
    """Build the `DrcRule` entries for `_OFFGRID_ROWS` (issue #2642).

    One `"ongrid"` rule per row, plus one `"angle"` rule for every row whose
    source line emits a `with_angle` call. Every value comes from the table
    above or from `OFFGRID_GRID_UM`; nothing is inferred here.
    """
    rules: list[DrcRule] = []
    for stem, layer, angle_limit_deg, angle_rule_id in _OFFGRID_ROWS:
        rid = native_output_id(stem, "OFFGRID", "x.1b")
        rules.append(
            DrcRule(
                id=f"{stem}.ongrid.1",
                description=(
                    f"{stem} vertices must be on the "
                    f"{OFFGRID_GRID_UM}um manufacturing grid"
                ),
                layer=layer,
                check="ongrid",
                threshold_dbu=0,  # unused by "ongrid" -- see grid_um
                grid_um=OFFGRID_GRID_UM,
                # sky130.lydrc OFFGRID block: `<layer>.ongrid(0.005)`
                # -> "x.1b : OFFGRID vertex on <layer>"
                scope="x",  # sky130.lydrc "x.*" rule-id family (#566)
                provenance=provenance("sky130/klayout/sky130.lydrc", rid),
                # One grid for every layer and every voltage domain: the
                # source block has no second column to have misread (#2369).
                voltage_independent=True,
            )
        )
        if angle_limit_deg is None:
            continue
        rid = native_output_id(stem, "angle", angle_rule_id)
        rules.append(
            DrcRule(
                id=f"{stem}.angle.1",
                description=(
                    f"minimum {angle_limit_deg:g}-degree interior corner "
                    f"angle on {stem}"
                ),
                layer=layer,
                check="angle",
                threshold_dbu=0,  # unused by "angle" -- see angle_limit_deg
                angle_limit_deg=angle_limit_deg,
                # sky130.lydrc OFFGRID block:
                # `<layer>.with_angle(0 .. <limit>)`
                # -> "<rule id> : non <limit> degree angle <layer>"
                scope="x",  # sky130.lydrc "x.*" rule-id family (#566)
                provenance=provenance("sky130/klayout/sky130.lydrc", rid),
                # `x.2c` is the diff/tap floor of a genuinely two-column
                # source rule (note 1 above) -- the only rows here that keep
                # the conservative default.
                voltage_independent=angle_rule_id != "x.2c",
            )
        )
    return rules
