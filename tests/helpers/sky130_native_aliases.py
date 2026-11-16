"""Native sky130A DRC output id -> deck rule id aliases (#5252, #5251).

Split out of ``test_drc_sky130_unmodeled_vs_native.py`` to keep it under the
per-file size ratchet. Each target must exist in the deck.
"""

from __future__ import annotations

#: Native output ids the deck implements under a different citation (#5252):
#: native id -> deck rule id. ``<layer>_OFFGRID`` / ``<layer>_angle`` are the
#: output names of ``sky130A_mr.drc``'s OFFGRID block, whose rule ids are
#: ``x.1b`` / ``x.3a`` / ``x.2`` (what the deck's provenance cites), and
#: ``m4.4`` is the ``sky130A.lydrc`` name of the 0.24um2 met4 area check the
#: mr deck calls ``m4.4a``. Each target must exist in the deck.
NATIVE_ALIASES: dict[str, str] = {
    "m4.4": "met4.area.1",
    # Enclosure rules cited via the mr deck's ids; kept here (not as deck
    # ``provenance``) so sky130.py stays within its size ratchet.
    "m1.4": "met1.enclosing.mcon.1",
    "m2.4": "met2.enclosing.via.1",
    "m3.4": "met3.enclosing.via2.1",
    "m4.3": "met4.enclosing.via3.1",
    "m5.3": "met5.enclosing.via4.1",
    "capm.3": "met3.enclosing.capm.1",
    "capm.4": "capm.enclosing.via3.1",
    "cap2m.3": "met4.enclosing.capm2.1",
    "cap2m.4": "capm2.enclosing.via4.1",
    # Fixed-size cut width / max-length siblings (#5251). The deck's
    # ``*.width.1`` rules are min/max-size checks; the native ids are the
    # lydrc min/max form (``via.1a``) or the mr deck's split halves.
    "ct.1_a": "mcon.width.1",
    "ct.1_b": "mcon.width.1",
    "ct.1_a/b": "mcon.width.1",
    "via.1a": "via.width.1",
    "via.1a_b": "via.width.1",
    "via2.1a": "via2.width.1",
    "via2.1a_b": "via2.width.1",
    "via3.1a": "via3.width.1",
    "via3.1_b": "via3.width.1",
    "via4.1a": "via4.width.1",
    "via4.1_b": "via4.width.1",
    **{
        f"{native}_OFFGRID": f"{stem}.ongrid.1"
        for native, stem in (
            ("m1", "met1"),
            ("m2", "met2"),
            ("m3", "met3"),
            ("m4", "met4"),
            ("m5", "met5"),
            ("mf", "mf"),
            ("nsm", "nsm"),
            ("pad", "pad"),
            ("rpm", "rpm"),
            # native ``ct`` is the mcon layer; ``licon``/``li`` are the
            # deck's ``licon1``/``li1`` rows (#5251).
            ("ct", "mcon"),
            ("licon", "licon1"),
            ("li", "li1"),
        )
    },
}
NATIVE_ALIASES.update(
    {
        k.replace("_OFFGRID", "_angle"): v.replace(".ongrid.1", ".angle.1")
        for k, v in list(NATIVE_ALIASES.items())
        if k.endswith("_OFFGRID")
    }
)
