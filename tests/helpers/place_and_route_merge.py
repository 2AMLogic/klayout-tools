"""Expected reports for geometry repairs skipped outside ASAP7."""


def _skipped_via_pad_trim(variant: str) -> dict:
    """Issue #4495: off ASAP7 both halves of the via-pad pass are a *named*
    no-op: the GDS is untouched and `tip_check` reports the skip, not a clean."""
    s = {
        "status": "skipped",
        "reason": f"PDK family of '{variant}' is not ASAP7; no-op",
    }
    counts = {"cells_touched": 0, "pads_trimmed": 0, "pads_shrunk": 0, "pads_left": 0}
    tip = {"violations": 0, "via_pad_violations": 0, "rules": []}
    tip |= {"router_fill_violations": 0, "router_fill_scan": None}
    return {**s, **counts, "layers": [], "tip_check": {**s, **tip}}


def _skipped_power_stack_repair(variant: str) -> dict:
    """Issue #6909: off ASAP7 the power-stack repair is a *named* no-op,
    with the same shape `repair_power_stack` returns everywhere."""
    return {
        "status": "skipped",
        "reason": f"PDK family of '{variant}' is not ASAP7; no-op",
        "patches": 0,
        "v4_m4_enclosure": {"targets": 0, "repaired": 0, "left": 0},
        "v4_m5_enclosure": {"targets": 0, "repaired": 0, "left": 0},
        "m3_stub_spacing": {"targets": 0, "repaired": 0, "left": 0},
    }


def _skipped_m4_grid_repair(variant: str) -> dict:
    """Issue #7817: off ASAP7 the M4 grid repair is a *named* no-op, with
    the same shape `repair_m4_grid` returns everywhere."""
    return {
        "status": "skipped",
        "reason": f"PDK family of '{variant}' is not ASAP7; no-op",
        "patches": 0,
        "targets": 0,
        "repaired": 0,
        "left": 0,
    }
