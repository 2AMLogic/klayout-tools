"""Measured gap reasons for V1-V3 corner spacing (ASAP7 Table 3.13.1)."""

#: V1.S.2-S.4 and V2.S.2-S.4 (upper metal M2 / M3).
CORNER_SPACING_M2_M3 = (
    "In the pinned routed slice every close pair passes with the sole axis "
    "that carries a full 5 nm strip on at least one end of each cut, but each "
    "fixed-axis reading flags one family whole: 2,357 of 2,357 V1 pairs on "
    "the vertical ends and 1,104 of 1,104 V2 pairs on the horizontal ends, "
    "and each pinned library's six close V1 pairs behave the same. The DRM "
    "fixes no M2 or M3 routing direction and Fig. 3.13.1 shows straight "
    "landings only, so a junction's two candidate facing ends have no "
    "ratified choice"
)

#: V3.S.2-S.4 (upper metal M4, horizontal by section 3.14).
CORNER_SPACING_M4 = (
    "The pinned routed slice holds no V3 pair closer than 59.09 nm, and every "
    "one of its 4,347 cuts carries the 11 nm M4 strip V3.M4.EN.2 demands on "
    "both horizontal ends, so geometry that satisfies EN.2 and the exact "
    "width V3.M4.AUX.2 always presents two capped horizontal facing ends and "
    "only the 23 nm bound can apply: each 27/30 nm case coincides with an "
    "EN.2 violation, and an arm across the track breaks AUX.2, so no "
    "available geometry exercises the choice the table leaves open"
)
