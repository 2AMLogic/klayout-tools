"""Shared geometry constants and errors for reference generators."""

#: PDK-generic contact/via cut **budget** (um) used by every phase-2
#: generator: the side every contact region, landing pad, ring band and port
#: width is laid out around (``CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM``),
#: and the drawn cut size on any layer whose cut rule is only a *minimum* --
#: it meets or exceeds sky130's ``licon1`` (no curated size rule) and
#: gf180mcu's ``contact.width.1`` (0.22um), and is widened per family where a
#: coarser minimum binds (``via_min_w_um``/``cap_top_via_min_w_um``).
#:
#: It is **not** the drawn size on a *fixed*-size cut layer (issue #2585),
#: where the foundry rule is a minimum and a maximum: sky130's ``via``
#: (0.15um) and sg13g2's ``Cont`` (0.16um)/``Via1``-``Via4`` (0.19um) would
#: all be over their maximum at 0.22um. There the drawn cut is clamped down
#: to the layer's fixed size (:func:`_cut_fixed_size_um`,
#: :func:`_clamp_cut_boxes`, :func:`_fixed_cut_side_um`) *inside* the
#: unchanged 0.22um-derived contact region -- so every pad, ring, port and
#: ``klt gen describe`` coordinate stays exactly where it was and each cut's
#: enclosure only grows.
CONTACT_SIZE_UM = 0.22

#: Margin (um) an active/poly/tap region must keep around a contact it
#: encloses -- exceeds both curated decks' enclosure rules (sky130
#: ``diff.enclosing.licon.1``/``poly.enclosing.licon.1``: 0.04um/0.05um;
#: gf180mcu ``comp.enclosing.contact.1``/``poly2.enclosing.contact.1``:
#: 0.07um each).
ENCLOSURE_MARGIN_UM = 0.1

#: Generic minimum spacing (um) between same-layer shapes of different unit
#: instances (array cells, ring segments). Clears the rules on the body layers
#: (poly/diffusion/contact/li1/lower metals), but NOT every curated rule, e.g.
#: nwell 0.6-1.27; sky130 met5 1.6, capm 0.84; sg13g2 TopMetal1/2 1.64/2.0,
#: TopVia1/2 0.42/1.06 (sg13cmos5l: level 1 only). Use the deck rule instead.
MIN_SAME_LAYER_SPACING_UM = 0.4

#: Margin (um) a well ring/tie keeps around the tap/comp region it encloses
#: -- exceeds gf180mcu's ``nwell.enclosing.comp.1`` (0.12um). sky130's
#: curated deck has no well-layer rule, so this only ever matters on
#: gf180mcu (see :data:`_PDK_ROLE_LAYERS`'s ``"well"`` entries).
WELL_ENCLOSURE_MARGIN_UM = 0.15

#: The four sides a ring gap (``ring_gap_side``) can be cut on, and the
#: outward normal (``direction_deg``) each one's reported port faces.
RING_SIDE_DIRECTIONS: dict[str, int] = {"N": 90, "S": 270, "E": 0, "W": 180}


class GenError(Exception):
    """Raised when a generation request cannot be fulfilled.

    Covers an unknown generator name, an unresolvable PDK, and invalid/
    out-of-range ``params`` -- the CLI turns this into a clean stderr
    message + exit code 1, never a traceback (see docs/cli/gen.md's exit
    code table).
    """
