"""sky130 1.8V low-threshold MOS flavour (issue #5283).

Kept out of `sky130.py` (per-file size ratchet); the provenance builder is
passed in to avoid a circular import.
"""

from __future__ import annotations

from collections.abc import Callable

from . import MOSFlavour, RuleProvenance


def lvt_mos_flavour(provenance: Callable[[str], RuleProvenance]) -> MOSFlavour:
    """Upstream `sky130.lvs` derives
    `pgate_1p8v_lvt = pgate_low_voltage_not_pass.not(hvtp).and(lvtn)` (L1196)
    -> `sky130_fd_pr__pfet_01v8_lvt` (L2051) and
    `ngate_1p8v_lvt = ngate_low_voltage_not_pass.and(lvtn)` (L1220) ->
    `sky130_fd_pr__nfet_01v8_lvt` (L2073). Keyed on lvtn (125/44) alone;
    `hvi` is excluded (those are the 5V `nvt`/`hvi` flavours), as are
    areaid_ed (81/19) and areaid_ce (81/2). `not_markers` applies to both
    polarities, so hvtp (78/44) is excluded for NMOS too -- harmless, since
    hvtp is a PMOS-only implant. Must be declared after `nvt` and `hvi`
    (flavours claim in declaration order).
    """
    return MOSFlavour(
        marker=(125, 44),  # lvtn.drawing
        flavour="lvt",
        not_markers=((75, 20), (78, 44), (81, 19), (81, 2)),
        description="sky130 1.8V low-threshold MOS (lvtn 125/44)",
        nfet_provenance=provenance("sky130_fd_pr__nfet_01v8_lvt"),
        pfet_provenance=provenance("sky130_fd_pr__pfet_01v8_lvt"),
    )
